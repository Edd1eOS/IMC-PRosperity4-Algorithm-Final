"""
trader_submission.py - Single-file submission for IMC Prosperity Round 4.

Architecture:
  - Phase-aware chooser option trading
  - Counterparty-aware VELVET directional overlay
  - Conservative HYDROGEL market making

Execution order each tick:
  1. Update state / profiles / phase detector
  2. HYDROGEL_PACK (stable MM)
  3. VELVETFRUIT_EXTRACT (FV + alpha)
  4. High-priority VEV strikes (aggressive take if PRE_CHOICE floor >> ask)
  5. Remaining VEV strikes (passive)
  6. Delta hedge via VELVET

traderData format: compact JSON-like string, max ~1000 chars.
"""

import json
import math
from typing import Dict, List

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG - all magic numbers here, parameterized for ablation
# ─────────────────────────────────────────────────────────────────────────────
CONFIG = {
    # ── Product limits (hard) ──────────────────────────────────────────────
    "limits": {
        "HYDROGEL_PACK": 50,
        "VELVETFRUIT_EXTRACT": 20,
        "VEV_4000": 300, "VEV_4500": 300, "VEV_5000": 300,
        "VEV_5100": 300, "VEV_5200": 300, "VEV_5300": 300,
        "VEV_5400": 300, "VEV_5500": 300, "VEV_6000": 300, "VEV_6500": 300,
    },

    # ── Phase ──────────────────────────────────────────────────────────────
    "phase_override": None,           # None = auto-detect; or "PRE_CHOICE" etc.
    "phase_confidence_threshold": 0.65,
    "default_phase": "PRE_CHOICE",

    # ── Option model parameters ────────────────────────────────────────────
    "total_expiry_days":  7.0,        # PARAM: unknown, ablate [3, 5, 7, 10, 14]
    "choice_time_days":   2.0,        # PARAM: unknown, ablate [0.5, 1, 2, 3, 5]
    "sigma":              0.30,       # PARAM: from ATM IV, ablate [0.20, 0.30, 0.40]
    "rate":               0.0,

    # ── Execution ──────────────────────────────────────────────────────────
    "take_edge_min":      2.0,        # min edge to aggressively take from book
    "intercept_min_edge": 1.0,        # min edge to intercept market trades
    "high_strike_take_mult": 1.0,     # multiply take threshold for K >= 5300
    "max_hedge_slippage": 3,          # max price vs FV for VELVET hedge
    "hedge_delta_threshold": 15.0,    # only hedge if |delta_imbalance| > this
    "velvet_mm_size":     0,          # 0 = no VELVET market making (MM loses vs informed CPs)

    # ── Quote spreads ──────────────────────────────────────────────────────
    "hydrogel_half_spread":  4,
    "velvet_half_spread":    3,
    "vev_half_spread":       1,

    # ── Counterparty ──────────────────────────────────────────────────────
    "counterparty_threshold":  0.6,   # min signal strength to use CP alpha
    "cp_min_samples":          5,
    "alpha_scale":             2.0,

    # ── EWMA half-lives (ticks) ────────────────────────────────────────────
    "hydrogel_ewma_hl":   20,
    "velvet_ewma_hl":     30,
    "velvet_micro_hl":    10,
    "vev_mid_ewma_hl":    20,
    "alpha_ewma_hl":      15,

    # ── Risk profile ──────────────────────────────────────────────────────
    "risk_profile": "balanced",
    "max_per_strike": 150,
    "max_total_delta": 100,

    # ── Chooser floor taking ───────────────────────────────────────────────
    # In PRE_CHOICE, if market_ask < chooser_floor - floor_min_edge -> take
    "chooser_floor_min_edge": 1.0,

    # ── Strike selection for PRE_CHOICE ───────────────────────────────────
    # Deep ITM options (4000, 4500) have no chooser premium - skip them in PRE_CHOICE
    # They cost 1200+/750+ per unit and provide zero additional chooser value.
    "skip_deep_itm_pre_choice": True,   # skip cheap-edge strikes in PRE_CHOICE
    "skip_itm_threshold": 5200,          # skip strikes <= this in PRE_CHOICE (115x RR at 5300+)

    # ── One-sided in PRE_CHOICE ────────────────────────────────────────────
    # Never sell VEV options in PRE_CHOICE: chooser floor provides asymmetric floor.
    # Even if model thinks price is above FV (e.g., wrong sigma), selling exposes us
    # to unlimited put component risk. Conservative: only buy in PRE_CHOICE.
    "no_sell_vev_pre_choice": True,

    # ── Recovery Stable v2 risk controls ──────────────────────────────────
    "velvet_stable_cap": 0,
    "velvet_alpha_gate": 1.5,
    "velvet_alpha_cap": 10,
    "enable_velvet_alpha": False,
    "hydrogel_normal_cap": 30,
    "hydrogel_profit_lock_cap": 20,
    "hydrogel_tight_cap": 10,
    "hydrogel_aggressive_take": False,
    "pre_choice_caps": {
        "VEV_4000": 0,
        "VEV_4500": 0,
        "VEV_5000": 0,
        "VEV_5100": 0,
        "VEV_5200": 0,
        "VEV_5300": 0,
        "VEV_5400": 0,
        "VEV_5500": 0,
        "VEV_6000": 300,
        "VEV_6500": 300,
    },
    "pre_choice_max_ask": {
        "VEV_5300": None,
        "VEV_5400": None,
        "VEV_5500": None,
        "VEV_6000": 1,
        "VEV_6500": 1,
    },
    "max_order_per_tick": {
        "VEV_6000": 10,
        "VEV_6500": 10,
        "VEV_5500": 10,
        "VELVETFRUIT_EXTRACT": 10,
        "HYDROGEL_PACK": 5,
    },
    "portfolio_profit_lock": 500.0,
    "portfolio_tight_lock": 800.0,
    "portfolio_drawdown_limit": 400.0,
    "hydro_drawdown_limit": 250.0,
    "endgame_ts": 850000,
    "final_flat_ts": 950000,
    "enable_postchoice_delta_hedge": False,

    # ── Version flag ──────────────────────────────────────────────────────
    "version": "stable",              # "stable" or "ceiling"
}

# ── Ceiling version overrides ─────────────────────────────────────────────────
CEILING_OVERRIDES = {
    "take_edge_min":         0.5,
    "intercept_min_edge":    0.5,
    "high_strike_take_mult": 0.5,    # more aggressive on high strikes
    "chooser_floor_min_edge": 0.5,
    "max_per_strike":        250,
    "max_total_delta":       180,
    "hedge_delta_threshold": 30.0,
    "velvet_mm_size":        1,      # small MM in ceiling for CP alpha capture
    "skip_itm_threshold":    4600,   # ceiling: also take 5000-5200 for their chooser premium
    "risk_profile":          "aggressive",
    "version":               "ceiling",
}

VEV_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
HIGH_STRIKES = [5300, 5400, 5500, 6000, 6500]

# Strike execution order by phase
STRIKE_ORDER = {
    "PRE_CHOICE":       [5300, 5400, 5500, 6000, 6500, 5200, 5100, 5000, 4500, 4000],
    "POST_CHOICE_CALL": [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500],
    "POST_CHOICE_PUT":  [6500, 6000, 5500, 5400, 5300, 5200, 5100, 5000, 4500, 4000],
    "AT_CHOICE":        [5200, 5100, 5300, 5000, 5400, 4500, 5500, 4000, 6000, 6500],
}

# ─────────────────────────────────────────────────────────────────────────────
# Math utilities (pure Python, no numpy/scipy)
# ─────────────────────────────────────────────────────────────────────────────

def _ncdf(x):
    t = 1.0/(1.0 + 0.2316419*abs(x))
    p = t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
    c = 1.0 - (1.0/math.sqrt(2*math.pi))*math.exp(-0.5*x*x)*p
    return c if x >= 0 else 1.0-c

def _bs_call(S, K, T, sig, r=0.0):
    if T <= 0 or sig <= 0 or S <= 0 or K <= 0: return max(S-K, 0.0)
    d1 = (math.log(S/K)+(r+0.5*sig*sig)*T)/(sig*math.sqrt(T))
    return S*_ncdf(d1)-K*math.exp(-r*T)*_ncdf(d1-sig*math.sqrt(T))

def _bs_put(S, K, T, sig, r=0.0):
    if T <= 0 or sig <= 0 or S <= 0 or K <= 0: return max(K-S, 0.0)
    d1 = (math.log(S/K)+(r+0.5*sig*sig)*T)/(sig*math.sqrt(T))
    d2 = d1-sig*math.sqrt(T)
    return K*math.exp(-r*T)*_ncdf(-d2)-S*_ncdf(-d1)

def _bs_call_delta(S, K, T, sig, r=0.0):
    if T <= 0 or sig <= 0 or S <= 0: return 1.0 if S > K else 0.0
    d1 = (math.log(S/K)+(r+0.5*sig*sig)*T)/(sig*math.sqrt(T))
    return _ncdf(d1)

def _bs_put_delta(S, K, T, sig, r=0.0):
    if T <= 0 or sig <= 0 or S <= 0: return -1.0 if K > S else 0.0
    d1 = (math.log(S/K)+(r+0.5*sig*sig)*T)/(sig*math.sqrt(T))
    return _ncdf(d1)-1.0

def _chooser_fv(S, K, T, tc, sig, r=0.0):
    """Chooser = call(T) + put(tc). Never below intrinsic floor."""
    fv = _bs_call(S, K, T, sig, r) + _bs_put(S, K, tc, sig, r)
    floor = max(max(S-K,0), max(K-S,0))
    return max(fv, floor)

def _chooser_delta(S, K, T, tc, sig, r=0.0):
    return _bs_call_delta(S, K, T, sig, r) + _bs_put_delta(S, K, tc, sig, r)

def _ewma_update(prev, val, alpha):
    if prev is None: return val
    return alpha*val + (1-alpha)*prev

def _ewma_alpha(halflife):
    return 1 - math.exp(-math.log(2)/max(halflife, 1))

# ─────────────────────────────────────────────────────────────────────────────
# Compact state (serialized to traderData)
# ─────────────────────────────────────────────────────────────────────────────

class State:
    """
    Minimal state that must survive between ticks.
    Deliberately small to avoid traderData bloat.
    """
    __slots__ = [
        "hydro_ewma", "velvet_ewma", "velvet_micro",
        "vev_ewma",       # dict: strike -> ewma
        "alpha_ewma",     # EWMA of CP directional signal for VELVET
        "cp_buy_n",       # dict: cp -> n trades bought VELVET
        "cp_buy_pos",     # dict: cp -> n trades where buyer was right (for VELVET)
        "cp_sell_n",
        "cp_sell_pos",
        "phase",          # current phase string
        "phase_conf",     # phase confidence
        "stuck_count",    # dict: strike -> ticks stuck at 0.5
        "cash_by_symbol", # conservative realized cash proxy
        "last_pos",       # last known positions to infer fills when own_trades unavailable
        "peak_pnl",       # portfolio peak pnl proxy
        "hydro_peak_pnl", # HYDROGEL-specific peak pnl proxy
        "mode",           # NORMAL / PROFIT_LOCK / RISK_OFF / ENDGAME_FLAT
        "ts",             # last timestamp
    ]

    def __init__(self):
        self.hydro_ewma   = None
        self.velvet_ewma  = None
        self.velvet_micro = None
        self.vev_ewma     = {}
        self.alpha_ewma   = None
        self.cp_buy_n     = {}
        self.cp_buy_pos   = {}
        self.cp_sell_n    = {}
        self.cp_sell_pos  = {}
        self.phase        = "PRE_CHOICE"
        self.phase_conf   = 0.5
        self.stuck_count  = {k: 0 for k in [5500, 6000, 6500]}
        self.cash_by_symbol = {}
        self.last_pos = {}
        self.peak_pnl = 0.0
        self.hydro_peak_pnl = 0.0
        self.mode = "NORMAL"
        self.ts           = 0

    def to_json(self):
        return json.dumps({
            "he": self.hydro_ewma,
            "ve": self.velvet_ewma,
            "vm": self.velvet_micro,
            "vv": self.vev_ewma,
            "ae": self.alpha_ewma,
            "bn": self.cp_buy_n,
            "bp": self.cp_buy_pos,
            "sn": self.cp_sell_n,
            "sp": self.cp_sell_pos,
            "ph": self.phase,
            "pc": round(self.phase_conf, 3),
            "sc": self.stuck_count,
            "cb": self.cash_by_symbol,
            "lp": self.last_pos,
            "pp": round(self.peak_pnl, 2),
            "hp": round(self.hydro_peak_pnl, 2),
            "md": self.mode,
            "ts": self.ts,
        }, separators=(',', ':'))

    @classmethod
    def from_json(cls, s):
        obj = cls()
        if not s:
            return obj
        try:
            d = json.loads(s)
            obj.hydro_ewma   = d.get("he")
            obj.velvet_ewma  = d.get("ve")
            obj.velvet_micro = d.get("vm")
            obj.vev_ewma     = {int(k): v for k, v in (d.get("vv") or {}).items()}
            obj.alpha_ewma   = d.get("ae")
            obj.cp_buy_n     = d.get("bn") or {}
            obj.cp_buy_pos   = d.get("bp") or {}
            obj.cp_sell_n    = d.get("sn") or {}
            obj.cp_sell_pos  = d.get("sp") or {}
            obj.phase        = d.get("ph", "PRE_CHOICE")
            obj.phase_conf   = d.get("pc", 0.5)
            obj.stuck_count  = {int(k): v for k, v in (d.get("sc") or {}).items()}
            obj.cash_by_symbol = d.get("cb") or {}
            obj.last_pos = {k: int(v) for k, v in (d.get("lp") or {}).items()}
            obj.peak_pnl = d.get("pp", 0.0)
            obj.hydro_peak_pnl = d.get("hp", 0.0)
            obj.mode = d.get("md", "NORMAL")
            obj.ts           = d.get("ts", 0)
        except Exception:
            pass  # corrupt state -> fresh start
        return obj


# ─────────────────────────────────────────────────────────────────────────────
# Order helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_book(order_depth):
    """Returns (bids_desc, asks_asc) as [(price,vol), ...]"""
    bids = sorted([(p, abs(v)) for p, v in order_depth.buy_orders.items()],  reverse=True)
    asks = sorted([(p, abs(v)) for p, v in order_depth.sell_orders.items()])
    return bids, asks

def _mid(order_depth):
    bids, asks = _get_book(order_depth)
    bb = bids[0][0] if bids else None
    ba = asks[0][0] if asks else None
    if bb is not None and ba is not None:
        return (bb + ba) / 2.0
    if bb is not None:
        return bb
    return ba

def _microprice(order_depth):
    bids, asks = _get_book(order_depth)
    if not bids or not asks: return _mid(order_depth)
    bp, bv = bids[0]; ap, av = asks[0]
    return (bp*av + ap*bv)/(bv+av) if bv+av > 0 else (bp+ap)/2.0

def _clip(qty, sym, positions, limits):
    pos = positions.get(sym, 0)
    lim = limits.get(sym, 300)
    if qty > 0: return min(qty, lim - pos)
    else:       return max(qty, -lim - pos)

def _add_order(orders, sym, price, qty):
    if qty != 0:
        orders.setdefault(sym, []).append(Order(sym, int(price), qty))


# ─────────────────────────────────────────────────────────────────────────────
# Main Trader class
# ─────────────────────────────────────────────────────────────────────────────

# Import the datamodel (provided by IMC competition environment)
try:
    from datamodel import OrderDepth, UserId, TradingState, Order
except ImportError:
    # Fallback for local testing
    from collections import namedtuple
    Order = namedtuple("Order", ["symbol", "price", "quantity"])
    class TradingState:
        def __init__(self):
            self.timestamp = 0; self.listings = {}; self.order_depths = {}
            self.own_trades = {}; self.market_trades = {}; self.position = {}
            self.observations = None; self.traderData = ""


class Trader:
    """
    Round 4 Trader. Phase-aware chooser + counterparty alpha + stable HYDROGEL MM.
    """

    def __init__(self):
        self._cfg = CONFIG

    def _best_bid_ask(self, order_depth):
        if order_depth is None:
            return None, None
        bids, asks = _get_book(order_depth)
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        return best_bid, best_ask

    def _update_cash_proxy(self, S, state, depths, pos):
        own_trades = getattr(state, "own_trades", {}) or {}
        own_trade_symbols = set()

        for sym, trades in own_trades.items():
            for trade in trades:
                trade_ts = getattr(trade, "timestamp", state.timestamp)
                if trade_ts != state.timestamp:
                    continue
                buyer = getattr(trade, "buyer", None)
                seller = getattr(trade, "seller", None)
                price = getattr(trade, "price", None)
                qty = getattr(trade, "quantity", 0)
                if price is None or qty == 0:
                    continue
                if buyer == "SUBMISSION":
                    S.cash_by_symbol[sym] = S.cash_by_symbol.get(sym, 0.0) - price * qty
                    own_trade_symbols.add(sym)
                elif seller == "SUBMISSION":
                    S.cash_by_symbol[sym] = S.cash_by_symbol.get(sym, 0.0) + price * qty
                    own_trade_symbols.add(sym)

        symbols = (set(S.last_pos) | set(pos)) - own_trade_symbols
        for sym in symbols:
            prev = S.last_pos.get(sym, 0)
            cur = pos.get(sym, 0)
            delta = cur - prev
            if delta == 0:
                continue
            best_bid, best_ask = self._best_bid_ask(depths.get(sym))
            if delta > 0:
                price = best_ask if best_ask is not None else (_mid(depths.get(sym)) or 0.0)
                S.cash_by_symbol[sym] = S.cash_by_symbol.get(sym, 0.0) - price * delta
            else:
                price = best_bid if best_bid is not None else (_mid(depths.get(sym)) or 0.0)
                S.cash_by_symbol[sym] = S.cash_by_symbol.get(sym, 0.0) + price * abs(delta)

    def _mark_position(self, sym, pos, order_depth):
        if pos == 0:
            return 0.0
        best_bid, best_ask = self._best_bid_ask(order_depth)
        if pos > 0:
            if best_bid is not None:
                return pos * best_bid
            return 0.0 if sym.startswith("VEV_") else pos * (_mid(order_depth) or 0.0)
        if best_ask is not None:
            return pos * best_ask
        return 0.0

    def _compute_risk_snapshot(self, S, pos, depths):
        total = 0.0
        by_symbol = {}
        for sym, qty in pos.items():
            mark = self._mark_position(sym, qty, depths.get(sym))
            pnl = S.cash_by_symbol.get(sym, 0.0) + mark
            by_symbol[sym] = pnl
            total += pnl
        hydro_pnl = by_symbol.get("HYDROGEL_PACK", 0.0)
        S.peak_pnl = max(S.peak_pnl, total)
        S.hydro_peak_pnl = max(S.hydro_peak_pnl, hydro_pnl)
        effective_peak = S.peak_pnl if S.peak_pnl > 0 else total
        hydro_effective_peak = S.hydro_peak_pnl if S.hydro_peak_pnl > 0 else hydro_pnl
        return {
            "portfolio_pnl": total,
            "portfolio_peak": effective_peak,
            "portfolio_drawdown": effective_peak - total,
            "hydro_pnl": hydro_pnl,
            "hydro_peak": hydro_effective_peak,
            "hydro_drawdown": hydro_effective_peak - hydro_pnl,
            "by_symbol": by_symbol,
        }

    def _risk_mode(self, ts, risk, cfg):
        if ts > cfg["final_flat_ts"]:
            return "ENDGAME_FLAT"
        if risk["portfolio_drawdown"] > cfg["portfolio_drawdown_limit"]:
            return "RISK_OFF"
        if ts > cfg["endgame_ts"] or risk["portfolio_pnl"] > cfg["portfolio_profit_lock"]:
            return "PROFIT_LOCK"
        return "NORMAL"

    def _hydrogel_cap(self, ts, pos, risk, mode, cfg):
        cap = cfg["hydrogel_normal_cap"]
        if risk["portfolio_pnl"] > cfg["portfolio_profit_lock"]:
            cap = min(cap, cfg["hydrogel_profit_lock_cap"])
        if risk["portfolio_pnl"] > cfg["portfolio_tight_lock"]:
            cap = min(cap, cfg["hydrogel_tight_cap"])
        if ts > cfg["endgame_ts"]:
            cap = min(cap, cfg["hydrogel_tight_cap"])
        if ts > cfg["final_flat_ts"]:
            cap = 0
        if risk["hydro_drawdown"] > cfg["hydro_drawdown_limit"]:
            cap = min(cap, cfg["hydrogel_tight_cap"])
        if mode == "RISK_OFF":
            cap = min(cap, abs(pos))
        return max(0, int(cap))

    def _velvet_cap(self, ts, alpha_signal, mode, cfg):
        if not cfg.get("enable_velvet_alpha", False):
            return 0
        if mode != "NORMAL":
            return 0
        if ts >= 75000:
            return 0
        if abs(alpha_signal) <= cfg.get("velvet_alpha_gate", 1.5):
            return cfg.get("velvet_stable_cap", 0)
        return cfg.get("velvet_alpha_cap", 10)

    def _symbol_cap(self, sym, phase, cfg, limits):
        cap = limits.get(sym, 300)
        if phase == "PRE_CHOICE" and sym.startswith("VEV_"):
            cap = min(cap, cfg.get("pre_choice_caps", {}).get(sym, 0))
        return max(0, int(cap))

    def _safe_add_order(self, orders_out, sym, price, qty, state_pos, sent_qty, phase, cfg, limits):
        if qty == 0:
            return 0

        cap = self._symbol_cap(sym, phase, cfg, limits)
        cur = state_pos.get(sym, 0)

        if qty > 0:
            qty = min(int(qty), cap - cur)
        else:
            qty = max(int(qty), -cap - cur)
        if qty == 0:
            return 0

        per_tick = cfg.get("max_order_per_tick", {}).get(sym)
        if per_tick is not None:
            used = sent_qty.get(sym, 0)
            room = int(per_tick) - used
            if room <= 0:
                return 0
            if qty > 0:
                qty = min(qty, room)
            else:
                qty = max(qty, -room)
            if qty == 0:
                return 0

        _add_order(orders_out, sym, price, qty)
        state_pos[sym] = cur + qty
        sent_qty[sym] = sent_qty.get(sym, 0) + abs(qty)
        return qty

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        cfg    = self._cfg
        limits = cfg["limits"]
        ts     = state.timestamp
        pos    = state.position          # {symbol: int}
        state_pos = dict(pos)
        depths = state.order_depths      # {symbol: OrderDepth}
        mtrades = state.market_trades    # {symbol: list[Trade]}

        # ── 1. Load / update persistent state ──────────────────────────────
        S = State.from_json(state.traderData)
        S.ts = ts
        self._update_cash_proxy(S, state, depths, pos)
        a_hy  = _ewma_alpha(cfg["hydrogel_ewma_hl"])
        a_ve  = _ewma_alpha(cfg["velvet_ewma_hl"])
        a_vm  = _ewma_alpha(cfg["velvet_micro_hl"])
        a_vev = _ewma_alpha(cfg["vev_mid_ewma_hl"])
        a_alp = _ewma_alpha(cfg["alpha_ewma_hl"])

        orders_out = {}
        sent_qty = {}
        logs = []

        # ── Get current mids ──────────────────────────────────────────────
        def get_mid(sym):
            od = depths.get(sym)
            return _mid(od) if od else None

        def get_micro(sym):
            od = depths.get(sym)
            return _microprice(od) if od else None

        # ── Update VELVET EWMA ────────────────────────────────────────────
        vm = get_micro("VELVETFRUIT_EXTRACT")
        if vm is not None:
            S.velvet_micro = _ewma_update(S.velvet_micro, vm, a_vm)
        vmid = get_mid("VELVETFRUIT_EXTRACT")
        if vmid is not None:
            S.velvet_ewma  = _ewma_update(S.velvet_ewma, vmid, a_ve)
        velvet_fv = (0.6*(S.velvet_micro or vmid or 5248)
                     + 0.4*(S.velvet_ewma  or vmid or 5248))

        # ── Update HYDROGEL EWMA ──────────────────────────────────────────
        hm = get_micro("HYDROGEL_PACK")
        if hm is not None:
            S.hydro_ewma = _ewma_update(S.hydro_ewma, hm, a_hy)
        hydro_fv = S.hydro_ewma or get_mid("HYDROGEL_PACK") or 9994

        # ── Update VEV EWMAs ──────────────────────────────────────────────
        for K in VEV_STRIKES:
            m = get_mid(f"VEV_{K}")
            if m is not None:
                S.vev_ewma[K] = _ewma_update(S.vev_ewma.get(K), m, a_vev)

        # ── Counterparty alpha from VELVET market trades ──────────────────
        S = self._update_cp_state(S, mtrades, velvet_fv, a_alp)
        alpha_signal = (S.alpha_ewma or 0.0) * cfg["alpha_scale"]

        # ── Phase detection ───────────────────────────────────────────────
        if not cfg.get("phase_override"):
            S = self._update_phase(S, depths, mtrades)
        phase = cfg.get("phase_override") or S.phase

        risk = self._compute_risk_snapshot(S, pos, depths)
        mode = self._risk_mode(ts, risk, cfg)
        S.mode = mode
        hydro_cap = self._hydrogel_cap(ts, pos.get("HYDROGEL_PACK", 0), risk, mode, cfg)
        velvet_cap = self._velvet_cap(ts, alpha_signal, mode, cfg)

        logs.append(f"ts={ts} ph={phase[:3]} md={mode[:4]} p={risk['portfolio_pnl']:.0f}")

        # ── Compute option params ─────────────────────────────────────────
        T   = cfg["total_expiry_days"]  / 365.0
        tc  = cfg["choice_time_days"]   / 365.0
        sig = cfg["sigma"]
        r   = cfg["rate"]

        # ── 2. HYDROGEL_PACK ─────────────────────────────────────────────
        if "HYDROGEL_PACK" in depths:
            self._trade_hydrogel(
                depths["HYDROGEL_PACK"], hydro_fv,
                state_pos.get("HYDROGEL_PACK", 0), limits, orders_out, cfg,
                phase, state_pos, sent_qty, risk, mode, hydro_cap
            )

        # ── 3. VELVETFRUIT_EXTRACT ────────────────────────────────────────
        if "VELVETFRUIT_EXTRACT" in depths:
            self._trade_velvet(
                depths["VELVETFRUIT_EXTRACT"], velvet_fv, alpha_signal,
                mtrades.get("VELVETFRUIT_EXTRACT", []),
                state_pos.get("VELVETFRUIT_EXTRACT", 0), limits, orders_out, cfg,
                phase, state_pos, sent_qty, mode, velvet_cap
            )

        # ── 4 & 5. VEV strikes ────────────────────────────────────────────
        ordered = STRIKE_ORDER.get(phase, STRIKE_ORDER["PRE_CHOICE"])
        S = self._trade_vev_all(
            ordered, depths, mtrades, state_pos, velvet_fv,
            T, tc, sig, r, phase, limits, orders_out, cfg, S, logs, sent_qty, risk, mode, ts
        )

        # ── 6. Delta hedge via VELVET ─────────────────────────────────────
        self._delta_hedge(
            depths.get("VELVETFRUIT_EXTRACT"), velvet_fv,
            state_pos, orders_out, limits, cfg, T, tc, sig, r, phase, sent_qty
        )

        S.last_pos = {sym: int(qty) for sym, qty in pos.items() if qty != 0}

        traderData = S.to_json()
        log_str = " | ".join(logs[:4])  # cap log length
        if ts % 5000 == 0 and log_str:
            print(log_str[:160])

        return orders_out, 0, traderData

    # ── Update counterparty state ─────────────────────────────────────────────

    def _update_cp_state(self, S, mtrades, velvet_fv, a_alp):
        cfg = self._cfg
        threshold = cfg["counterparty_threshold"]
        min_samples = cfg["cp_min_samples"]

        velvet_trades = mtrades.get("VELVETFRUIT_EXTRACT", [])
        raw_alpha = 0.0
        n_signals = 0

        for t in velvet_trades:
            buyer  = t.buyer  if hasattr(t, 'buyer')  else t.get('buyer','')
            seller = t.seller if hasattr(t, 'seller') else t.get('seller','')
            price  = t.price  if hasattr(t, 'price')  else t.get('price', velvet_fv)
            qty    = t.quantity if hasattr(t, 'quantity') else t.get('quantity', 0)

            # Update buy stats for buyer
            S.cp_buy_n[buyer]  = S.cp_buy_n.get(buyer, 0)  + qty
            # Assume buyer bought below mid -> positive alpha indicator
            if price < velvet_fv:
                S.cp_buy_pos[buyer] = S.cp_buy_pos.get(buyer, 0) + qty

            # Update sell stats for seller
            S.cp_sell_n[seller]  = S.cp_sell_n.get(seller, 0) + qty
            if price > velvet_fv:
                S.cp_sell_pos[seller] = S.cp_sell_pos.get(seller, 0) + qty

            # Compute alpha from this trade
            for cp, side in [(buyer, 1), (seller, -1)]:
                n_key = "cp_buy_n" if side == 1 else "cp_sell_n"
                p_key = "cp_buy_pos" if side == 1 else "cp_sell_pos"
                ns = getattr(S, n_key)
                ps = getattr(S, p_key)
                n_cp = ns.get(cp, 0)
                if n_cp >= min_samples:
                    pos_rate = ps.get(cp, 0) / n_cp
                    # Confidence scaling
                    conf = min(1.0, n_cp / 20.0)
                    if conf >= threshold:
                        raw_alpha += side * (pos_rate - 0.5) * 2.0 * conf
                        n_signals += 1

        if n_signals > 0:
            S.alpha_ewma = _ewma_update(S.alpha_ewma, raw_alpha / n_signals, a_alp)

        return S

    # ── Phase update ──────────────────────────────────────────────────────────

    def _update_phase(self, S, depths, mtrades):
        """Update stuck counts and detect potential phase changes."""
        for K in [5500, 6000, 6500]:
            sym = f"VEV_{K}"
            od = depths.get(sym)
            if od is None:
                continue
            m = _mid(od)
            if m is None:
                continue
            key = str(K)
            if m == 0.5:
                S.stuck_count[K] = S.stuck_count.get(K, 0) + 1
            else:
                # Price moved from 0.5 -> possible phase change
                if S.stuck_count.get(K, 0) > 20 and m > 5.0:
                    # Jump from stuck-at-zero: could be POST_CHOICE_PUT
                    if S.phase == "PRE_CHOICE":
                        S.phase = "AT_CHOICE"
                        S.phase_conf = 0.6
                S.stuck_count[K] = 0

        return S

    # ── HYDROGEL trading ──────────────────────────────────────────────────────

    def _trade_hydrogel(self, od, fv, pos, limits, orders_out, cfg, phase, state_pos, sent_qty, risk, mode, hydro_cap):
        bids, asks = _get_book(od)
        hs = cfg["hydrogel_half_spread"]
        lim = max(1, hydro_cap)
        pos_frac = pos / lim
        skew = -pos_frac * hs

        if mode == "ENDGAME_FLAT":
            if pos > 0 and bids:
                best_bid, best_bv = bids[0]
                self._safe_add_order(orders_out, "HYDROGEL_PACK", best_bid, -min(best_bv, pos), state_pos, sent_qty, phase, cfg, {**limits, "HYDROGEL_PACK": hydro_cap})
            elif pos < 0 and asks:
                best_ask, best_av = asks[0]
                self._safe_add_order(orders_out, "HYDROGEL_PACK", best_ask, min(best_av, abs(pos)), state_pos, sent_qty, phase, cfg, {**limits, "HYDROGEL_PACK": hydro_cap})
            return

        if mode == "RISK_OFF":
            if pos > 0 and bids:
                best_bid, best_bv = bids[0]
                self._safe_add_order(orders_out, "HYDROGEL_PACK", best_bid, -min(best_bv, max(1, pos)), state_pos, sent_qty, phase, cfg, {**limits, "HYDROGEL_PACK": hydro_cap})
            elif pos < 0 and asks:
                best_ask, best_av = asks[0]
                self._safe_add_order(orders_out, "HYDROGEL_PACK", best_ask, min(best_av, max(1, abs(pos))), state_pos, sent_qty, phase, cfg, {**limits, "HYDROGEL_PACK": hydro_cap})
            return

        # Passive quotes
        bid_q = round(fv - hs + skew)
        ask_q = round(fv + hs + skew)
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        if best_bid is not None and bid_q > best_bid:
            bid_q = best_bid
        if best_ask is not None and ask_q < best_ask:
            ask_q = best_ask
        if best_ask is not None and bid_q >= best_ask:
            bid_q = best_ask - 1
        if best_bid is not None and ask_q <= best_bid:
            ask_q = best_bid + 1
        local_limits = {**limits, "HYDROGEL_PACK": hydro_cap}
        bid_sz = _clip(max(0, min(10, hydro_cap - pos)), "HYDROGEL_PACK", {"HYDROGEL_PACK": pos}, local_limits)
        ask_sz = _clip(-max(0, min(10, hydro_cap + pos)), "HYDROGEL_PACK", {"HYDROGEL_PACK": pos}, local_limits)

        if bid_sz > 0:
            self._safe_add_order(orders_out, "HYDROGEL_PACK", bid_q, bid_sz, state_pos, sent_qty, phase, cfg, local_limits)
        if ask_sz < 0:
            self._safe_add_order(orders_out, "HYDROGEL_PACK", ask_q, ask_sz, state_pos, sent_qty, phase, cfg, local_limits)

        if not cfg.get("hydrogel_aggressive_take", False):
            return

        # Aggressive take: only for big mispricings (2x spread)
        take_edge = cfg["take_edge_min"] * 2
        if asks:
            best_ask, best_av = asks[0]
            if best_ask < fv - take_edge:
                qty = _clip(best_av, "HYDROGEL_PACK", {"HYDROGEL_PACK": pos}, local_limits)
                if qty > 0:
                    self._safe_add_order(orders_out, "HYDROGEL_PACK", best_ask, qty, state_pos, sent_qty, phase, cfg, local_limits)
        if bids:
            best_bid, best_bv = bids[0]
            if best_bid > fv + take_edge:
                qty = _clip(-best_bv, "HYDROGEL_PACK", {"HYDROGEL_PACK": pos}, local_limits)
                if qty < 0:
                    self._safe_add_order(orders_out, "HYDROGEL_PACK", best_bid, qty, state_pos, sent_qty, phase, cfg, local_limits)

    # ── VELVET trading ────────────────────────────────────────────────────────

    def _trade_velvet(self, od, fv, alpha, market_trades, pos, limits, orders_out, cfg, phase, state_pos, sent_qty, mode, velvet_cap):
        if velvet_cap <= 0:
            return

        bids, asks = _get_book(od)
        hs    = cfg["velvet_half_spread"]
        lim   = velvet_cap
        p_sym = {"VELVETFRUIT_EXTRACT": pos}
        local_limits = {**limits, "VELVETFRUIT_EXTRACT": velvet_cap}

        pos_frac = pos / lim
        skew = -pos_frac * hs * 1.5 + alpha * 0.3

        bid_q = round(fv - hs + skew)
        ask_q = round(fv + hs + skew)

        # Small quote sizes to avoid building up large VELVET inventory from MM alone
        # Larger sizes reserved for delta-hedge in _delta_hedge()
        mm_size = cfg.get("velvet_mm_size", 5)
        bid_sz = _clip(min(mm_size, lim - pos), "VELVETFRUIT_EXTRACT", p_sym, local_limits)
        ask_sz = _clip(-min(mm_size, lim + pos), "VELVETFRUIT_EXTRACT", p_sym, local_limits)

        if bid_sz > 0:
            self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", bid_q, bid_sz, state_pos, sent_qty, phase, cfg, local_limits)
        if ask_sz < 0:
            self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", ask_q, ask_sz, state_pos, sent_qty, phase, cfg, local_limits)

        # Directional take only on strong alpha signal (e.g., Mark 01 or 67 bought)
        take_e = cfg["take_edge_min"]
        if alpha > cfg["alpha_scale"] * 0.7 and asks:
            bp, bv = asks[0]
            if bp < fv + alpha - take_e:
                qty = _clip(min(bv, 10), "VELVETFRUIT_EXTRACT", p_sym, local_limits)
                if qty > 0:
                    self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", bp, qty, state_pos, sent_qty, phase, cfg, local_limits)
        if alpha < -cfg["alpha_scale"] * 0.7 and bids:
            bp, bv = bids[0]
            if bp > fv + alpha + take_e:
                qty = _clip(-min(bv, 10), "VELVETFRUIT_EXTRACT", p_sym, local_limits)
                if qty < 0:
                    self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", bp, qty, state_pos, sent_qty, phase, cfg, local_limits)

    # ── VEV trading (all strikes) ─────────────────────────────────────────────

    def _trade_vev_all(self, ordered, depths, mtrades, pos, spot,
                       T, tc, sig, r, phase, limits, orders_out, cfg, S, logs, sent_qty, risk, mode, ts):
        floor_min_edge = cfg["chooser_floor_min_edge"]
        take_edge = cfg["take_edge_min"]
        hs = cfg["vev_half_spread"]

        for K in ordered:
            sym = f"VEV_{K}"
            od = depths.get(sym)
            if od is None:
                continue

            if (phase == "PRE_CHOICE"
                    and cfg.get("skip_deep_itm_pre_choice", True)
                    and K <= cfg.get("skip_itm_threshold", 4600)):
                continue

            cap = self._symbol_cap(sym, phase, cfg, limits)
            if phase == "PRE_CHOICE" and cap <= 0:
                continue

            cur_pos = pos.get(sym, 0)
            bids, asks = _get_book(od)
            if not bids and not asks:
                continue

            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            if best_bid is not None and best_ask is not None:
                mkt_mid = (best_bid + best_ask) / 2
            elif best_bid is not None:
                mkt_mid = best_bid
            else:
                mkt_mid = best_ask

            fv, _delta = self._option_fv(spot, K, T, tc, sig, r, phase)

            if mkt_mid is not None:
                a_vev = _ewma_alpha(cfg["vev_mid_ewma_hl"])
                S.vev_ewma[K] = _ewma_update(S.vev_ewma.get(K), mkt_mid, a_vev)

            p_sym = {sym: cur_pos}
            took_best_ask = False

            max_ask = cfg.get("pre_choice_max_ask", {}).get(sym)
            allowed_in_risk_mode = not (
                mode in ("RISK_OFF", "ENDGAME_FLAT") and sym not in ("VEV_6000", "VEV_6500")
            )
            allowed_in_endgame = ts <= cfg["final_flat_ts"]
            allow_pre_choice_buy = (
                phase == "PRE_CHOICE"
                and best_ask is not None
                and max_ask is not None
                and best_ask <= max_ask
                and cur_pos < cap
                and allowed_in_risk_mode
                and (ts <= cfg["endgame_ts"] or sym in ("VEV_6000", "VEV_6500"))
                and allowed_in_endgame
            )

            if mode in ("RISK_OFF", "ENDGAME_FLAT") and sym == "VEV_5500":
                allow_pre_choice_buy = False

            if allow_pre_choice_buy and K >= 5300:
                call_intr = max(spot - K, 0)
                put_intr = max(K - spot, 0)
                ch_floor = max(call_intr, put_intr)
                if ch_floor > best_ask + floor_min_edge:
                    av = asks[0][1] if asks else 0
                    qty = _clip(min(av, limits.get(sym, 300)), sym, p_sym, limits)
                    if qty > 0:
                        filled = self._safe_add_order(orders_out, sym, best_ask, qty, pos, sent_qty, phase, cfg, limits)
                        if filled > 0:
                            logs.append(f"FLOOR_TAKE {sym} a={best_ask} floor={ch_floor:.0f}")
                            p_sym[sym] = p_sym.get(sym, cur_pos) + filled
                            cur_pos += filled
                            took_best_ask = True

            eff_edge = take_edge * (cfg["high_strike_take_mult"] if K >= 5300 else 1.0)
            if best_ask is not None and fv - best_ask > eff_edge and not took_best_ask:
                if phase != "PRE_CHOICE" or allow_pre_choice_buy:
                    av = asks[0][1] if asks else 0
                    qty = _clip(min(av, 15), sym, p_sym, limits)
                    if qty > 0:
                        filled = self._safe_add_order(orders_out, sym, best_ask, qty, pos, sent_qty, phase, cfg, limits)
                        if filled > 0:
                            p_sym[sym] = p_sym.get(sym, cur_pos) + filled
                            cur_pos += filled

            if not (phase == "PRE_CHOICE" and cfg.get("no_sell_vev_pre_choice", True)):
                if best_bid is not None and best_bid - fv > eff_edge:
                    bv = bids[0][1] if bids else 0
                    qty = _clip(-min(bv, 15), sym, p_sym, limits)
                    if qty < 0:
                        filled = self._safe_add_order(orders_out, sym, best_bid, qty, pos, sent_qty, phase, cfg, limits)
                        if filled < 0:
                            p_sym[sym] = p_sym.get(sym, cur_pos) + filled
                            cur_pos += filled

            if phase != "PRE_CHOICE" and mode not in ("RISK_OFF", "ENDGAME_FLAT"):
                for t in mtrades.get(sym, []):
                    tp = t.price if hasattr(t, 'price') else t.get('price', 0)
                    tq = t.quantity if hasattr(t, 'quantity') else t.get('quantity', 0)
                    if fv - tp > cfg["intercept_min_edge"]:
                        qty = _clip(min(tq, 15), sym, p_sym, limits)
                        if qty > 0:
                            filled = self._safe_add_order(orders_out, sym, int(tp), qty, pos, sent_qty, phase, cfg, limits)
                            if filled > 0:
                                p_sym[sym] = p_sym.get(sym, cur_pos) + filled
                                cur_pos += filled

            if phase != "PRE_CHOICE" and mode not in ("RISK_OFF", "ENDGAME_FLAT") and best_bid is not None and best_ask is not None:
                spread = best_ask - best_bid
                if spread > 2:
                    bid_q = round(fv - hs)
                    ask_q = round(fv + hs)
                    if bid_q >= best_bid:
                        bid_q = best_bid - 1
                    if ask_q <= best_ask:
                        ask_q = best_ask + 1
                    bid_sz = _clip(min(5, limits.get(sym, 300) - cur_pos), sym, p_sym, limits)
                    ask_sz = _clip(-min(5, limits.get(sym, 300) + cur_pos), sym, p_sym, limits)
                    if bid_sz > 0:
                        self._safe_add_order(orders_out, sym, bid_q, bid_sz, pos, sent_qty, phase, cfg, limits)
                    if ask_sz < 0:
                        self._safe_add_order(orders_out, sym, ask_q, ask_sz, pos, sent_qty, phase, cfg, limits)

        return S

    # ── Option FV by phase ────────────────────────────────────────────────────

    def _option_fv(self, S, K, T, tc, sig, r, phase):
        """Returns (fair_value, delta)."""
        if phase == "PRE_CHOICE" or phase == "AT_CHOICE":
            fv    = _chooser_fv(S, K, T, tc, sig, r)
            delta = _chooser_delta(S, K, T, tc, sig, r)
        elif phase == "POST_CHOICE_CALL":
            fv    = _bs_call(S, K, T, sig, r)
            delta = _bs_call_delta(S, K, T, sig, r)
            fv    = max(fv, max(S - K, 0))
        elif phase == "POST_CHOICE_PUT":
            fv    = _bs_put(S, K, T, sig, r)
            delta = _bs_put_delta(S, K, T, sig, r)
            fv    = max(fv, max(K - S, 0))
        else:
            fv    = _chooser_fv(S, K, T, tc, sig, r)
            delta = _chooser_delta(S, K, T, tc, sig, r)
        return fv, delta

    # ── Delta hedge ───────────────────────────────────────────────────────────

    def _delta_hedge(self, od, velvet_fv, pos, orders_out, limits, cfg, T, tc, sig, r, phase, sent_qty):
        if od is None or phase == "PRE_CHOICE" or not cfg.get("enable_postchoice_delta_hedge", False):
            return

        # Compute net VEV delta
        net_delta = 0.0
        for K in VEV_STRIKES:
            sym = f"VEV_{K}"
            cur = pos.get(sym, 0)
            if cur == 0:
                continue
            S = velvet_fv
            _, d = self._option_fv(S, K, T, tc, sig, r, phase)
            net_delta += cur * d

        velvet_pos = pos.get("VELVETFRUIT_EXTRACT", 0)
        hedge_needed = -(net_delta + velvet_pos)

        # Only hedge if imbalance is significant
        if abs(hedge_needed) < cfg["hedge_delta_threshold"]:
            return

        hedge_qty = round(hedge_needed)
        bids, asks = _get_book(od)
        max_slip = cfg["max_hedge_slippage"]
        lim = limits["VELVETFRUIT_EXTRACT"]
        p_sym = {"VELVETFRUIT_EXTRACT": velvet_pos}

        if hedge_qty > 0 and asks:
            for ap, av in asks:
                if ap > velvet_fv + max_slip:
                    break
                qty = _clip(min(av, hedge_qty), "VELVETFRUIT_EXTRACT", p_sym, limits)
                if qty > 0:
                    filled = self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", ap, qty, pos, sent_qty, phase, cfg, limits)
                    if filled > 0:
                        hedge_qty -= filled
                        p_sym["VELVETFRUIT_EXTRACT"] = p_sym.get("VELVETFRUIT_EXTRACT", 0) + filled
                if hedge_qty <= 0:
                    break
        elif hedge_qty < 0 and bids:
            rem = abs(hedge_qty)
            for bp, bv in bids:
                if bp < velvet_fv - max_slip:
                    break
                qty = _clip(-min(bv, rem), "VELVETFRUIT_EXTRACT", p_sym, limits)
                if qty < 0:
                    filled = self._safe_add_order(orders_out, "VELVETFRUIT_EXTRACT", bp, qty, pos, sent_qty, phase, cfg, limits)
                    if filled < 0:
                        rem += filled
                        p_sym["VELVETFRUIT_EXTRACT"] = p_sym.get("VELVETFRUIT_EXTRACT", 0) + filled
                if rem <= 0:
                    break
