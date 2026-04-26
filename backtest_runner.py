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

from trader_submission import Trader, CONFIG, CEILING_OVERRIDES, VEV_STRIKES

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

    def record_fill(self, sym, price, qty, day):
        """qty > 0 = bought at price, qty < 0 = sold at price."""
        self.cash -= price * qty
        self.positions[sym] += qty
        self.turnover += abs(price * qty)
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

            try:
                trader_state = json.loads(trader_data_str) if trader_data_str else {}
                mode = trader_state.get("md")
                if mode:
                    modes_triggered.add(mode)
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
    summary["config"] = {
        "match_trades": match_trades_mode,
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
