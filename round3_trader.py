"""
round3_trader.py — IMC Prosperity 4 Round 3 Strategy [v1.0]
===========================================================
Round 3 "Gloves Off" / Solvenar — first voucher (options) round.

Products & limits (per P4 Wiki, Round 3 page):
  VELVETFRUIT_EXTRACT        — delta-1 underlying, limit 200
  HYDROGEL_PACK              — delta-1 independent asset, limit 200
  VEV_{4000..6500} (x10)     — European call vouchers on the underlying,
                               limit 300 each, TTE = 5 days at R3 start,
                               never expires during the competition
                               (R3 TTE=5d, R4=4d, R5=3d).

Architecture (three orthogonal modules, no R1/R2 code reuse):
  Module A — Voucher IV-smile deviation trader + delta hedge
    * Pure-Python Black-Scholes (math.erf) for price / delta / vega.
    * Bisection IV reversal per strike, per tick.
    * Live parabolic smile fit (sigma = a*m^2 + b*m + c) over filtered
      samples via 3x3 Gaussian elimination (no numpy).
    * Per-strike rolling z-score of iv_dev drives SELL (high IV)
      and BUY (low IV) signals, aggressing on best bid/ask.
    * Aggregated net delta hedged on VELVETFRUIT_EXTRACT with
      deadband + step-cap + hard hedge budget pre-check.
  Module B — HYDROGEL_PACK clean-slate market maker
    microprice +- adaptive edge, inventory skew, opportunistic taker.
  Module C — Global position clamping & trader-data compression.

Preferences applied (all conservative, per user approval):
  z threshold 2.0, voucher lot cap 15, min smile samples 4,
  rolling window 100, hedge budget 180, deadband 3, step 20.
"""

import json
import math
from datamodel import OrderDepth, TradingState, Order


# ================================================================
# Constants (source: P4 Wiki Round 3 "Gloves Off" page)
# ================================================================
SYM_UND = "VELVETFRUIT_EXTRACT"
SYM_HYD = "HYDROGEL_PACK"

UND_LIMIT = 200
HYD_LIMIT = 200
VOU_LIMIT = 300

STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
SYM_VEV = {k: "VEV_" + str(k) for k in STRIKES}

R3_START_TTE_DAYS = 5.0
TICKS_PER_DAY     = 1_000_000
DAYS_PER_YEAR     = 365.0

# IV filter & smile
MIN_SMILE_SAMPLES = 4
IV_LO             = 0.05
IV_HI             = 2.5

# Delta-based strike classification (replaces price-based DEEP_ITM_BUFFER)
DELTA_DEEP_ITM    = 0.97     # delta >= this => exclude from smile & Phase 4
DELTA_DEEP_OTM    = 0.03     # delta <= this => exclude from smile & Phase 4

# VRP-based voucher signal (replaces z-score/smile-deviation framework)
VRP_RV_WINDOW      = 50      # rolling per-tick log-return window for realized vol
VRP_WARMUP         = 20      # minimum observations before signal fires
VRP_THRESHOLD      = 0.02    # minimum |ATM_IV - RV_ann| to trigger a trade
VRP_ATM_WINGS      = 2       # number of nearest-ATM strikes to trade on
VOU_LOT_CAP        = 10      # max voucher qty per signal tick
# Annualization: observations are every 100 timestamp units; TICKS_PER_DAY in ts units
PRICE_OBS_PER_YEAR = (TICKS_PER_DAY / 100.0) * DAYS_PER_YEAR   # 3_650_000

# Delta hedge
HEDGE_BUDGET      = 180      # projected net delta must stay within +-BUDGET
HEDGE_DEADBAND    = 8
HEDGE_MAX_STEP    = 20

# HYDROGEL MM
HYD_EDGE_MIN      = 2.0
HYD_EDGE_MULT     = 0.4
HYD_INV_STRENGTH  = 6.0
HYD_LOT           = 15
HYD_SPREAD_WINDOW = 20
HYD_TAKER_PAD     = 1
HYD_INV_HARD_CAP  = 100     # absolute pos limit: only reduce-side quotes beyond this
ENABLE_OPTIONS    = False    # master switch: set True to re-enable directional voucher trades
ZONE_HIGH_ENTER   = 9990     # price crosses above this: zone -> HIGH (short-biased, ask only)
ZONE_HIGH_EXIT    = 9960     # price crosses below this: zone HIGH -> MID
ZONE_LOW_ENTER    = 9945     # price crosses below this: zone -> LOW (buy-biased, bid only + unwind)
ZONE_LOW_EXIT     = 9980     # price crosses above this: zone LOW -> MID
LOW_UNWIND_THRESH = 4        # |short pos| > this in LOW zone: taker-buy to reduce
LOW_UNWIND_LOT    = 10       # lot per taker-buy in LOW zone
HYD_SHORT_HARD_CAP = -12     # absolute max short position in HIGH zone (hard inventory cap)
MID_TAKER_FORCED_BUY_LOT = 12  # aggressively buy back shorts in MID zone
MID_TAKER_FORCED_BUY_THRESH = -8   # only if pos <= this in MID

# Logger / state
MAX_LOG_LEN       = 3750
MAX_TRADER_DATA   = 3800


# ================================================================
# Logger (byte-capped, flush once per tick)
# ================================================================
class Logger:
    def __init__(self) -> None:
        self._buf  = []
        self._used = 0

    def log(self, *args) -> None:
        msg = " ".join(str(a) for a in args)
        cost = len(msg) + 1
        if self._used + cost > MAX_LOG_LEN:
            return
        self._buf.append(msg)
        self._used += cost

    def flush(self) -> None:
        if self._buf:
            print("\n".join(self._buf))
        self._buf = []
        self._used = 0


# ================================================================
# Black-Scholes (pure-python, Python 3's math.erf is sufficient)
# ================================================================
SQRT_2   = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_call(S, K, T, sigma):
    if T <= 0.0 or sigma <= 0.0:
        return max(S - K, 0.0)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def bs_delta(S, K, T, sigma):
    if T <= 0.0 or sigma <= 0.0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def bs_vega(S, K, T, sigma):
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return S * norm_pdf(d1) * math.sqrt(T)


def implied_vol(mkt, S, K, T, lo=1e-4, hi=3.0, tol=1e-4, max_iter=50):
    """Bisect for sigma; return None if the market price is outside BS feasibility."""
    intrinsic = max(S - K, 0.0)
    if mkt <= intrinsic + 1e-6:
        return None
    if mkt >= S:
        return None
    f_lo = bs_call(S, K, T, lo) - mkt
    f_hi = bs_call(S, K, T, hi) - mkt
    if f_lo * f_hi > 0.0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_call(S, K, T, mid) - mkt
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0.0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# ================================================================
# TTE conversion
# ================================================================
def tte_years(timestamp):
    days_left = R3_START_TTE_DAYS - timestamp / TICKS_PER_DAY
    if days_left < 0.01:
        days_left = 0.01
    return days_left / DAYS_PER_YEAR


# ================================================================
# Order-book helpers
# ================================================================
def best_bid_ask(od):
    bb = max(od.buy_orders.keys()) if od.buy_orders else None
    ba = min(od.sell_orders.keys()) if od.sell_orders else None
    return bb, ba


def mid_price(od):
    bb, ba = best_bid_ask(od)
    if bb is None or ba is None:
        return None
    return 0.5 * (bb + ba)


def microprice(od):
    bb, ba = best_bid_ask(od)
    if bb is None or ba is None:
        return None
    q_bid = abs(od.buy_orders[bb])
    q_ask = abs(od.sell_orders[ba])
    total = q_bid + q_ask
    if total == 0:
        return 0.5 * (bb + ba)
    return (bb * q_ask + ba * q_bid) / total


# ================================================================
# Parabolic smile fit via 3x3 Gaussian elimination on normal equations
# ================================================================
def fit_parabola(samples):
    n = len(samples)
    if n < 3:
        return None
    s0 = s1 = s2 = s3 = s4 = 0.0
    sy0 = sy1 = sy2 = 0.0
    for m, y in samples:
        m2 = m * m
        s0 += 1.0
        s1 += m
        s2 += m2
        s3 += m2 * m
        s4 += m2 * m2
        sy0 += y
        sy1 += m * y
        sy2 += m2 * y
    mat = [
        [s4, s3, s2, sy2],
        [s3, s2, s1, sy1],
        [s2, s1, s0, sy0],
    ]
    for i in range(3):
        max_r = i
        max_v = abs(mat[i][i])
        for r in range(i + 1, 3):
            if abs(mat[r][i]) > max_v:
                max_r = r
                max_v = abs(mat[r][i])
        if max_v < 1e-12:
            return None
        if max_r != i:
            mat[i], mat[max_r] = mat[max_r], mat[i]
        inv = 1.0 / mat[i][i]
        for r in range(i + 1, 3):
            f = mat[r][i] * inv
            for c in range(i, 4):
                mat[r][c] -= f * mat[i][c]
    c2 = mat[2][3] / mat[2][2]
    c1 = (mat[1][3] - mat[1][2] * c2) / mat[1][1]
    c0 = (mat[0][3] - mat[0][1] * c1 - mat[0][2] * c2) / mat[0][0]
    return c0, c1, c2   # a, b, c in sigma = a*m^2 + b*m + c


# ================================================================
# Module A: Vouchers + delta hedge
# ================================================================
def voucher_and_hedge_orders(
    timestamp, od_und, ods_vev, pos_und, pos_vev, vrp_state, logger,
):
    result = {}
    if not ENABLE_OPTIONS:
        return result
    S = mid_price(od_und)
    if S is None:
        return result
    T = tte_years(timestamp)

    # ---- Phase 1: per-strike IV + delta computation ----
    # Smile fitting removed: signal no longer depends on cross-strike smile deviation.
    info_by_K = {}
    for K in STRIKES:
        sym = SYM_VEV[K]
        od = ods_vev.get(K)
        if od is None:
            continue
        bb, ba = best_bid_ask(od)
        if bb is None or ba is None:
            continue
        mid = 0.5 * (bb + ba)
        info = {"bb": bb, "ba": ba, "mid": mid}

        iv = implied_vol(mid, S, K, T)
        if iv is None or iv < IV_LO or iv > IV_HI:
            info["reason"] = "IV_INVALID"
            info_by_K[K] = info
            continue

        delta_k = bs_delta(S, K, T, iv)
        if delta_k >= DELTA_DEEP_ITM:
            info["reason"] = "DEEP_ITM_DELTA"
            info["mkt_iv"] = iv
            info["delta"]  = delta_k
            info_by_K[K]   = info
            continue
        if delta_k <= DELTA_DEEP_OTM:
            info["reason"] = "DEEP_OTM_DELTA"
            info["mkt_iv"] = iv
            info["delta"]  = delta_k
            info_by_K[K]   = info
            continue

        info["mkt_iv"] = iv
        info["delta"]  = delta_k
        info_by_K[K]   = info

    # ---- Phase 2: net delta from live positions (uses per-strike mkt_iv) ----
    def delta_for_K(K, info):
        reason = info.get("reason")
        if reason == "DEEP_ITM_DELTA":
            return info.get("delta", 1.0)
        if reason == "DEEP_OTM_DELTA":
            return info.get("delta", 0.0)
        if info.get("mkt_iv") is not None:
            return bs_delta(S, K, T, info["mkt_iv"])
        return 0.5

    current_net_delta = 0.0
    for K, info in info_by_K.items():
        p = pos_vev.get(K, 0)
        if p != 0:
            current_net_delta += p * delta_for_K(K, info)

    projected_net_delta = current_net_delta

    # ---- Phase 3: VRP-based trading (Realized Vol vs ATM Implied Vol) ----
    # Signal source: comparison of two independent systems (realized vs implied)
    # rather than self-referential smile-deviation z-score.
    #   VRP > VRP_THRESHOLD  => IV expensive => sell ATM vega
    #   VRP < -VRP_THRESHOLD => IV cheap    => buy  ATM vega
    rv_hist = vrp_state.get("rv_hist", [])
    S_prev  = vrp_state.get("S_prev")
    if S_prev is not None and S_prev > 0.0:
        lr = math.log(S / S_prev)
        rv_hist.append(lr)
        if len(rv_hist) > VRP_RV_WINDOW:
            del rv_hist[: len(rv_hist) - VRP_RV_WINDOW]
    vrp_state["rv_hist"] = rv_hist
    vrp_state["S_prev"]  = S

    if len(rv_hist) >= VRP_WARMUP:
        mean_lr = sum(rv_hist) / len(rv_hist)
        var_lr  = sum((x - mean_lr) * (x - mean_lr) for x in rv_hist) / max(len(rv_hist) - 1, 1)
        rv_ann  = math.sqrt(var_lr * PRICE_OBS_PER_YEAR) if var_lr > 0.0 else 0.0

        # Select nearest-ATM strikes that have valid IV and delta
        tradeable = [
            (K, info) for K, info in info_by_K.items()
            if "reason" not in info and info.get("mkt_iv") is not None
        ]
        tradeable.sort(key=lambda x: abs(x[0] - S))
        atm_strikes = tradeable[:VRP_ATM_WINGS]

        if atm_strikes:
            atm_iv = sum(info["mkt_iv"] for _, info in atm_strikes) / len(atm_strikes)
            vrp    = atm_iv - rv_ann
            logger.log("VRP=" + str(round(vrp, 4)) +
                       " RV=" + str(round(rv_ann, 4)) +
                       " ATMIV=" + str(round(atm_iv, 4)))

            if vrp > VRP_THRESHOLD:
                # IV overpriced: sell ATM vega, aggress on best bid
                for K, info in atm_strikes:
                    pos_k   = pos_vev.get(K, 0)
                    delta_k = info["delta"]
                    sym     = SYM_VEV[K]
                    max_qty = min(VOU_LOT_CAP, VOU_LIMIT + pos_k)
                    if max_qty <= 0:
                        continue
                    for try_qty in range(max_qty, 0, -1):
                        new_proj = projected_net_delta + (-try_qty) * delta_k
                        if abs(new_proj) <= HEDGE_BUDGET:
                            result.setdefault(sym, []).append(Order(sym, info["bb"], -try_qty))
                            projected_net_delta = new_proj
                            logger.log("VEV" + str(K) + " SELL " + str(try_qty) +
                                       "@" + str(info["bb"]) +
                                       " VRP=" + str(round(vrp, 3)))
                            break
            elif vrp < -VRP_THRESHOLD:
                # IV underpriced: buy ATM vega, aggress on best ask
                for K, info in atm_strikes:
                    pos_k   = pos_vev.get(K, 0)
                    delta_k = info["delta"]
                    sym     = SYM_VEV[K]
                    max_qty = min(VOU_LOT_CAP, VOU_LIMIT - pos_k)
                    if max_qty <= 0:
                        continue
                    for try_qty in range(max_qty, 0, -1):
                        new_proj = projected_net_delta + try_qty * delta_k
                        if abs(new_proj) <= HEDGE_BUDGET:
                            result.setdefault(sym, []).append(Order(sym, info["ba"], try_qty))
                            projected_net_delta = new_proj
                            logger.log("VEV" + str(K) + " BUY " + str(try_qty) +
                                       "@" + str(info["ba"]) +
                                       " VRP=" + str(round(vrp, 3)))
                            break

    # ---- Phase 4: delta hedge on underlying ----
    target = -int(round(projected_net_delta))
    if target >  UND_LIMIT: target =  UND_LIMIT
    if target < -UND_LIMIT: target = -UND_LIMIT
    diff = target - pos_und

    if abs(diff) >= HEDGE_DEADBAND:
        step = HEDGE_MAX_STEP if abs(diff) > HEDGE_MAX_STEP else abs(diff)
        bb_u, ba_u = best_bid_ask(od_und)
        if diff > 0 and ba_u is not None:
            qty = min(step, UND_LIMIT - pos_und)
            if qty > 0:
                result.setdefault(SYM_UND, []).append(Order(SYM_UND, ba_u, qty))
                logger.log("UND BUY " + str(qty) + "@" + str(ba_u) +
                           " tgt=" + str(target) + " pos=" + str(pos_und))
        elif diff < 0 and bb_u is not None:
            qty = min(step, UND_LIMIT + pos_und)
            if qty > 0:
                result.setdefault(SYM_UND, []).append(Order(SYM_UND, bb_u, -qty))
                logger.log("UND SELL " + str(qty) + "@" + str(bb_u) +
                           " tgt=" + str(target) + " pos=" + str(pos_und))

    return result


# ================================================================
# Module B: HYDROGEL market maker (clean-slate, no R1/R2 heritage)
# ================================================================
def hydrogel_orders(od, pos, spread_hist, zone, logger):
    bb, ba = best_bid_ask(od)
    if bb is None or ba is None:
        return []

    mp = microprice(od)
    if mp is None:
        return []

    raw_spread = ba - bb
    spread_hist.append(raw_spread)
    if len(spread_hist) > HYD_SPREAD_WINDOW:
        del spread_hist[: len(spread_hist) - HYD_SPREAD_WINDOW]
    avg_spread = sum(spread_hist) / len(spread_hist)

    edge = avg_spread * HYD_EDGE_MULT
    if edge < HYD_EDGE_MIN:
        edge = HYD_EDGE_MIN
    inv_penalty = (pos / HYD_LIMIT) * HYD_INV_STRENGTH

    bid_px = int(round(mp - edge - inv_penalty))
    ask_px = int(round(mp + edge - inv_penalty))
    if bid_px >= ask_px:
        bid_px = int(round(mp)) - 1
        ask_px = int(round(mp)) + 1

    orders = []
    sim_pos = pos

    # Hard inventory cap: beyond HYD_INV_HARD_CAP only allow reduce-side activity
    hard_long  = pos >= HYD_INV_HARD_CAP
    hard_short = pos <= -HYD_INV_HARD_CAP

    # MID zone forced taker buy: when in MID zone and carrying significant short,
    # aggressively buy back to compress position and reduce upswing drawdown.
    if zone == "MID" and pos <= MID_TAKER_FORCED_BUY_THRESH and not hard_long:
        if ba is not None:
            room = HYD_LIMIT - sim_pos
            qty = min(MID_TAKER_FORCED_BUY_LOT, od.sell_orders.get(ba, 0), room)
            if qty > 0:
                orders.append(Order(SYM_HYD, ba, qty))
                sim_pos += qty
                logger.log("HYD MID TAKER BUY " + str(qty) + "@" + str(ba))

    # Taker phase: aggress when book is mispriced relative to microprice
    if not hard_long and ba < mp - edge - HYD_TAKER_PAD:
        room = HYD_LIMIT - sim_pos
        qty = min(abs(od.sell_orders[ba]), room, HYD_LOT)
        if qty > 0:
            orders.append(Order(SYM_HYD, ba, qty))
            sim_pos += qty
            logger.log("HYD TAKE " + str(qty) + "@" + str(ba))
    # In HIGH zone and LOW zone, avoid aggressive short extension via taker sells.
    # LOW zone: focus on closing shorts; HIGH zone: use passive maker only for smoother control.
    if zone not in ["HIGH", "LOW"] and not hard_short and bb > mp + edge + HYD_TAKER_PAD:
        room = HYD_LIMIT + sim_pos
        qty = min(od.buy_orders[bb], room, HYD_LOT)
        if qty > 0:
            orders.append(Order(SYM_HYD, bb, -qty))
            sim_pos -= qty
            logger.log("HYD HIT " + str(qty) + "@" + str(bb))

    # Zone taker unwind: in LOW zone, if still carrying a short, cross spread to reduce.
    if zone == "LOW" and pos < -LOW_UNWIND_THRESH and not hard_long:
        qty = min(LOW_UNWIND_LOT, HYD_LIMIT - sim_pos, (-pos) - LOW_UNWIND_THRESH)
        if qty > 0:
            orders.append(Order(SYM_HYD, ba, qty))
            sim_pos += qty
            logger.log("HYD ZONE UNWIND " + str(qty) + "@" + str(ba))

    # Zone-controlled maker: HIGH = ask only (build short); LOW = bid only (close short); MID = both
    # Added: HIGH zone absolute short cap (HYD_SHORT_HARD_CAP); MID zone sell prohibition
    if zone == "HIGH":
        buy_room = 0
        # Strict cap sizing: never post more ask size than remaining room to hard cap.
        room_to_cap = max(0, sim_pos - HYD_SHORT_HARD_CAP)
        sell_room = 0 if hard_short else min(HYD_LIMIT + sim_pos, HYD_LOT, room_to_cap)
    elif zone == "LOW":
        buy_room  = 0 if hard_long else min(HYD_LIMIT - sim_pos, HYD_LOT)
        sell_room = 0
    else:  # MID
        buy_room  = 0 if hard_long else min(HYD_LIMIT - sim_pos, HYD_LOT)
        sell_room = 0  # force sell_room=0 in MID to block counter-productive short extension
    if buy_room > 0:
        orders.append(Order(SYM_HYD, bid_px, buy_room))
    if sell_room > 0:
        orders.append(Order(SYM_HYD, ask_px, -sell_room))

    return orders


# ================================================================
# Trader
# ================================================================
class Trader:

    def run(self, state: TradingState):
        logger = Logger()
        result = {}

        # -- state deserialize --
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}
        vrp_state = mem.get("vrp", {})
        if not isinstance(vrp_state, dict):
            vrp_state = {}
        hyd_spread_hist = list(mem.get("hyd_spread", []))
        hyd_zone        = mem.get("hyd_zone", "MID")

        # -- Module A: vouchers + underlying delta hedge --
        try:
            if SYM_UND in state.order_depths:
                od_und = state.order_depths[SYM_UND]
                pos_und = state.position.get(SYM_UND, 0)
                ods_vev = {}
                pos_vev = {}
                for K in STRIKES:
                    sym = SYM_VEV[K]
                    if sym in state.order_depths:
                        ods_vev[K] = state.order_depths[sym]
                    pos_vev[K] = state.position.get(sym, 0)
                a_orders = voucher_and_hedge_orders(
                    state.timestamp, od_und, ods_vev, pos_und, pos_vev,
                    vrp_state, logger,
                )
                for sym, lst in a_orders.items():
                    result.setdefault(sym, []).extend(lst)
        except Exception as e:
            logger.log("[MODA ERR] " + type(e).__name__ + ":" + str(e))

        # -- Module B: HYDROGEL --
        try:
            if SYM_HYD in state.order_depths:
                od_hyd = state.order_depths[SYM_HYD]
                pos_hyd = state.position.get(SYM_HYD, 0)
                cur_mid = microprice(od_hyd)
                if cur_mid is not None:
                    if hyd_zone == "HIGH":
                        if cur_mid < ZONE_HIGH_EXIT:
                            hyd_zone = "MID"
                    elif hyd_zone == "LOW":
                        if cur_mid > ZONE_LOW_EXIT:
                            hyd_zone = "MID"
                    else:  # MID
                        if cur_mid > ZONE_HIGH_ENTER:
                            hyd_zone = "HIGH"
                        elif cur_mid < ZONE_LOW_ENTER:
                            hyd_zone = "LOW"
                b_orders = hydrogel_orders(od_hyd, pos_hyd, hyd_spread_hist, hyd_zone, logger)
                if b_orders:
                    result.setdefault(SYM_HYD, []).extend(b_orders)
        except Exception as e:
            logger.log("[MODB ERR] " + type(e).__name__ + ":" + str(e))

        # -- Module C: global clamping (safety net across all symbols) --
        limits = {SYM_UND: UND_LIMIT, SYM_HYD: HYD_LIMIT}
        for K in STRIKES:
            limits[SYM_VEV[K]] = VOU_LIMIT
        for sym in list(result.keys()):
            lim = limits.get(sym, 50)
            cur = state.position.get(sym, 0)
            clamped = []
            sim = cur
            for o in result[sym]:
                if o.quantity > 0:
                    room = lim - sim
                    v = o.quantity if o.quantity < room else room
                    if v > 0:
                        clamped.append(Order(sym, o.price, v))
                        sim += v
                elif o.quantity < 0:
                    absq = -o.quantity
                    room = lim + sim
                    v = absq if absq < room else room
                    if v > 0:
                        clamped.append(Order(sym, o.price, -v))
                        sim -= v
            result[sym] = clamped

        # -- state serialize --
        mem["vrp"]       = vrp_state
        mem["hyd_spread"] = hyd_spread_hist[-HYD_SPREAD_WINDOW:]
        mem["hyd_zone"]   = hyd_zone
        td = json.dumps(mem, separators=(",", ":"))
        if len(td) > MAX_TRADER_DATA:
            if "rv_hist" in mem.get("vrp", {}):
                mem["vrp"]["rv_hist"] = mem["vrp"]["rv_hist"][-20:]
            mem["hyd_spread"] = mem["hyd_spread"][-10:]
            td = json.dumps(mem, separators=(",", ":"))
            logger.log("[TRIM] td=" + str(len(td)))

        logger.flush()
        return result, 0, td
