"""
backtest_runner.py - Multi-dimensional sweep backtest for Round 4 strategy.

Supports:
  - match_trades modes: ["all", "worse", "none"]
  - choice_time_days, total_expiry_days, sigma, risk_profile, counterparty_threshold

Outputs per run:
  - pnl by day, by symbol, by module
  - turnover, max inventory, drawdown proxy, edge attribution

NO future leak:
  - Market trades at timestamp T are only visible at timestamp T.
  - No lookahead into future prices.
  - Order fill simulation is conservative (no partial fill above our price).
"""

import csv
import json
import os
import copy
from collections import defaultdict, namedtuple
from itertools import product as iterproduct

# ── Import trader and config ──────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(__file__))

from trader_submission import Trader, CONFIG, CEILING_OVERRIDES, VEV_STRIKES, FINAL_MODE

DATA_DIR = r"D:\IMC Prosperity 4\Data\Round4\ROUND_4"
DAYS = [1, 2, 3]

# ── Mock datamodel types ──────────────────────────────────────────────────────
OrderDepth = namedtuple("OrderDepth", ["buy_orders", "sell_orders"])

class MockTrade:
    def __init__(self, sym, buyer, seller, price, qty):
        self.symbol = sym; self.buyer = buyer; self.seller = seller
        self.price = price; self.quantity = qty

class MockState:
    def __init__(self, ts, order_depths, market_trades, position, trader_data="", own_trades=None):
        self.timestamp = ts
        self.order_depths = order_depths
        self.market_trades = market_trades
        self.position = position
        self.traderData = trader_data
        self.listings = {}; self.own_trades = own_trades or {}; self.observations = None

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_data():
    """Load prices and trades for all days into indexed structures."""
    # prices: (day, ts, product) -> {bid, bid_vol, ask, ask_vol, mid}
    price_index = {}
    # trades: (day, ts) -> list of MockTrade
    trade_index = defaultdict(list)

    for day in DAYS:
        with open(os.path.join(DATA_DIR, f"prices_round_4_day_{day}.csv")) as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                ts = int(row["timestamp"])
                p  = row["product"]
                def flt(k): return float(row[k]) if row.get(k) else None
                price_index[(day, ts, p)] = {
                    "bid":     flt("bid_price_1"),
                    "bid_vol": flt("bid_volume_1"),
                    "ask":     flt("ask_price_1"),
                    "ask_vol": flt("ask_volume_1"),
                    "mid":     flt("mid_price"),
                }

        with open(os.path.join(DATA_DIR, f"trades_round_4_day_{day}.csv")) as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                ts = int(row["timestamp"])
                trade_index[(day, ts)].append(MockTrade(
                    row["symbol"], row["buyer"], row["seller"],
                    float(row["price"]), int(row["quantity"])
                ))

    # Build sorted timestamp list per day
    ts_by_day = {}
    for day in DAYS:
        ts_set = sorted({ts for (d, ts, _) in price_index if d == day})
        ts_by_day[day] = ts_set

    return price_index, trade_index, ts_by_day


def _build_order_depth(price_data):
    """Convert price row dict to mock OrderDepth."""
    buy_orders, sell_orders = {}, {}
    bid = price_data.get("bid")
    bv  = price_data.get("bid_vol")
    ask = price_data.get("ask")
    av  = price_data.get("ask_vol")
    if bid is not None and bv is not None and bv != 0:
        buy_orders[int(bid)] = int(bv)
    if ask is not None and av is not None and av != 0:
        sell_orders[int(ask)] = -int(av)  # sell orders are negative
    return OrderDepth(buy_orders=buy_orders, sell_orders=sell_orders)


# ── Fill simulation ───────────────────────────────────────────────────────────

def _simulate_fills(orders_out, price_index, trade_index, day, ts,
                    match_trades_mode, positions):
    """
    Simulate order fills for one timestamp.

    For passive orders: fill if market trades at our price (within match_trades_mode).
    For aggressive orders (price inside spread): fill immediately.

    match_trades_mode:
      "all"   - our passive orders fill if any market trade occurs at a compatible price
      "worse" - fill only if market trade price is same or worse for counterparty
      "none"  - passive orders never fill; only aggressive orders fill

    Returns: fills dict {symbol: [(price, qty, side)]}
    """
    fills = defaultdict(list)

    all_products = list(orders_out.keys())

    for sym in all_products:
        sym_orders = orders_out.get(sym, [])
        pd = price_index.get((day, ts, sym), {})
        best_ask = pd.get("ask")
        best_bid = pd.get("bid")

        # Market trades at this ts for this symbol
        mkt = [t for t in trade_index.get((day, ts), []) if t.symbol == sym]

        for order in sym_orders:
            price = order.price
            qty   = order.quantity   # >0 buy, <0 sell

            if qty > 0:  # buy order
                # Aggressive: fills if our price >= best_ask
                if best_ask is not None and price >= best_ask:
                    fill_price = best_ask
                    fill_qty   = min(qty, int(pd.get("ask_vol", qty) or qty))
                    fills[sym].append((fill_price, fill_qty, "buy"))
                    positions[sym] = positions.get(sym, 0) + fill_qty
                elif match_trades_mode != "none":
                    # Passive fill from market trade hitting our price
                    for t in mkt:
                        if match_trades_mode == "all" and t.price <= price:
                            fills[sym].append((price, min(qty, t.quantity), "buy"))
                            positions[sym] = positions.get(sym, 0) + min(qty, t.quantity)
                            break
                        elif match_trades_mode == "worse" and t.price <= price:
                            # Seller accepted worse price than ours
                            fills[sym].append((price, min(qty, t.quantity), "buy"))
                            positions[sym] = positions.get(sym, 0) + min(qty, t.quantity)
                            break

            elif qty < 0:  # sell order
                abs_qty = abs(qty)
                if best_bid is not None and price <= best_bid:
                    fill_price = best_bid
                    fill_qty   = min(abs_qty, int(pd.get("bid_vol", abs_qty) or abs_qty))
                    fills[sym].append((fill_price, -fill_qty, "sell"))
                    positions[sym] = positions.get(sym, 0) - fill_qty
                elif match_trades_mode != "none":
                    for t in mkt:
                        if match_trades_mode == "all" and t.price >= price:
                            fills[sym].append((price, -min(abs_qty, t.quantity), "sell"))
                            positions[sym] = positions.get(sym, 0) - min(abs_qty, t.quantity)
                            break
                        elif match_trades_mode == "worse" and t.price >= price:
                            fills[sym].append((price, -min(abs_qty, t.quantity), "sell"))
                            positions[sym] = positions.get(sym, 0) - min(abs_qty, t.quantity)
                            break

    return fills


# ── PnL accounting ────────────────────────────────────────────────────────────

class PnLTracker:
    def __init__(self):
        self.cash = 0.0
        self.positions = defaultdict(int)
        self.pnl_by_sym = defaultdict(float)
        self.pnl_by_day = defaultdict(float)
        self.turnover    = 0.0
        self.fills_by_sym = defaultdict(list)
        self.max_pos_by_sym = defaultdict(int)
        self.peak_pnl = 0.0
        self.worst_drawdown = 0.0
        self.fill_count = 0

    def record_fill(self, sym, price, qty, day):
        """qty > 0 = bought at price, qty < 0 = sold at price."""
        self.cash -= price * qty
        self.positions[sym] += qty
        self.turnover += abs(price * qty)
        self.fill_count += 1
        self.fills_by_sym[sym].append((price, qty))
        self.max_pos_by_sym[sym] = max(self.max_pos_by_sym[sym], abs(self.positions[sym]))

    def mark_to_market(self, mids, day):
        """Compute unrealized PnL and add to daily PnL."""
        total = self.cash
        for sym, pos in self.positions.items():
            m = mids.get(sym)
            if m is not None:
                total += pos * m
        return total

    def mark_pnl_snapshot(self, mids, day):
        """Record daily PnL snapshot."""
        mtm = self.mark_to_market(mids, day)
        self.pnl_by_day[day] = mtm
        self.peak_pnl = max(self.peak_pnl, mtm)
        self.worst_drawdown = min(self.worst_drawdown, mtm - self.peak_pnl)

    def mark_intraday_snapshot(self, mids):
        mtm = self.mark_to_market(mids, -1)
        self.peak_pnl = max(self.peak_pnl, mtm)
        self.worst_drawdown = min(self.worst_drawdown, mtm - self.peak_pnl)

    def final_summary(self, mids):
        total_pnl = self.mark_to_market(mids, -1)
        pnl_by_product = {}
        for sym, fills in self.fills_by_sym.items():
            realized = sum(-price * qty for price, qty in fills)
            pnl_by_product[sym] = realized + self.positions.get(sym, 0) * mids.get(sym, 0)
        return {
            "total_pnl": total_pnl,
            "cash":      self.cash,
            "positions": dict(self.positions),
            "pnl_by_day": dict(self.pnl_by_day),
            "turnover":  self.turnover,
            "fill_count": self.fill_count,
            "max_pos":   dict(self.max_pos_by_sym),
            "peak_pnl": self.peak_pnl,
            "worst_drawdown": self.worst_drawdown,
            "pnl_by_product": pnl_by_product,
        }


class OwnTrade:
    def __init__(self, sym, buyer, seller, price, qty, ts):
        self.symbol = sym
        self.buyer = buyer
        self.seller = seller
        self.price = price
        self.quantity = qty
        self.timestamp = ts


def _fills_to_own_trades(fills, ts):
    own = defaultdict(list)
    for sym, sym_fills in fills.items():
        for fill_price, fill_qty, side in sym_fills:
            qty = abs(fill_qty)
            if side == "buy":
                own[sym].append(OwnTrade(sym, "SUBMISSION", "MARKET", fill_price, qty, ts))
            else:
                own[sym].append(OwnTrade(sym, "MARKET", "SUBMISSION", fill_price, qty, ts))
    return dict(own)


# ── Single backtest run ───────────────────────────────────────────────────────

def run_backtest(config_overrides=None, match_trades_mode="worse", verbose=False):
    """
    Run one complete backtest with given config overrides.
    Returns summary dict.
    """
    cfg = copy.deepcopy(CONFIG)
    if config_overrides:
        cfg.update(config_overrides)

    price_index, trade_index, ts_by_day = _load_data()

    trader   = Trader()
    trader._cfg = cfg  # inject config

    pnl = PnLTracker()
    trader_data_str = ""
    positions = defaultdict(int)
    own_trades = {}
    modes_triggered = set()
    vev_confirmation_triggered = False
    endgame_flat_triggered = False
    cap_violation_occurred = False
    order_count_by_sym = defaultdict(int)
    forbidden_order_count = 0
    forbidden_symbols_ordered = set()
    last_trader_state = {}
    curve_snapshots = []
    hydro_diag = {
        "final_mode": cfg.get("final_mode", FINAL_MODE),
        "hydro_orders_sent": 0,
        "orders_vev": 0,
        "blocked_orders": 0,
        "hydro_cp_score": 0.0,
        "cp_events_seen": 0,
        "mode_ticks": {"SB": 0, "AE": 0, "CF": 0},
        "directional_mode_ticks": 0,
        "inside_quotes_sent": 0,
        "buy_quotes_sent": 0,
        "sell_quotes_sent": 0,
        "blocked_non_hydrogel_orders": 0,
        "eligible_reentry_ticks": 0,
        "reentry_start_pnl": 0.0,
        "reentry_attempts": 0,
        "profit_protect_triggered": False,
        "reentry_triggered": False,
        "risk_off_triggered": False,
        "stop_triggered": False,
        "endgame_triggered": False,
        "final_flat_triggered": False,
        "hydro_cap": 0,
        "hydro_cash": 0.0,
        "hydro_pnl_proxy": 0.0,
        "hydro_peak_pnl": 0.0,
        "hydro_drawdown": 0.0,
        "risk_mode": "",
        "mode": "",
        # Diagnostic tick counters
        "ticks_normal": 0,
        "ticks_profit_protect": 0,
        "ticks_reentry": 0,
        "ticks_risk_off": 0,
        "ticks_stop": 0,
        "ticks_endgame": 0,
        # Order counters per state
        "orders_normal": 0,
        "orders_profit_protect": 0,
        "orders_reentry": 0,
        "orders_risk_off": 0,
        "orders_endgame": 0,
        # Spread eligibility counters
        "sp14": 0, "sp15": 0, "sp16": 0, "sp18": 0,
        # Blocked opportunity counters
        "blk_spread": 0, "blk_pos": 0, "blk_risk": 0, "blk_end": 0, "blk_cross": 0,
        # Profit floor and unlock counters
        "protected_profit_floor": None,
        "spread14_unlock_attempts": 0,
        "spread15_unlock_attempts": 0,
    }

    all_products = (
        ["HYDROGEL_PACK", "VELVETFRUIT_EXTRACT"]
        + [f"VEV_{k}" for k in VEV_STRIKES]
    )

    for day in DAYS:
        ts_list = ts_by_day[day]
        day_start_pnl = pnl.mark_to_market({}, day)

        for ts in ts_list:
            # Build order depths from price data
            order_depths = {}
            mids = {}
            for sym in all_products:
                pd = price_index.get((day, ts, sym), {})
                if pd is not None and len(pd) > 0:
                    order_depths[sym] = _build_order_depth(pd)
                    mids[sym] = pd.get("mid")

            # Market trades at this timestamp
            ts_trades = trade_index.get((day, ts), [])
            market_trades_dict = defaultdict(list)
            for t in ts_trades:
                market_trades_dict[t.symbol].append(t)

            # Build mock state
            state = MockState(
                ts=ts,
                order_depths=order_depths,
                market_trades=dict(market_trades_dict),
                position=dict(positions),
                trader_data=trader_data_str,
                own_trades=own_trades,
            )

            # Run trader
            try:
                orders_out, _conv, trader_data_str = trader.run(state)
            except Exception as e:
                if verbose:
                    print(f"  ERROR day={day} ts={ts}: {e}")
                continue

            allowed_symbols = None
            if cfg.get("strategy_mode") == "FINAL_SPRINT_ARCHITECTURE":
                if cfg.get("final_mode", FINAL_MODE) in ("SAFE_BASELINE", "ALPHA_ENGINE"):
                    allowed_symbols = {"HYDROGEL_PACK"}
                else:
                    allowed_symbols = {"HYDROGEL_PACK", "VEV_6000", "VEV_6500"}
            elif cfg.get("strategy_mode") in (
                "HYDROGEL_ONLY_PASSIVE",
                "HYDROGEL_ONLY_INSIDE_SPREAD",
                "HYDROGEL_ONLY_INSIDE_SPREAD_CAPACITY",
                "HYDROGEL_ONLY_INSIDE_SPREAD_REENTRY",
                "HYDROGEL_ONLY_REENTRY_DIAGNOSTIC_UNLOCK",
            ):
                allowed_symbols = {"HYDROGEL_PACK"}

            for sym, sym_orders in orders_out.items():
                order_count_by_sym[sym] += len(sym_orders)
                if allowed_symbols is not None and sym not in allowed_symbols:
                    forbidden_order_count += len(sym_orders)
                    forbidden_symbols_ordered.add(sym)

            try:
                trader_state = json.loads(trader_data_str) if trader_data_str else {}
                last_trader_state = trader_state
                mode = trader_state.get("md")
                if mode:
                    modes_triggered.add(mode)
                if trader_state.get("vc", 0):
                    vev_confirmation_triggered = True
                if mode in ("ENDGAME", "ENDGAME_FLAT"):
                    endgame_flat_triggered = True
                if int(trader_state.get("cv", 0)) > 0:
                    cap_violation_occurred = True

                hydro_diag["hydro_orders_sent"] = max(
                    hydro_diag["hydro_orders_sent"],
                    int(trader_state.get("hbo", 0)) + int(trader_state.get("hso", 0)),
                )
                hydro_diag["final_mode"] = trader_state.get("fmd", hydro_diag["final_mode"])
                hydro_diag["orders_vev"] = max(hydro_diag["orders_vev"], int(trader_state.get("ove", 0)))
                hydro_diag["blocked_orders"] = max(hydro_diag["blocked_orders"], int(trader_state.get("blo", 0)))
                hydro_diag["hydro_cp_score"] = float(trader_state.get("hcs", hydro_diag["hydro_cp_score"]))
                hydro_diag["cp_events_seen"] = max(hydro_diag["cp_events_seen"], int(trader_state.get("cpe", 0)))
                hydro_diag["mode_ticks"] = trader_state.get("mtk", hydro_diag["mode_ticks"])
                hydro_diag["directional_mode_ticks"] = max(hydro_diag["directional_mode_ticks"], int(trader_state.get("dtk", 0)))
                hydro_diag["inside_quotes_sent"] = max(hydro_diag["inside_quotes_sent"], int(trader_state.get("isq", 0)))
                hydro_diag["buy_quotes_sent"] = max(hydro_diag["buy_quotes_sent"], int(trader_state.get("hbo", 0)))
                hydro_diag["sell_quotes_sent"] = max(hydro_diag["sell_quotes_sent"], int(trader_state.get("hso", 0)))
                hydro_diag["blocked_non_hydrogel_orders"] = max(hydro_diag["blocked_non_hydrogel_orders"], int(trader_state.get("bno", 0)))
                hydro_diag["eligible_reentry_ticks"] = int(trader_state.get("ert", 0))
                hydro_diag["reentry_start_pnl"] = float(trader_state.get("rsp", 0.0))
                hydro_diag["reentry_attempts"] = max(hydro_diag["reentry_attempts"], int(trader_state.get("rat", 0)))
                hydro_diag["profit_protect_triggered"] = hydro_diag["profit_protect_triggered"] or bool(trader_state.get("ppt", 0))
                hydro_diag["reentry_triggered"] = hydro_diag["reentry_triggered"] or bool(trader_state.get("ret", 0))
                hydro_diag["risk_off_triggered"] = hydro_diag["risk_off_triggered"] or bool(trader_state.get("rot", 0))
                hydro_diag["stop_triggered"] = hydro_diag["stop_triggered"] or bool(trader_state.get("spt", 0))
                hydro_diag["endgame_triggered"] = hydro_diag["endgame_triggered"] or bool(trader_state.get("eg", 0))
                hydro_diag["final_flat_triggered"] = hydro_diag["final_flat_triggered"] or bool(trader_state.get("ff", 0))
                hydro_diag["hydro_cap"] = int(trader_state.get("hc", 0))
                hydro_diag["hydro_cash"] = float(trader_state.get("hca", 0.0))
                hydro_diag["hydro_pnl_proxy"] = float(trader_state.get("hpp", 0.0))
                hydro_diag["hydro_peak_pnl"] = max(hydro_diag["hydro_peak_pnl"], float(trader_state.get("hp", 0.0)))
                hydro_diag["hydro_drawdown"] = max(hydro_diag["hydro_drawdown"], float(trader_state.get("hdp", 0.0)))
                hydro_diag["risk_mode"] = trader_state.get("rm", hydro_diag["risk_mode"])
                hydro_diag["mode"] = trader_state.get("md", hydro_diag["mode"])
                # New diagnostic counters (cumulative: take max = final value)
                hydro_diag["ticks_normal"] = max(hydro_diag["ticks_normal"], int(trader_state.get("tN", 0)))
                hydro_diag["ticks_profit_protect"] = max(hydro_diag["ticks_profit_protect"], int(trader_state.get("tP", 0)))
                hydro_diag["ticks_reentry"] = max(hydro_diag["ticks_reentry"], int(trader_state.get("tR", 0)))
                hydro_diag["ticks_risk_off"] = max(hydro_diag["ticks_risk_off"], int(trader_state.get("tO", 0)))
                hydro_diag["ticks_stop"] = max(hydro_diag["ticks_stop"], int(trader_state.get("tS", 0)))
                hydro_diag["ticks_endgame"] = max(hydro_diag["ticks_endgame"], int(trader_state.get("tE", 0)))
                hydro_diag["orders_normal"] = max(hydro_diag["orders_normal"], int(trader_state.get("oN", 0)))
                hydro_diag["orders_profit_protect"] = max(hydro_diag["orders_profit_protect"], int(trader_state.get("oP", 0)))
                hydro_diag["orders_reentry"] = max(hydro_diag["orders_reentry"], int(trader_state.get("oR", 0)))
                hydro_diag["orders_risk_off"] = max(hydro_diag["orders_risk_off"], int(trader_state.get("oO", 0)))
                hydro_diag["orders_endgame"] = max(hydro_diag["orders_endgame"], int(trader_state.get("oE", 0)))
                hydro_diag["sp14"] = max(hydro_diag["sp14"], int(trader_state.get("sp14", 0)))
                hydro_diag["sp15"] = max(hydro_diag["sp15"], int(trader_state.get("sp15", 0)))
                hydro_diag["sp16"] = max(hydro_diag["sp16"], int(trader_state.get("sp16", 0)))
                hydro_diag["sp18"] = max(hydro_diag["sp18"], int(trader_state.get("sp18", 0)))
                hydro_diag["blk_spread"] = max(hydro_diag["blk_spread"], int(trader_state.get("blks", 0)))
                hydro_diag["blk_pos"] = max(hydro_diag["blk_pos"], int(trader_state.get("blkp", 0)))
                hydro_diag["blk_risk"] = max(hydro_diag["blk_risk"], int(trader_state.get("blkr", 0)))
                hydro_diag["blk_end"] = max(hydro_diag["blk_end"], int(trader_state.get("blke", 0)))
                hydro_diag["blk_cross"] = max(hydro_diag["blk_cross"], int(trader_state.get("blkc", 0)))
                if trader_state.get("ppf") is not None:
                    hydro_diag["protected_profit_floor"] = float(trader_state.get("ppf"))
                hydro_diag["spread14_unlock_attempts"] = max(hydro_diag["spread14_unlock_attempts"], int(trader_state.get("s14a", 0)))
                hydro_diag["spread15_unlock_attempts"] = max(hydro_diag["spread15_unlock_attempts"], int(trader_state.get("s15a", 0)))
            except Exception:
                pass

            # Simulate fills
            fills = _simulate_fills(
                orders_out, price_index, trade_index, day, ts,
                match_trades_mode, positions
            )
            own_trades = _fills_to_own_trades(fills, ts)

            # Record fills
            for sym, sym_fills in fills.items():
                for fill_price, fill_qty, side in sym_fills:
                    pnl.record_fill(sym, fill_price, fill_qty, day)
                    if verbose:
                        print(f"  FILL day={day} ts={ts} {sym} "
                              f"{'BUY' if fill_qty>0 else 'SELL'} "
                              f"{abs(fill_qty)}@{fill_price:.1f}")

            pnl.mark_intraday_snapshot(mids)
            curve_snapshots.append((day, ts, pnl.mark_to_market(mids, -1)))

        # End of day mark
        day_mids = {sym: price_index.get((day, ts_list[-1], sym), {}).get("mid", 0)
                    for sym in all_products}
        pnl.mark_pnl_snapshot(day_mids, day)

        if verbose:
            day_pnl = pnl.mark_to_market(day_mids, day)
            print(f"Day {day}: total PnL={day_pnl:.1f}  "
                  f"positions={dict((k,v) for k,v in pnl.positions.items() if v != 0)}")

    # Final mark
    last_day = DAYS[-1]
    last_ts  = ts_by_day[last_day][-1]
    final_mids = {sym: price_index.get((last_day, last_ts, sym), {}).get("mid", 0)
                  for sym in all_products}

    summary = pnl.final_summary(final_mids)
    summary["modes_triggered"] = sorted(modes_triggered)
    summary["vev_confirmation_triggered"] = vev_confirmation_triggered
    summary["endgame_flat_triggered"] = endgame_flat_triggered
    summary["cap_violation_occurred"] = cap_violation_occurred
    summary["order_count_by_sym"] = dict(order_count_by_sym)
    summary["forbidden_order_count"] = forbidden_order_count
    summary["forbidden_symbols_ordered"] = sorted(forbidden_symbols_ordered)
    summary["final_hydro_position"] = int(summary["positions"].get("HYDROGEL_PACK", 0))
    summary["max_hydro_position"] = int(summary["max_pos"].get("HYDROGEL_PACK", 0))
    summary["max_drawdown"] = abs(summary.get("worst_drawdown", 0.0))
    summary["hydro_diagnostics"] = hydro_diag
    summary["last_trader_state"] = last_trader_state
    pre_25k_peak = max((value for _day, ts, value in curve_snapshots if ts < 25000), default=summary.get("total_pnl", 0.0))
    post_25k_peak = max((value for _day, ts, value in curve_snapshots if ts >= 25000), default=summary.get("total_pnl", 0.0))
    summary["curve_kept_growing_after_25k"] = post_25k_peak > pre_25k_peak + 1.0
    summary["flatlined"] = post_25k_peak <= pre_25k_peak + 1.0
    summary["config"] = {
        "match_trades": match_trades_mode,
        "strategy_mode": cfg.get("strategy_mode"),
        "final_mode": cfg.get("final_mode", FINAL_MODE),
        "choice_time_days": cfg.get("choice_time_days"),
        "total_expiry_days": cfg.get("total_expiry_days"),
        "sigma": cfg.get("sigma"),
        "risk_profile": cfg.get("risk_profile"),
        "version": cfg.get("version"),
    }
    return summary


# ── Sweep ─────────────────────────────────────────────────────────────────────

# Ablation dimensions - the 5 most important sweep combos
ABLATION_PRIORITY = [
    # (description, config_overrides, match_mode)
    ("baseline_stable",
     {"version": "stable", "choice_time_days": 2, "total_expiry_days": 7, "sigma": 0.30},
     "worse"),

    ("ceiling_aggressive",
     {**CEILING_OVERRIDES, "choice_time_days": 2, "total_expiry_days": 7, "sigma": 0.30},
     "all"),

    ("sigma_sensitivity",
     {"version": "stable", "choice_time_days": 2, "total_expiry_days": 7, "sigma": 0.20},
     "worse"),

    ("short_choice_time",
     {"version": "stable", "choice_time_days": 0.5, "total_expiry_days": 7, "sigma": 0.30},
     "worse"),

    ("no_intercept",
     {"version": "stable", "intercept_min_edge": 999.0},
     "none"),
]

SWEEP_GRID = {
    "match_trades":       ["all", "worse", "none"],
    "choice_time_days":   [0.5, 1.0, 2.0, 5.0],
    "total_expiry_days":  [3.0, 7.0, 14.0],
    "sigma":              [0.20, 0.30, 0.40],
    "risk_profile":       ["conservative", "balanced", "aggressive"],
}


def run_priority_ablations(verbose=False):
    """Run the 5 priority ablations. Recommended first step."""
    results = []
    for name, overrides, match_mode in ABLATION_PRIORITY:
        print(f"\nRunning: {name} (match={match_mode})")
        try:
            result = run_backtest(config_overrides=overrides,
                                  match_trades_mode=match_mode,
                                  verbose=verbose)
            result["name"] = name
            results.append(result)
            print(f"  PnL={result['total_pnl']:.1f}  "
                  f"Turnover={result['turnover']:.0f}  "
                  f"MaxPos={max(result['max_pos'].values(), default=0)}")
        except Exception as e:
            print(f"  FAILED: {e}")
    return results


def run_full_sweep(max_runs=None, verbose=False):
    """
    Full parameter sweep. Can be expensive; use max_runs to cap.
    """
    keys = list(SWEEP_GRID.keys())
    vals = list(SWEEP_GRID.values())
    combos = list(iterproduct(*vals))
    if max_runs:
        combos = combos[:max_runs]

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        match_mode = params.pop("match_trades")
        print(f"[{i+1}/{len(combos)}] {params} match={match_mode}")
        try:
            r = run_backtest(config_overrides=params,
                             match_trades_mode=match_mode,
                             verbose=False)
            r["params"] = {**params, "match_trades": match_mode}
            results.append(r)
            print(f"  PnL={r['total_pnl']:.1f}")
        except Exception as e:
            print(f"  FAILED: {e}")
    return results


def print_report(results):
    """Print summary table of backtest results."""
    print("\n" + "="*90)
    print(f"{'Name/Config':40s} {'PnL':>10} {'Turnover':>12} {'MaxPos':>8} {'Match':>7}")
    print("="*90)
    for r in sorted(results, key=lambda x: -x.get("total_pnl", 0)):
        cfg_r = r.get("config", r.get("params", {}))
        name  = r.get("name", json.dumps(cfg_r, separators=(',', ':')))[:40]
        print(f"{name:40s} {r['total_pnl']:>10.1f} "
              f"{r['turnover']:>12.0f} "
              f"{max(r['max_pos'].values(), default=0):>8d} "
              f"{cfg_r.get('match_trades',''):>7}")

    # PnL by symbol breakdown (from best run)
    if results:
        best = max(results, key=lambda x: x["total_pnl"])
        print(f"\nBest run: {best.get('name','?')}  PnL={best['total_pnl']:.1f}")
        print("  Positions:")
        for sym, pos in sorted(best["positions"].items()):
            if pos != 0:
                print(f"    {sym}: {pos}")


def print_hydro_diagnostics(result):
    diag = result.get("hydro_diagnostics", {})
    print("\nHYDRO Diagnostics")
    print("=" * 90)
    print(f"strategy_mode: {result.get('config', {}).get('strategy_mode')}")
    print(f"final_mode: {result.get('config', {}).get('final_mode')}")
    print(f"final_pnl: {result.get('total_pnl', 0.0):.1f}")
    print(f"peak_pnl: {result.get('peak_pnl', 0.0):.1f}")
    print(f"final_hydro_position: {result.get('final_hydro_position', 0)}")
    print(f"max_hydro_position: {result.get('max_hydro_position', 0)}")
    print(f"max_drawdown: {result.get('max_drawdown', 0.0):.1f}")
    print(f"hydro_orders_sent: {diag.get('hydro_orders_sent', 0)}")
    print(f"orders_vev: {diag.get('orders_vev', 0)}")
    print(f"hydro_cp_score: {diag.get('hydro_cp_score', 0.0):.2f}")
    print(f"cp_events_seen: {diag.get('cp_events_seen', 0)}")
    print(f"directional_mode_ticks: {diag.get('directional_mode_ticks', 0)}")
    print(f"inside_quotes_sent: {diag.get('inside_quotes_sent', 0)}")
    print(f"buy_quotes_sent: {diag.get('buy_quotes_sent', 0)}")
    print(f"sell_quotes_sent: {diag.get('sell_quotes_sent', 0)}")
    print(f"blocked_orders: {diag.get('blocked_orders', 0)}")
    print(f"reentry_attempts: {diag.get('reentry_attempts', 0)}")
    print(f"spread15_unlock_attempts: {diag.get('spread15_unlock_attempts', 0)}")
    print(f"spread14_unlock_attempts: {diag.get('spread14_unlock_attempts', 0)}")
    print(f"blocked_non_hydrogel_orders: {diag.get('blocked_non_hydrogel_orders', 0)}")
    print(f"profit_protect_triggered: {diag.get('profit_protect_triggered', False)}")
    print(f"reentry_triggered: {diag.get('reentry_triggered', False)}")
    print(f"risk_off_triggered: {diag.get('risk_off_triggered', False)}")
    print(f"stop_triggered: {diag.get('stop_triggered', False)}")
    print(f"endgame_triggered: {diag.get('endgame_triggered', False)}")
    print(f"final_flat_triggered: {diag.get('final_flat_triggered', False)}")
    print(f"protected_profit_floor: {diag.get('protected_profit_floor')}")
    print(f"forbidden_order_count: {result.get('forbidden_order_count', 0)}")
    print(f"forbidden_symbols_ordered: {result.get('forbidden_symbols_ordered', [])}")
    print(f"final_positions: {result.get('positions', {})}")
    print(f"curve_kept_growing_after_25k: {result.get('curve_kept_growing_after_25k', False)}")
    print(f"flatlined: {result.get('flatlined', False)}")
    print(f"mode_ticks: {diag.get('mode_ticks', {})}")
    print(f"\nState time distribution:")
    print(f"  NORMAL ticks:         {diag.get('ticks_normal', 0)}")
    print(f"  PROFIT_PROTECT ticks: {diag.get('ticks_profit_protect', 0)}")
    print(f"  REENTRY ticks:        {diag.get('ticks_reentry', 0)}")
    print(f"  RISK_OFF ticks:       {diag.get('ticks_risk_off', 0)}")
    print(f"  STOP ticks:           {diag.get('ticks_stop', 0)}")
    print(f"  ENDGAME ticks:        {diag.get('ticks_endgame', 0)}")
    print(f"\nOrders generated per state:")
    print(f"  NORMAL:        {diag.get('orders_normal', 0)}")
    print(f"  PROFIT_PROTECT:{diag.get('orders_profit_protect', 0)}")
    print(f"  REENTRY:       {diag.get('orders_reentry', 0)}")
    print(f"  RISK_OFF:      {diag.get('orders_risk_off', 0)}")
    print(f"  ENDGAME:       {diag.get('orders_endgame', 0)}")
    print(f"\nSpread eligibility (cumulative ticks spread >= X):")
    print(f"  spread>=14: {diag.get('sp14', 0)}")
    print(f"  spread>=15: {diag.get('sp15', 0)}")
    print(f"  spread>=16: {diag.get('sp16', 0)}")
    print(f"  spread>=18: {diag.get('sp18', 0)}")
    print(f"\nBlocked opportunity counters:")
    print(f"  blk_spread:  {diag.get('blk_spread', 0)}")
    print(f"  blk_pos:     {diag.get('blk_pos', 0)}")
    print(f"  blk_risk:    {diag.get('blk_risk', 0)}")
    print(f"  blk_end:     {diag.get('blk_end', 0)}")
    print(f"  blk_cross:   {diag.get('blk_cross', 0)}")
    # Flatline analysis (use only active trading ticks, exclude endgame)
    total_ticks = sum(diag.get(f"ticks_{s}", 0) for s in ("normal", "profit_protect", "reentry", "risk_off", "stop", "endgame"))
    active_ticks = total_ticks - diag.get("ticks_endgame", 0)
    flatline_reason = "UNKNOWN"
    if active_ticks > 0:
        t_stop = diag.get("ticks_stop", 0)
        t_risk = diag.get("ticks_risk_off", 0)
        blk_s = diag.get("blk_spread", 0)
        if t_stop + t_risk > active_ticks * 0.5:
            flatline_reason = "B: risk state blocks trading"
        elif blk_s > active_ticks * 0.3:
            flatline_reason = "A: no spread opportunity"
        elif diag.get("blk_pos", 0) > 10:
            flatline_reason = "C: inventory not flat enough"
        elif diag.get("blk_cross", 0) > 10:
            flatline_reason = "E: crossing-safety blocked"
        elif diag.get("ticks_endgame", 0) > active_ticks:
            flatline_reason = "D: endgame rules"
        else:
            flatline_reason = "UNKNOWN"
    print(f"\nFlatline reason (best guess): {flatline_reason}")


def classify_hydro_result(result):
    pnl = float(result.get("total_pnl", 0.0))
    drawdown = float(result.get("max_drawdown", 0.0))
    diag = result.get("hydro_diagnostics", {})
    mode = result.get("config", {}).get("strategy_mode", "")
    final_mode = result.get("config", {}).get("final_mode", FINAL_MODE)
    flatlined = bool(result.get("flatlined", False))
    if result.get("forbidden_order_count", 0) > 0:
        return "IMPLEMENTATION_FAILED"
    if result.get("cap_violation_occurred", False):
        return "IMPLEMENTATION_FAILED"
    if mode == "FINAL_SPRINT_ARCHITECTURE" and final_mode == "ALPHA_ENGINE":
        if pnl > 150.0 and not flatlined:
            return "ALPHA_ENGINE_SUCCESS_150_PLUS"
        if pnl > 100.0 and not flatlined:
            return "ALPHA_ENGINE_SUCCESS_100_PLUS"
        if pnl > 0.0 and not flatlined:
            return "ALPHA_ENGINE_SMALL_POSITIVE"
        if pnl >= 0.0 and flatlined:
            return "ALPHA_ENGINE_FLATLINE"
        return "ALPHA_ENGINE_NEGATIVE"
    if mode == "FINAL_SPRINT_ARCHITECTURE" and final_mode == "CONVEX_FINAL":
        if pnl > 150.0:
            return "ALPHA_ENGINE_SUCCESS_150_PLUS"
        if pnl > 100.0:
            return "ALPHA_ENGINE_SUCCESS_100_PLUS"
        if pnl > 0.0:
            return "ALPHA_ENGINE_SMALL_POSITIVE"
        return "ALPHA_ENGINE_NEGATIVE"
    if "DIAGNOSTIC_UNLOCK" in mode:
        if pnl > 120.0 and drawdown <= max(60.0, pnl * 0.6):
            return "HYDRO_UNLOCK_IMPROVED_TO_120_PLUS"
        if 80.0 < pnl <= 120.0:
            return "HYDRO_UNLOCK_IMPROVED_BUT_BELOW_120"
        if 45.0 <= pnl <= 80.0:
            return "HYDRO_UNLOCK_SAME_AS_REENTRY"
        if pnl < 0.0:
            if diag.get("reentry_attempts", 0) > 0 or diag.get("spread15_unlock_attempts", 0) > 0:
                return "HYDRO_UNLOCK_OVERTRADES"
            return "HYDRO_UNLOCK_NEGATIVE"
        return "HYDRO_UNLOCK_IMPLEMENTATION_FAILED"
    # Legacy classification for old modes
    if pnl > 80.0 and drawdown <= max(40.0, pnl * 0.75):
        return "HYDRO_REENTRY_IMPROVED"
    if 40.0 <= pnl <= 60.0:
        return "HYDRO_REENTRY_SAME_AS_CAPACITY"
    if pnl < 0.0:
        if diag.get("reentry_attempts", 0) > 0 or diag.get("reentry_triggered", False):
            return "HYDRO_REENTRY_OVERTRADES"
        return "HYDRO_REENTRY_NEGATIVE"
    return "HYDRO_REENTRY_NEGATIVE"


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "priority"

    if mode == "priority":
        results = run_priority_ablations(verbose=True)
        print_report(results)
    elif mode == "sweep":
        max_r = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        results = run_full_sweep(max_runs=max_r)
        print_report(results)
    elif mode == "single":
        result = run_backtest(verbose=True)
        print(json.dumps(result, indent=2, default=str))
        print_hydro_diagnostics(result)
        print(f"classification: {classify_hydro_result(result)}")
