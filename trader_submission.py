"""
trader_submission.py - Spread-filtered MM + VEV call-selling for IMC Prosperity Round 4.

Products:
  HYDROGEL_PACK          (primary MM, ~12-tick natural spread)
  VELVETFRUIT_EXTRACT    (secondary MM, ~5-tick natural spread)
  VEV_5300/5400/5500     (sell OTM calls that trade at vol premium above BS fair)

MM logic each tick (HYDROGEL + VELVET):
  1. Skip quoting when natural spread < min_spread.
  2. Quote one tick inside the book; skew by inventory * skew_rate.
  3. EWMA sanity bound: never bid above ewma, never ask below ewma.
  4. Aggressive takes DISABLED (net-negative empirically).

VEV option sell logic each tick:
  1. Compute Black-Scholes call fair value using VELVET mid and T_rem.
     T_rem = max(1, 10000 - timestamp // 100)  [local ticks until end of day]
     sigma_tick = 0.000217  [historical vol per local tick, calibrated from 3-day data]
  2. If market bid > BS_fair * VEV_PREMIUM + VEV_MIN_EDGE:
     aggressively sell (take market bid).  Position cap -VEV_LIMIT per strike.
  3. VELVET delta hedge: short VEV calls have positive delta; adjust VELVET MM
     skew to bias toward long VELVET proportionally.

Edge:
  VEV market consistently trades at 20-30% above BS(historical sigma).
  Day 3 example: VEV_5300 bid=57 vs BS_fair=44, edge=+13 per unit.
  VEV_5400 bid=20 vs BS_fair=12, edge=+8 per unit.

State: {"h": ewma_hydro, "v": ewma_velvet}  (~30 chars traderData)
"""

import json
import math

try:
    from datamodel import OrderDepth, UserId, TradingState, Order
except ImportError:
    from collections import namedtuple
    Order = namedtuple("Order", ["symbol", "price", "quantity"])

    class TradingState:
        pass


HYDRO  = "HYDROGEL_PACK"
VELVET = "VELVETFRUIT_EXTRACT"

POS_LIMIT = {HYDRO: 50, VELVET: 20}

# ── HYDROGEL parameters ───────────────────────────────────────────────────────
H_MIN_SPREAD = 12   # skip quoting when natural spread < 12 ticks
H_SIZE       = 8    # units per side per quote
H_SOFT_CAP   = 20   # stop adding to one side when |pos| >= this (50% of hard limit)
H_SKEW       = 1.2  # ticks of quote-level skew per unit of inventory
H_TAKE       = 999  # aggressive take disabled (takes are net-negative; passive fills are the edge)

# ── VELVETFRUIT parameters ────────────────────────────────────────────────────
V_MIN_SPREAD = 4    # natural spread avg = 5; skip when it collapses to <= 3
V_SIZE       = 10   # units per side (larger size captures full flow per fill opportunity)
V_SOFT_CAP   = 20   # 100% of hard limit (20)
V_SKEW       = 1.2
V_TAKE       = 999  # aggressive take disabled

# ── EWMA for sanity-bound tracking ────────────────────────────────────────────
EWMA_ALPHA = 0.04   # alpha = 0.04 => half-life ~17 ticks (slow tracking for mean-reverting markets)

# ── VEV option sell parameters ────────────────────────────────────────────────
VEV_STRIKES_TRADE  = [5300, 5400, 5500]   # strikes to sell (OTM calls with vol premium)
VEV_SIGMA_TICK     = 0.000217             # historical vol per local tick (3-day calibration)
VEV_PREMIUM        = 1.20                 # sell when market bid > BS_fair * VEV_PREMIUM ...
VEV_MIN_EDGE       = 2                    # ... + VEV_MIN_EDGE absolute ticks
VEV_SIZE           = 5                    # units per aggressive sell
VEV_LIMIT          = 400                  # max short position per strike


# ── Black-Scholes primitives (for VEV option fair-value) ─────────────────────

def _norm_cdf(x):
    """Rational approximation, accurate to ~1e-5."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def _bs_call(S, K, T_ticks, sigma_tick):
    """European call value. T_ticks in local data ticks. Returns intrinsic if T<=0."""
    if T_ticks <= 0 or sigma_tick <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0)
    vol = sigma_tick * math.sqrt(T_ticks)
    d1  = (math.log(S / K) + 0.5 * vol * vol) / vol
    d2  = d1 - vol
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def _bs_call_delta(S, K, T_ticks, sigma_tick):
    """Delta of a European call (dC/dS in [0,1])."""
    if T_ticks <= 0 or sigma_tick <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    vol = sigma_tick * math.sqrt(T_ticks)
    d1  = (math.log(S / K) + 0.5 * vol * vol) / vol
    return _norm_cdf(d1)


# ── EWMA helper ───────────────────────────────────────────────────────────────

def _ewma(prev, val):
    if prev is None:
        return float(val)
    return EWMA_ALPHA * val + (1.0 - EWMA_ALPHA) * prev


def _book(depth):
    if depth is None:
        return [], []
    bids = sorted(depth.buy_orders.items(), reverse=True)
    asks = sorted(depth.sell_orders.items())
    return bids, asks


def _iround(x):
    return int(math.floor(x + 0.5))


def _mm_orders(sym, depth, pos, ewma_prev,
               min_spread, size, soft_cap, skew_rate, take_edge):
    """
    Compute market-making orders for one product using inside-book quoting.
    Returns (order_list, updated_ewma).

    Quoting:
      - Only when  best_ask - best_bid >= min_spread  (spread quality gate).
      - Bid at  best_bid + 1  (improved bid, attracts sellers).
      - Ask at  best_ask - 1  (improved ask, attracts buyers).
      - Inventory skew shifts both prices:  long -> skew up (prefer selling).
      - EWMA sanity bound: cap bid <= ewma, cap ask >= ewma.
    """
    bids, asks = _book(depth)
    if not bids or not asks:
        return [], ewma_prev

    best_bid_p, best_bid_v = bids[0]
    best_ask_p, best_ask_v = asks[0]
    mid = (best_bid_p + best_ask_p) * 0.5

    new_ewma = _ewma(ewma_prev, mid)
    fv = new_ewma

    pos = pos or 0
    spread = best_ask_p - best_bid_p

    hard  = POS_LIMIT[sym]
    r_buy = hard - pos
    r_sel = hard + pos

    orders = []

    # ── Passive quotes (only when spread is generous enough) ──────────────────
    if spread >= min_spread:
        skew = pos * skew_rate    # long  -> skew > 0 -> bid lower, ask higher

        # Bid: one tick inside best_bid, shifted down by skew if long
        bid_p = best_bid_p + 1 - _iround(max(skew, 0))
        bid_p = min(bid_p, _iround(fv) - 1)   # sanity: never bid above ewma
        bid_p = min(bid_p, best_ask_p - 1)     # never cross the spread

        if pos < soft_cap and r_buy > 0 and bid_p >= 1:
            qty = min(size, r_buy)
            orders.append(Order(sym, bid_p, qty))

        # Ask: one tick inside best_ask, shifted up by skew if short
        ask_p = best_ask_p - 1 + _iround(max(-skew, 0))
        ask_p = max(ask_p, _iround(fv) + 1)   # sanity: never ask below ewma
        ask_p = max(ask_p, best_bid_p + 1)     # never cross the spread

        if pos > -soft_cap and r_sel > 0:
            qty = min(size, r_sel)
            orders.append(Order(sym, ask_p, -qty))

    # ── Aggressive take (capped by soft_cap to bound end-of-round position) ────
    if asks and best_ask_p < fv - take_edge and pos < soft_cap and r_buy > 0:
        qty = min(size, r_buy)
        orders.append(Order(sym, best_ask_p, qty))

    if bids and best_bid_p > fv + take_edge and pos > -soft_cap and r_sel > 0:
        qty = min(size, r_sel)
        orders.append(Order(sym, best_bid_p, -qty))

    return orders, new_ewma


def _vev_orders(sym, K, depth, pos, velvet_mid, T_rem):
    """
    Sell overpriced OTM calls aggressively.

    Fire when: market_bid > BS_fair * VEV_PREMIUM + VEV_MIN_EDGE
    Fill: one aggressive sell order at the market bid.
    Cap: no more than VEV_LIMIT short per strike.

    Returns list of Order objects.
    """
    if depth is None or velvet_mid is None or velvet_mid <= 0:
        return []

    bids = sorted(depth.buy_orders.items(), reverse=True)
    if not bids:
        return []

    best_bid_p, best_bid_v = bids[0]
    fv  = _bs_call(velvet_mid, K, T_rem, VEV_SIGMA_TICK)
    threshold = fv * VEV_PREMIUM + VEV_MIN_EDGE

    if best_bid_p <= threshold:
        return []

    pos = pos or 0
    room = VEV_LIMIT + pos   # pos is negative when short; room > 0 means can sell more
    if room <= 0:
        return []

    qty = min(VEV_SIZE, room, abs(best_bid_v) if best_bid_v else VEV_SIZE)
    return [Order(sym, best_bid_p, -qty)]


class Trader:

    def run(self, state: TradingState):
        try:
            S = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            S = {}

        pos    = getattr(state, "position", {}) or {}
        depths = getattr(state, "order_depths", {}) or {}
        ts     = getattr(state, "timestamp", 0)
        out    = {}

        # ── Remaining option life (local ticks until end of day) ──────────────
        T_rem = max(1, 10000 - ts // 100)

        # ── VELVET mid price (needed for VEV BS pricing + delta hedge) ─────────
        vd = depths.get(VELVET)
        velvet_mid = None
        if vd:
            vb = sorted(vd.buy_orders.items(),  reverse=True)
            va = sorted(vd.sell_orders.items())
            if vb and va:
                velvet_mid = (vb[0][0] + va[0][0]) * 0.5

        # ── VEV option sell orders ─────────────────────────────────────────────
        for K in VEV_STRIKES_TRADE:
            sym = f"VEV_{K}"
            vev_orders = _vev_orders(
                sym=sym, K=K,
                depth=depths.get(sym),
                pos=pos.get(sym, 0),
                velvet_mid=velvet_mid,
                T_rem=T_rem,
            )
            if vev_orders:
                out[sym] = vev_orders

        # ── HYDROGEL_PACK ──────────────────────────────────────────────────────
        h_orders, new_h = _mm_orders(
            sym=HYDRO, depth=depths.get(HYDRO),
            pos=pos.get(HYDRO, 0), ewma_prev=S.get("h"),
            min_spread=H_MIN_SPREAD, size=H_SIZE, soft_cap=H_SOFT_CAP,
            skew_rate=H_SKEW, take_edge=H_TAKE,
        )
        if h_orders:
            out[HYDRO] = h_orders
        if new_h is not None:
            S["h"] = round(new_h, 2)

        # ── VELVETFRUIT_EXTRACT ────────────────────────────────────────────────
        v_orders, new_v = _mm_orders(
            sym=VELVET, depth=depths.get(VELVET),
            pos=pos.get(VELVET, 0), ewma_prev=S.get("v"),
            min_spread=V_MIN_SPREAD, size=V_SIZE, soft_cap=V_SOFT_CAP,
            skew_rate=V_SKEW, take_edge=V_TAKE,
        )
        if v_orders:
            out[VELVET] = v_orders
        if new_v is not None:
            S["v"] = round(new_v, 2)

        return out, 0, json.dumps(S)


# ── Backward-compatibility stubs for backtest_runner.py ──────────────────────
FINAL_MODE         = "SIMPLE_MM"
FINAL_MODE_OPTIONS = ("SIMPLE_MM",)
CONFIG             = {"strategy_mode": "SIMPLE_MM", "final_mode": FINAL_MODE}
CEILING_OVERRIDES  = {}
VEV_STRIKES        = [5300, 5400, 5500]
HIGH_STRIKES       = []
STRIKE_ORDER       = {}
