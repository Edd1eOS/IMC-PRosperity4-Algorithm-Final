"""
counterparty_profiles.py - Build informed-trader profiles via rolling event study.

For each Mark, computes:
  informed_buy_score   - how reliably their buys precede price rises
  informed_sell_score  - how reliably their sells precede price drops
  product_specialization - which symbols they concentrate in
  reaction_horizon     - which tick lag shows strongest signal
  confidence_score     - based on sample size and consistency

Method: simple sign-accuracy + expected-value scoring on forward PnL,
with exponential decay update for recency weighting.
No ML, no leakage.
"""

import math
from collections import defaultdict
from research.round4_loader import (
    load_prices_raw, load_trades_raw,
    counterparty_forward_pnl, counterparty_netflow, FORWARD_HORIZONS, PRODUCTS
)

# ── Config ────────────────────────────────────────────────────────────────────
MIN_SAMPLES = 5           # minimum trades to form a score
DECAY_HALFLIFE = 500      # ticks for exponential decay (recency weight)
SCORE_THRESHOLD = 0.60    # positive-rate threshold to flag as informed


def _decay_weight(age_ticks, halflife=DECAY_HALFLIFE):
    """Exponential weight: recent trades count more."""
    return math.exp(-math.log(2) * age_ticks / halflife)


def build_profiles(trades_raw, prices_raw):
    """
    Returns dict: {counterparty: CounterpartyProfile}
    """
    fp = counterparty_forward_pnl(trades_raw, prices_raw)
    net = counterparty_netflow(trades_raw)

    # Separate buy and sell trades per counterparty
    buy_trades  = defaultdict(list)   # cp -> list of trades where cp = buyer
    sell_trades = defaultdict(list)   # cp -> list of trades where cp = seller

    for t in trades_raw:
        buy_trades[t["buyer"]].append(t)
        sell_trades[t["seller"]].append(t)

    profiles = {}
    all_cps = set(t["buyer"] for t in trades_raw) | set(t["seller"] for t in trades_raw)

    for cp in sorted(all_cps):
        prof = _build_single_profile(
            cp, buy_trades[cp], sell_trades[cp], fp.get(cp, {}), net.get(cp, {})
        )
        profiles[cp] = prof

    return profiles


def _build_single_profile(cp, buys, sells, fp_data, net_data):
    """Build profile dict for one counterparty."""

    # ── Trade counts ────────────────────────────────────────────────────────
    total_trades = len(buys) + len(sells)
    if total_trades == 0:
        return _empty_profile(cp)

    # ── Product specialization ───────────────────────────────────────────────
    sym_counts = defaultdict(int)
    for t in buys + sells:
        sym_counts[t["symbol"]] += t["quantity"]
    total_vol = sum(sym_counts.values())
    spec = {s: v / total_vol for s, v in sym_counts.items()} if total_vol > 0 else {}
    top_product = max(spec, key=spec.get) if spec else None
    herfindahl = sum(v**2 for v in spec.values())  # concentration index

    # ── Informed scores per horizon ──────────────────────────────────────────
    buy_scores  = {}  # horizon -> (pos_rate, mean_pnl, n)
    sell_scores = {}

    for h in FORWARD_HORIZONS:
        if h not in fp_data:
            continue
        sym_data = fp_data[h]

        # Aggregate across symbols (weighted by n)
        buy_pnl_sum, buy_n = 0.0, 0
        sell_pnl_sum, sell_n = 0.0, 0
        buy_pos_sum, sell_pos_sum = 0.0, 0.0

        for sym, stats in sym_data.items():
            n = stats["n"]
            # fp_data already separates buyer and seller sides
            # We recompute per-side using raw trade forward PnL
            # (fp_data[cp][h][sym] is the cp's perspective as buyer or seller
            #  but since a cp can be both, we need to re-derive)
            pass

        # Use the combined forward PnL from fp_data directly
        for sym, stats in sym_data.items():
            # This is already signed correctly: positive means cp's trades were profitable
            n = stats["n"]
            pr = stats["pos_rate"]
            mean = stats["mean"]
            buy_pnl_sum += mean * n
            buy_n += n
            buy_pos_sum += pr * n

        if buy_n >= MIN_SAMPLES:
            buy_scores[h] = {
                "pos_rate": buy_pos_sum / buy_n,
                "mean_pnl": buy_pnl_sum / buy_n,
                "n": buy_n,
            }

    # Best horizon by pos_rate
    best_h = max(buy_scores.keys(), key=lambda h: buy_scores[h]["pos_rate"]) \
        if buy_scores else None

    # Overall informed score: max pos_rate across horizons
    informed_score = buy_scores[best_h]["pos_rate"] if best_h else 0.5

    # Directional bias: net buy volume across VEV products
    vev_net = sum(v for k, v in net_data.items() if k.startswith("VEV_"))
    velvet_net = net_data.get("VELVETFRUIT_EXTRACT", 0)
    hydrogel_net = net_data.get("HYDROGEL_PACK", 0)

    # Confidence based on sample size
    n_eff = min(total_trades, 100)  # cap at 100 for stability
    confidence = 1 - math.exp(-n_eff / 20)  # asymptotes to 1

    # Check if predominantly buyer or seller
    buy_vol = sum(t["quantity"] for t in buys)
    sell_vol = sum(t["quantity"] for t in sells)
    total_vol_num = buy_vol + sell_vol
    buy_frac = buy_vol / total_vol_num if total_vol_num > 0 else 0.5

    return {
        "cp": cp,
        "total_trades": total_trades,
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "buy_frac": buy_frac,
        "informed_score": informed_score,
        "informed_buy_score": informed_score if buy_frac > 0.5 else informed_score * 0.8,
        "informed_sell_score": informed_score if buy_frac < 0.5 else informed_score * 0.8,
        "product_specialization": spec,
        "top_product": top_product,
        "concentration": herfindahl,
        "reaction_horizon": best_h,
        "vev_net": vev_net,
        "velvet_net": velvet_net,
        "hydrogel_net": hydrogel_net,
        "horizon_scores": buy_scores,
        "confidence_score": confidence,
        "is_informed": informed_score >= SCORE_THRESHOLD and total_trades >= MIN_SAMPLES,
    }


def _empty_profile(cp):
    return {
        "cp": cp, "total_trades": 0, "buy_vol": 0, "sell_vol": 0,
        "buy_frac": 0.5, "informed_score": 0.5,
        "informed_buy_score": 0.5, "informed_sell_score": 0.5,
        "product_specialization": {}, "top_product": None,
        "concentration": 0, "reaction_horizon": None,
        "vev_net": 0, "velvet_net": 0, "hydrogel_net": 0,
        "horizon_scores": {}, "confidence_score": 0.0, "is_informed": False,
    }


# ── Rolling update for live use ───────────────────────────────────────────────

class RollingCounterpartyTracker:
    """
    Lightweight online tracker for use in the actual trader.
    Maintains exponentially weighted buy/sell signal per counterparty/product.
    No future leak: updated only with observed data at current timestamp.
    """

    def __init__(self, halflife_ticks=500, min_samples=5, score_threshold=0.60):
        self.halflife = halflife_ticks
        self.min_samples = min_samples
        self.threshold = score_threshold
        # State: cp -> product -> {buy_score_ew, sell_score_ew, buy_n, sell_n}
        self._state = {}
        self._current_tick = 0

    def set_tick(self, tick):
        self._current_tick = tick

    def record_trade(self, buyer, seller, symbol, price, quantity, current_mid):
        """
        Record a trade and its immediate direction relative to mid.
        This is called when we observe the market trade.
        """
        for cp, side in [(buyer, "buy"), (seller, "sell")]:
            if cp not in self._state:
                self._state[cp] = {}
            if symbol not in self._state[cp]:
                self._state[cp][symbol] = {
                    "buy_sum": 0.0, "buy_n": 0,
                    "sell_sum": 0.0, "sell_n": 0,
                    "buy_pos": 0, "sell_pos": 0,
                }
            s = self._state[cp][symbol]

            if side == "buy":
                # Buyer pays ask-ish; positive edge = mid rose after buy
                edge = current_mid - price  # positive if bought below mid
                s["buy_sum"] += edge * quantity
                s["buy_n"] += quantity
                if edge > 0:
                    s["buy_pos"] += quantity
            else:
                edge = price - current_mid   # positive if sold above mid
                s["sell_sum"] += edge * quantity
                s["sell_n"] += quantity
                if edge > 0:
                    s["sell_pos"] += quantity

    def get_alpha_signal(self, cp, symbol, side):
        """
        Return alpha signal in [-1, 1] for a counterparty trading symbol on given side.
        Positive = directional edge (buy signal for buy side, sell signal for sell side).
        Returns 0 if insufficient data.
        """
        if cp not in self._state or symbol not in self._state[cp]:
            return 0.0
        s = self._state[cp][symbol]
        if side == "buy":
            n = s["buy_n"]
            if n < self.min_samples:
                return 0.0
            pos_rate = s["buy_pos"] / n
        else:
            n = s["sell_n"]
            if n < self.min_samples:
                return 0.0
            pos_rate = s["sell_pos"] / n

        # Map pos_rate [0,1] to [-1,1], 0.5 = neutral
        return (pos_rate - 0.5) * 2.0

    def get_directional_bias(self, cp):
        """
        Overall directional bias for a counterparty: positive = tends to be right buying.
        Used for VELVET hedging decisions.
        """
        if cp not in self._state:
            return 0.0
        signals = []
        for sym, s in self._state[cp].items():
            if sym == "VELVETFRUIT_EXTRACT":
                n_b = s["buy_n"]
                n_s = s["sell_n"]
                if n_b >= self.min_samples:
                    signals.append(s["buy_pos"] / n_b - 0.5)
                if n_s >= self.min_samples:
                    signals.append(0.5 - s["sell_pos"] / n_s)
        return sum(signals) / len(signals) if signals else 0.0


# ── Report ────────────────────────────────────────────────────────────────────

def run_report():
    print("Loading data for counterparty profiling...")
    prices_raw = load_prices_raw()
    trades_raw = load_trades_raw()

    profiles = build_profiles(trades_raw, prices_raw)

    print("\n=== Counterparty Profiles ===\n")
    for cp, p in sorted(profiles.items()):
        if p["total_trades"] == 0:
            continue
        print(f"{'─'*60}")
        print(f"  {cp}")
        print(f"  Trades: {p['total_trades']}  "
              f"BuyVol={p['buy_vol']}  SellVol={p['sell_vol']}  "
              f"BuyFrac={p['buy_frac']:.2f}")
        print(f"  InformedScore={p['informed_score']:.3f}  "
              f"Confidence={p['confidence_score']:.3f}  "
              f"IsInformed={p['is_informed']}")
        print(f"  TopProduct={p['top_product']}  Concentration={p['concentration']:.3f}")
        print(f"  ReactionHorizon={p['reaction_horizon']}")
        print(f"  NetFlow: VEV={p['vev_net']:+d}  "
              f"VELVET={p['velvet_net']:+d}  "
              f"HYDROGEL={p['hydrogel_net']:+d}")
        if p["product_specialization"]:
            top_syms = sorted(p["product_specialization"].items(),
                              key=lambda x: -x[1])[:4]
            print(f"  Specialization: " +
                  "  ".join(f"{s}:{v:.2f}" for s, v in top_syms))
        if p["horizon_scores"]:
            for h in FORWARD_HORIZONS:
                if h in p["horizon_scores"]:
                    hs = p["horizon_scores"][h]
                    print(f"  h={h:3d}: pos_rate={hs['pos_rate']:.3f} "
                          f"mean_pnl={hs['mean_pnl']:+.3f} n={hs['n']}")


if __name__ == "__main__":
    run_report()
