"""
strategy/models.py - Fair value and signal models for all products.

Three models:
  HydrogelModel   - stable market making around microprice/EWMA
  VelvetModel     - counterparty-aware directional + fair value
  ChooserModel    - phase-aware chooser/vanilla option pricing
  PortfolioRiskModel - risk bucketing and limit enforcement

No future leak. All models only use data available at the current timestamp.
"""

import math
from collections import deque

# ── Black-Scholes helpers (duplicated from chooser_scenarios for standalone use) ──

def _norm_cdf(x):
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
    c = 1.0 - (1.0/math.sqrt(2*math.pi)) * math.exp(-0.5*x*x) * p
    return c if x >= 0 else 1.0 - c

def _bs_call(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    return S*_norm_cdf(d1) - K*math.exp(-r*T)*_norm_cdf(d1 - sigma*math.sqrt(T))

def _bs_put(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    return K*math.exp(-r*T)*_norm_cdf(-d2) - S*_norm_cdf(-d1)

def _bs_call_delta(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    return _norm_cdf(d1)

def _bs_put_delta(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    return _norm_cdf(d1) - 1.0


# ── EWMA helper ───────────────────────────────────────────────────────────────

class EWMA:
    """Exponentially weighted moving average with configurable half-life."""
    def __init__(self, halflife=50):
        self.alpha = 1 - math.exp(-math.log(2) / halflife)
        self._val = None
        self.n = 0

    def update(self, x):
        if x is None:
            return self._val
        if self._val is None:
            self._val = x
        else:
            self._val = self.alpha * x + (1 - self.alpha) * self._val
        self.n += 1
        return self._val

    @property
    def value(self):
        return self._val


# ── HydrogelModel ─────────────────────────────────────────────────────────────

class HydrogelModel:
    """
    Conservative market maker for HYDROGEL_PACK.
    Goal: small, consistent PnL. Not the primary alpha source.

    fair_value = microprice + EWMA blend
    quote_width = config-driven, widens near position limits
    """

    def __init__(self, config):
        self.config = config
        self._ewma   = EWMA(halflife=config.get("hydrogel_ewma_hl", 20))
        self._spread_ewma = EWMA(halflife=config.get("hydrogel_spread_hl", 50))
        self._position = 0
        self.limit = config.get("limits", {}).get("HYDROGEL_PACK", 200)

    def update(self, bid, bid_vol, ask, ask_vol, position):
        self._position = position
        if bid is None or ask is None:
            return

        microprice = (bid * ask_vol + ask * bid_vol) / (bid_vol + ask_vol) \
            if (bid_vol or 0) + (ask_vol or 0) > 0 else (bid + ask) / 2

        self._ewma.update(microprice)
        self._spread_ewma.update(ask - bid)

    def fair_value(self):
        if self._ewma.value is None:
            return None
        return self._ewma.value

    def quote_params(self):
        fv = self.fair_value()
        if fv is None:
            return None

        base_half_spread = self.config.get("hydrogel_half_spread", 4)
        mean_spread = self._spread_ewma.value or 8.0

        # Widen quotes when near limits
        pos_frac = abs(self._position) / self.limit
        spread_mult = 1.0 + pos_frac  # double spread at limit

        half_spread = base_half_spread * spread_mult

        # Skew: if long, lower bids; if short, raise asks
        skew = -self._position / self.limit * base_half_spread

        return {
            "fair_value": fv,
            "bid": round(fv - half_spread + skew),
            "ask": round(fv + half_spread + skew),
            "bid_size": max(1, min(10, self.limit - self._position)),
            "ask_size": max(1, min(10, self.limit + self._position)),
            "take_bid": round(fv - half_spread * 2),  # only take extreme mispricings
            "take_ask": round(fv + half_spread * 2),
        }


# ── VelvetModel ───────────────────────────────────────────────────────────────

class VelvetModel:
    """
    Fair value + directional alpha for VELVETFRUIT_EXTRACT.
    Used also for delta-hedging VEV options.

    Components:
      microprice   - instant fair value from top-of-book
      ewma_mid     - smoothed mid price
      vwap_local   - recent volume-weighted average
      cp_alpha     - directional alpha from counterparty signals

    Output: fair_value, directional_alpha, quote_skew, hedge_preference
    """

    def __init__(self, config):
        self.config = config
        self._ewma_mid  = EWMA(halflife=config.get("velvet_ewma_hl", 30))
        self._ewma_micro = EWMA(halflife=config.get("velvet_micro_hl", 10))
        self._position = 0
        self.limit = config.get("limits", {}).get("VELVETFRUIT_EXTRACT", 200)

        # VWAP: rolling window of (price, volume) pairs
        self._recent_trades = deque(maxlen=config.get("velvet_vwap_window", 20))

        # Counterparty alpha: rolling signal accumulator
        # Keys: (cp, side) -> deque of (signed_edge, weight)
        self._cp_signals = {}
        self._alpha_decay = config.get("cp_alpha_decay", 0.95)

        # Aggregated alpha
        self._alpha_ewma = EWMA(halflife=config.get("alpha_ewma_hl", 10))

    def update_book(self, bid, bid_vol, ask, ask_vol, position):
        self._position = position
        if bid is None or ask is None:
            return

        microprice = (bid * ask_vol + ask * bid_vol) / (bid_vol + ask_vol) \
            if (bid_vol or 0) + (ask_vol or 0) > 0 else (bid + ask) / 2

        self._ewma_micro.update(microprice)
        self._ewma_mid.update((bid + ask) / 2)

    def update_trade(self, buyer, seller, price, quantity, cp_profiles=None):
        """
        Record a market trade. If cp_profiles provided, extract alpha signal.
        """
        self._recent_trades.append((price, quantity))

        if cp_profiles is None:
            return

        # Compute alpha from counterparty identity
        alpha = 0.0
        n = 0
        cp_threshold = self.config.get("counterparty_threshold", 0.60)

        for cp in [buyer, seller]:
            if cp not in cp_profiles:
                continue
            prof = cp_profiles[cp]
            if not prof.get("is_informed"):
                continue
            if prof.get("confidence_score", 0) < 0.3:
                continue

            # Buyer = bullish signal, seller = bearish
            sign = 1 if cp == buyer else -1
            strength = prof.get("informed_score", 0.5) - 0.5  # 0 = neutral
            if prof.get("confidence_score", 0) > cp_threshold:
                alpha += sign * strength * 2.0
                n += 1

        if n > 0:
            self._alpha_ewma.update(alpha / n)

    def fair_value(self):
        """Blended fair value: 60% EWMA microprice + 40% EWMA mid."""
        micro = self._ewma_micro.value
        mid   = self._ewma_mid.value
        if micro is None and mid is None:
            return None
        if micro is None:
            return mid
        if mid is None:
            return micro
        return 0.6 * micro + 0.4 * mid

    def vwap(self):
        if not self._recent_trades:
            return self.fair_value()
        total_vol = sum(q for _, q in self._recent_trades)
        if total_vol == 0:
            return self.fair_value()
        return sum(p * q for p, q in self._recent_trades) / total_vol

    def directional_alpha(self):
        """
        Returns alpha in price units. Positive = bullish, negative = bearish.
        Used to adjust quotes and hedge decisions.
        """
        return (self._alpha_ewma.value or 0.0) * self.config.get("alpha_scale", 2.0)

    def quote_params(self):
        fv = self.fair_value()
        if fv is None:
            return None

        alpha = self.directional_alpha()
        base_hs = self.config.get("velvet_half_spread", 3)
        pos_frac = self._position / self.limit

        # Position skew + alpha skew
        skew = -pos_frac * base_hs * 2 + alpha * 0.5

        return {
            "fair_value": fv,
            "directional_alpha": alpha,
            "bid": round(fv - base_hs + skew),
            "ask": round(fv + base_hs + skew),
            "bid_size": max(1, min(20, self.limit - self._position)),
            "ask_size": max(1, min(20, self.limit + self._position)),
            "quote_skew": skew,
            "hedge_preference": "buy" if alpha > 1 else "sell" if alpha < -1 else "neutral",
        }


# ── ChooserModel ──────────────────────────────────────────────────────────────

class ChooserModel:
    """
    Phase-aware option pricing model for VEV_xxxx products.

    PRE_CHOICE mode:
      Uses full chooser formula: C(T) + P(t_c)
      For OTM high strikes: chooser_floor >> market price -> aggressive take
      Delta = call_delta(T) + put_delta(t_c)

    POST_CHOICE mode:
      Uses vanilla call or put depending on choice_outcome.
      Recalculates delta/gamma for hedging.

    All magic numbers are in config (parameterized).
    """

    def __init__(self, config, strike):
        self.config = config
        self.strike = strike
        self._spot_ewma = EWMA(halflife=config.get("velvet_ewma_hl", 30))
        self._mid_ewma  = EWMA(halflife=config.get("vev_mid_ewma_hl", 20))
        self._position  = 0
        self.limit = config.get("limits", {}).get(f"VEV_{strike}", 300)

    def update(self, spot, option_mid, position):
        self._spot_ewma.update(spot)
        if option_mid is not None:
            self._mid_ewma.update(option_mid)
        self._position = position

    def _get_times(self):
        """Return (T_years, t_c_years) from config."""
        T_days = self.config.get("total_expiry_days", 7)
        tc_days = self.config.get("choice_time_days", 2)
        return T_days / 365.0, tc_days / 365.0

    def _get_sigma(self):
        return self.config.get("sigma", 0.30)

    def fair_value(self, phase="PRE_CHOICE"):
        """
        Compute fair value given current spot and phase.
        Returns (fair_bid, fair_ask, delta, confidence).
        """
        S = self._spot_ewma.value
        if S is None:
            return None, None, None, 0.0

        K = self.strike
        T, t_c = self._get_times()
        sigma = self._get_sigma()
        r = self.config.get("rate", 0.0)

        if phase == "PRE_CHOICE":
            call_val = _bs_call(S, K, T, sigma, r)
            put_tc   = _bs_put(S, K, t_c, sigma, r)
            fv       = call_val + put_tc
            delta    = _bs_call_delta(S, K, T, sigma, r) + _bs_put_delta(S, K, t_c, sigma, r)
            confidence = self.config.get("pre_choice_confidence", 0.7)

        elif phase == "POST_CHOICE_CALL":
            fv       = _bs_call(S, K, T, sigma, r)
            delta    = _bs_call_delta(S, K, T, sigma, r)
            confidence = self.config.get("post_choice_confidence", 0.85)

        elif phase == "POST_CHOICE_PUT":
            fv       = _bs_put(S, K, T, sigma, r)
            delta    = _bs_put_delta(S, K, T, sigma, r)
            confidence = self.config.get("post_choice_confidence", 0.85)

        else:  # AT_CHOICE - use max of call/put as floor
            call_val = _bs_call(S, K, T, sigma, r)
            put_val  = _bs_put(S, K, T, sigma, r)
            fv       = max(call_val, put_val)
            delta    = (_bs_call_delta(S, K, T, sigma, r)
                        if call_val >= put_val
                        else _bs_put_delta(S, K, T, sigma, r))
            confidence = 0.5

        # Conservative floor: intrinsic value cannot be negative
        call_intr = max(S - K, 0.0)
        put_intr  = max(K - S, 0.0)
        chooser_floor = max(call_intr, put_intr)
        fv = max(fv, chooser_floor)  # never price below chooser floor

        return fv, delta, confidence

    def quote_params(self, bid, ask, phase="PRE_CHOICE"):
        """
        Return quote and taking parameters for this strike.

        Returns dict with:
          fair_bid, fair_ask - our fair value bounds for quoting
          take_bid_if_below  - take from book if ask is below this
          take_ask_if_above  - take from book if bid is above this
          quote_bid, quote_ask - passive quotes
          delta_estimate, confidence
        """
        S = self._spot_ewma.value
        if S is None or bid is None or ask is None:
            return None

        fv, delta, confidence = self.fair_value(phase)
        if fv is None:
            return None

        quote_half_spread = self.config.get("vev_half_spread", 1)
        take_edge = self.config.get("take_edge_min", 2.0)

        # In PRE_CHOICE, especially for high strikes, we are aggressive buyers
        is_high_strike = self.strike >= 5300
        take_mult = self.config.get("high_strike_take_mult", 2.0) if is_high_strike else 1.0

        return {
            "fair_value": fv,
            "fair_bid":   fv - quote_half_spread,
            "fair_ask":   fv + quote_half_spread,
            # Aggressive take threshold: buy if market ask << our FV
            "take_bid_threshold": fv - take_edge * take_mult,  # buy if ask < this
            # Conservative take: sell if market bid >> our FV
            "take_ask_threshold": fv + take_edge,              # sell if bid > this
            # Passive quotes
            "quote_bid":  round(fv - quote_half_spread),
            "quote_ask":  round(fv + quote_half_spread),
            "bid_size":   max(1, min(10, self.limit - self._position)),
            "ask_size":   max(1, min(10, self.limit + self._position)),
            "delta_estimate": delta,
            "confidence": confidence,
            "phase": phase,
            # Chooser floor vs market - the key edge signal
            "chooser_floor": max(max(S - self.strike, 0), max(self.strike - S, 0)),
            "market_mid":    (bid + ask) / 2,
            "edge_vs_market": fv - (bid + ask) / 2,
        }


# ── PortfolioRiskModel ────────────────────────────────────────────────────────

class PortfolioRiskModel:
    """
    Risk bucketing and limit enforcement.

    Buckets:
      hydrogel_bucket   - HYDROGEL_PACK position and notional
      velvet_bucket     - VELVETFRUIT_EXTRACT position
      chooser_bucket    - aggregate VEV delta + per-strike concentration

    Risk profiles: conservative / balanced / aggressive
    """

    PROFILES = {
        "conservative": {
            "max_total_delta": 50,   # max net VELVET delta equivalent
            "max_vev_notional": 5000,
            "max_per_strike": 50,    # max position per VEV strike
            "hydrogel_max": 100,
            "velvet_max": 100,
        },
        "balanced": {
            "max_total_delta": 100,
            "max_vev_notional": 15000,
            "max_per_strike": 150,
            "hydrogel_max": 200,
            "velvet_max": 200,
        },
        "aggressive": {
            "max_total_delta": 200,
            "max_vev_notional": 50000,
            "max_per_strike": 300,
            "hydrogel_max": 200,
            "velvet_max": 200,
        },
    }

    def __init__(self, config):
        self.config = config
        self.profile_name = config.get("risk_profile", "balanced")
        self.profile = self.PROFILES[self.profile_name]
        self.limits = config.get("limits", {})

        # State
        self.positions = {}    # symbol -> position
        self.deltas = {}       # symbol -> delta (set by models)
        self.mids   = {}       # symbol -> current mid

    def update(self, positions, deltas, mids):
        self.positions = dict(positions)
        self.deltas = dict(deltas)
        self.mids = dict(mids)

    def net_vev_delta(self):
        """Sum of (position * delta) across all VEV strikes."""
        total = 0.0
        for sym, pos in self.positions.items():
            if sym.startswith("VEV_"):
                d = self.deltas.get(sym, 0.0)
                total += pos * d
        return total

    def velvet_hedge_needed(self):
        """
        How much VELVET to buy/sell to neutralize VEV delta.
        Positive = need to buy VELVET.
        """
        vev_delta = self.net_vev_delta()
        velvet_pos = self.positions.get("VELVETFRUIT_EXTRACT", 0)
        # VEV delta already accounts for direction; hedge = -vev_delta - velvet_pos
        return -vev_delta - velvet_pos

    def position_headroom(self, symbol):
        """How much more of symbol we can buy/sell within limits."""
        pos = self.positions.get(symbol, 0)
        lim = self.limits.get(symbol, 300)
        return lim - pos, lim + pos  # (can_buy_more, can_sell_more)

    def check_order(self, symbol, qty):
        """
        Returns True if we can place this order within hard limits.
        qty > 0 = buy, < 0 = sell.
        """
        pos = self.positions.get(symbol, 0)
        lim = self.limits.get(symbol, 300)
        new_pos = pos + qty
        return -lim <= new_pos <= lim

    def clip_qty(self, symbol, qty):
        """Clip qty to what's allowed by hard limits."""
        pos = self.positions.get(symbol, 0)
        lim = self.limits.get(symbol, 300)
        if qty > 0:
            return min(qty, lim - pos)
        else:
            return max(qty, -lim - pos)

    def risk_summary(self):
        vev_delta = self.net_vev_delta()
        velvet_pos = self.positions.get("VELVETFRUIT_EXTRACT", 0)
        hydrogel_pos = self.positions.get("HYDROGEL_PACK", 0)

        vev_notional = sum(
            abs(self.positions.get(sym, 0)) * self.mids.get(sym, 0)
            for sym in self.positions if sym.startswith("VEV_")
        )

        per_strike = {
            sym: self.positions.get(sym, 0)
            for sym in self.positions if sym.startswith("VEV_")
        }

        return {
            "vev_delta": vev_delta,
            "velvet_pos": velvet_pos,
            "hydrogel_pos": hydrogel_pos,
            "vev_notional": vev_notional,
            "per_strike": per_strike,
            "total_delta_equiv": vev_delta + velvet_pos,
            "max_per_strike_conc": max((abs(v) for v in per_strike.values()), default=0),
        }

    def at_risk_limit(self, symbol, qty):
        """True if adding qty would approach risk profile limit."""
        rs = self.risk_summary()
        if symbol.startswith("VEV_"):
            new_strike_pos = abs(rs["per_strike"].get(symbol, 0) + qty)
            if new_strike_pos > self.profile["max_per_strike"]:
                return True
        elif symbol == "VELVETFRUIT_EXTRACT":
            if abs(rs["velvet_pos"] + qty) > self.profile["velvet_max"]:
                return True
        elif symbol == "HYDROGEL_PACK":
            if abs(rs["hydrogel_pos"] + qty) > self.profile["hydrogel_max"]:
                return True
        return False
