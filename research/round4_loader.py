"""
round4_loader.py - Load and clean Round 4 data into analysis-ready structures.

Outputs:
  - prices_df: long-format prices with mid/spread/depth
  - trades_df: all market trades with counterparty info
  - mid_pivot: timestamp x product pivot of mid prices
  - depth_stats: per-product top-of-book stats
  - cp_netflow: counterparty x product net buy volume
  - cp_pnl: counterparty forward PnL by horizon
  - option_matrix: buyer-seller-symbol trade counts
"""

import os
import csv
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────────────────
DATA_DIR = r"D:\IMC Prosperity 4\Data\Round4\ROUND_4"
DAYS = [1, 2, 3]
PRODUCTS = [
    "HYDROGEL_PACK", "VELVETFRUIT_EXTRACT",
    "VEV_4000", "VEV_4500", "VEV_5000", "VEV_5100", "VEV_5200",
    "VEV_5300", "VEV_5400", "VEV_5500", "VEV_6000", "VEV_6500",
]
VEV_STRIKES = {int(p.split("_")[1]) for p in PRODUCTS if p.startswith("VEV_")}
FORWARD_HORIZONS = [1, 5, 10, 50]   # in tick units (1 tick = 100ms)

# ── Loaders ──────────────────────────────────────────────────────────────────

def load_prices_raw(days=DAYS):
    """Return list of dicts with all price rows, adding global_ts = day*1e6 + timestamp."""
    rows = []
    for day in days:
        path = os.path.join(DATA_DIR, f"prices_round_4_day_{day}.csv")
        with open(path) as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                row["day"] = int(row["day"])
                row["timestamp"] = int(row["timestamp"])
                row["global_ts"] = day * 1_000_000 + int(row["timestamp"])
                row["mid_price"] = float(row["mid_price"]) if row["mid_price"] else None
                for field in ["bid_price_1","bid_volume_1","bid_price_2","bid_volume_2",
                              "ask_price_1","ask_volume_1","ask_price_2","ask_volume_2"]:
                    try:
                        row[field] = float(row[field]) if row[field] else None
                    except Exception:
                        row[field] = None
                rows.append(row)
    return rows


def load_trades_raw(days=DAYS):
    """Return list of dicts with all trade rows."""
    rows = []
    for day in days:
        path = os.path.join(DATA_DIR, f"trades_round_4_day_{day}.csv")
        with open(path) as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                row["day"] = day
                row["timestamp"] = int(row["timestamp"])
                row["global_ts"] = day * 1_000_000 + int(row["timestamp"])
                row["price"] = float(row["price"])
                row["quantity"] = int(row["quantity"])
                rows.append(row)
    return rows


# ── Aggregations ─────────────────────────────────────────────────────────────

def build_mid_pivot(prices_raw):
    """
    Returns dict: global_ts -> {product: mid_price}
    Also returns sorted list of (global_ts, timestamp, day) tuples.
    """
    pivot = defaultdict(dict)
    ts_meta = {}
    for row in prices_raw:
        gts = row["global_ts"]
        pivot[gts][row["product"]] = row["mid_price"]
        ts_meta[gts] = (row["timestamp"], row["day"])
    sorted_ts = sorted(pivot.keys())
    return pivot, sorted_ts, ts_meta


def depth_stats(prices_raw):
    """Per-product: mean_mid, std_mid, mean_spread, mean_bid_vol, mean_ask_vol."""
    acc = defaultdict(lambda: {"mids": [], "spreads": [], "bid_vols": [], "ask_vols": []})
    for row in prices_raw:
        p = row["product"]
        if row["mid_price"] is not None:
            acc[p]["mids"].append(row["mid_price"])
        if row["bid_price_1"] and row["ask_price_1"]:
            acc[p]["spreads"].append(row["ask_price_1"] - row["bid_price_1"])
        if row["bid_volume_1"]:
            acc[p]["bid_vols"].append(row["bid_volume_1"])
        if row["ask_volume_1"]:
            acc[p]["ask_vols"].append(row["ask_volume_1"])

    stats = {}
    for p, d in acc.items():
        def safe_mean(lst): return sum(lst) / len(lst) if lst else None
        def safe_std(lst):
            if len(lst) < 2: return None
            m = sum(lst) / len(lst)
            return (sum((x - m)**2 for x in lst) / (len(lst) - 1)) ** 0.5
        stats[p] = {
            "mean_mid": safe_mean(d["mids"]),
            "std_mid": safe_std(d["mids"]),
            "mean_spread": safe_mean(d["spreads"]),
            "mean_bid_vol": safe_mean(d["bid_vols"]),
            "mean_ask_vol": safe_mean(d["ask_vols"]),
            "n": len(d["mids"]),
        }
    return stats


def counterparty_netflow(trades_raw):
    """
    Returns dict: {counterparty: {product: net_volume}}
    Buyer = positive, seller = negative.
    """
    net = defaultdict(lambda: defaultdict(int))
    for t in trades_raw:
        net[t["buyer"]][t["symbol"]] += t["quantity"]
        net[t["seller"]][t["symbol"]] -= t["quantity"]
    return {cp: dict(v) for cp, v in net.items()}


def counterparty_forward_pnl(trades_raw, prices_raw, horizons=FORWARD_HORIZONS):
    """
    For each trade, compute how much the mid moved after h ticks.
    Returns dict: {counterparty: {horizon: {product: [pnl_per_unit]}}}

    PnL for buyer = (mid_at_t+h - trade_price) * quantity
    PnL for seller = (trade_price - mid_at_t+h) * quantity
    No future leak: we look up mid at t+h using pre-built pivot.
    """
    # Build mid lookup: (day, product) -> sorted list of (timestamp, mid)
    mid_series = defaultdict(list)
    for row in prices_raw:
        if row["mid_price"] is not None:
            mid_series[(row["day"], row["product"])].append(
                (row["timestamp"], row["mid_price"])
            )
    for key in mid_series:
        mid_series[key].sort()

    def lookup_mid_at(day, product, ts_target):
        """Binary search for mid at or after ts_target."""
        series = mid_series.get((day, product), [])
        if not series:
            return None
        lo, hi = 0, len(series) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid][0] < ts_target:
                lo = mid + 1
            else:
                hi = mid
        return series[lo][1] if series[lo][0] >= ts_target else None

    # Tick interval is 100ms in this data
    TICK = 100

    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for t in trades_raw:
        day = t["day"]
        sym = t["symbol"]
        ts0 = t["timestamp"]
        tp = t["price"]

        for h in horizons:
            future_ts = ts0 + h * TICK
            future_mid = lookup_mid_at(day, sym, future_ts)
            if future_mid is None:
                continue

            buyer_pnl_per_unit = future_mid - tp
            seller_pnl_per_unit = tp - future_mid

            result[t["buyer"]][h][sym].append(buyer_pnl_per_unit)
            result[t["seller"]][h][sym].append(seller_pnl_per_unit)

    # Flatten: mean pnl per unit
    flat = {}
    for cp, horizons_dict in result.items():
        flat[cp] = {}
        for h, sym_dict in horizons_dict.items():
            flat[cp][h] = {}
            for sym, pnls in sym_dict.items():
                flat[cp][h][sym] = {
                    "mean": sum(pnls) / len(pnls),
                    "n": len(pnls),
                    "pos_rate": sum(1 for x in pnls if x > 0) / len(pnls),
                }
    return flat


def option_volume_matrix(trades_raw):
    """
    Returns dict: {(buyer, seller, symbol): {"count": int, "total_qty": int, "total_notional": float}}
    """
    mat = defaultdict(lambda: {"count": 0, "total_qty": 0, "total_notional": 0.0})
    for t in trades_raw:
        key = (t["buyer"], t["seller"], t["symbol"])
        mat[key]["count"] += 1
        mat[key]["total_qty"] += t["quantity"]
        mat[key]["total_notional"] += t["price"] * t["quantity"]
    return dict(mat)


# ── Main analysis printout ────────────────────────────────────────────────────

def run_analysis():
    print("Loading data...")
    prices_raw = load_prices_raw()
    trades_raw = load_trades_raw()

    print(f"\nProducts: {PRODUCTS}")
    print(f"Total price rows: {len(prices_raw)}")
    print(f"Total trade rows: {len(trades_raw)}")

    print("\n=== Depth Stats (all days) ===")
    ds = depth_stats(prices_raw)
    for p in PRODUCTS:
        s = ds.get(p, {})
        print(f"  {p:30s} mid={s.get('mean_mid',0):.2f}  "
              f"std={s.get('std_mid',0) or 0:.2f}  "
              f"spread={s.get('mean_spread',0) or 0:.2f}  "
              f"bid_vol={s.get('mean_bid_vol',0) or 0:.1f}  "
              f"ask_vol={s.get('mean_ask_vol',0) or 0:.1f}")

    print("\n=== Counterparty Net Flow ===")
    net = counterparty_netflow(trades_raw)
    cps = sorted(net.keys())
    for cp in cps:
        flows = net[cp]
        print(f"  {cp}:")
        for p in PRODUCTS:
            v = flows.get(p, 0)
            if v != 0:
                print(f"    {p}: {v:+d}")

    print("\n=== Counterparty Forward PnL (per unit, mean) ===")
    fp = counterparty_forward_pnl(trades_raw, prices_raw)
    for cp in sorted(fp.keys()):
        print(f"  {cp}:")
        for h in FORWARD_HORIZONS:
            if h not in fp[cp]:
                continue
            items = sorted(fp[cp][h].items(), key=lambda x: abs(x[1]["mean"]), reverse=True)
            top = items[:4]
            line = "  ".join(f"{sym}:{d['mean']:+.2f}(n={d['n']})" for sym, d in top)
            print(f"    h={h:3d}: {line}")

    print("\n=== Option Volume Matrix (buyer->seller, top pairs) ===")
    ovm = option_volume_matrix(trades_raw)
    sorted_ovm = sorted(ovm.items(), key=lambda x: x[1]["total_qty"], reverse=True)
    for (buyer, seller, sym), stats in sorted_ovm[:30]:
        print(f"  {buyer:10s} -> {seller:10s} {sym:25s} "
              f"qty={stats['total_qty']:4d}  notional={stats['total_notional']:.0f}")

    return prices_raw, trades_raw


if __name__ == "__main__":
    run_analysis()
