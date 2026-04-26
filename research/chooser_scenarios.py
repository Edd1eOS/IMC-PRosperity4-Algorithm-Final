"""
chooser_scenarios.py - Chooser option pricing under multiple parameter scenarios.

A "chooser" option at time t_c allows the holder to choose call or put with expiry T.
Classic formula (McDonald 2003):
    chooser_value = C(S, K, T, σ, r) + P(S, K, t_c, σ, r)
where C = BS call, P = BS put (at shorter expiry t_c).

We also compute:
  - vanilla_call_value (as naive baseline)
  - parity_put_proxy   (intrinsic + small time premium)
  - chooser_floor      = max(call_intrinsic, put_intrinsic) + small premium
  - chooser_mid        = full formula above
  - chooser_upper      = chooser_mid * stress_mult

Key insight: for high-strike OTM calls (5400-6500) with VELVET~5248,
the PUT component of the chooser value is large (intrinsic 152-1252).
Current market prices 5400 at ~17 and 6000 at ~0. Massive potential edge.
"""

import math

# ── BS Primitives ─────────────────────────────────────────────────────────────

def _norm_cdf(x):
    """Rational approximation, accurate to ~1e-5."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def bs_call(S, K, T, sigma, r=0.0):
    """Black-Scholes call price. Returns 0 if T<=0 or sigma<=0."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_put(S, K, T, sigma, r=0.0):
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call_delta(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1)


def bs_put_delta(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def chooser_value(S, K, T_expiry, t_choice, sigma, r=0.0):
    """
    Full chooser value = call(T_expiry) + put(t_choice).
    t_choice is time until choice is made.
    T_expiry is time until option expires.
    All times in years.
    """
    c = bs_call(S, K, T_expiry, sigma, r)
    p = bs_put(S, K, t_choice, sigma, r)
    return c + p


def chooser_delta(S, K, T_expiry, t_choice, sigma, r=0.0):
    """
    Delta of chooser = delta_call(T_expiry) + delta_put(t_choice)
    """
    dc = bs_call_delta(S, K, T_expiry, sigma, r)
    dp = bs_put_delta(S, K, t_choice, sigma, r)
    return dc + dp


# ── IV Estimation ─────────────────────────────────────────────────────────────

def implied_vol_call(S, K, T, market_price, r=0.0,
                     low=0.01, high=5.0, tol=1e-4, max_iter=50):
    """Binary search for implied vol from call price."""
    if market_price <= max(S - K * math.exp(-r * T), 0):
        return low  # intrinsic or below
    for _ in range(max_iter):
        mid = (low + high) / 2
        val = bs_call(S, K, T, mid, r)
        if abs(val - market_price) < tol:
            return mid
        if val < market_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# ── ATM Sigma Estimation ──────────────────────────────────────────────────────

def estimate_atm_sigma(prices_raw, T_years):
    """
    Use ATM option (closest strike to VELVET mid) to back out implied vol.
    Average across timestamps for stability.
    """
    from collections import defaultdict

    # Collect VELVET mids and VEV prices simultaneously
    ts_data = defaultdict(dict)
    for row in prices_raw:
        gts = row["global_ts"]
        p = row["product"]
        mid = row["mid_price"]
        if mid is not None:
            ts_data[gts][p] = mid

    strikes = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]

    ivs = []
    for gts in sorted(ts_data.keys())[:1000]:  # sample first 1000 ts
        d = ts_data[gts]
        S = d.get("VELVETFRUIT_EXTRACT")
        if S is None:
            continue
        # Find closest strike
        best_k = min(strikes, key=lambda k: abs(k - S))
        sym = f"VEV_{best_k}"
        mp = d.get(sym)
        if mp is None or mp <= 0:
            continue
        iv = implied_vol_call(S, best_k, T_years, mp)
        if 0.01 < iv < 3.0:
            ivs.append(iv)

    return sum(ivs) / len(ivs) if ivs else 0.30  # default 30%


# ── Scenario Analysis ─────────────────────────────────────────────────────────

STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]

# Scenarios: vary key unknowns
SCENARIOS = {
    # name: (choice_time_days, total_expiry_days, sigma)
    "base_t2_T7_s020":    (2, 7,  0.20),
    "base_t2_T7_s030":    (2, 7,  0.30),
    "base_t2_T7_s040":    (2, 7,  0.40),
    "early_t0.5_T7_s030": (0.5, 7, 0.30),
    "late_t5_T7_s030":    (5, 7,  0.30),
    "short_t1_T3_s030":   (1, 3,  0.30),
    "long_t5_T14_s030":   (5, 14, 0.30),
}


def run_scenarios(spot_price, market_prices, scenarios=SCENARIOS, r=0.0):
    """
    For each scenario and each strike, compute pricing metrics.

    market_prices: dict {strike (int): observed_mid_price}
    Returns list of dicts with all metrics.
    """
    results = []
    for scenario_name, (t_c_days, T_days, sigma) in scenarios.items():
        t_c = t_c_days / 365.0
        T   = T_days / 365.0
        S   = spot_price

        for K in STRIKES:
            sym = f"VEV_{K}"
            market_mid = market_prices.get(K)

            call_val = bs_call(S, K, T, sigma, r)
            put_val_tc = bs_put(S, K, t_c, sigma, r)
            chooser_val = call_val + put_val_tc  # full chooser formula

            call_intr = max(S - K, 0)
            put_intr  = max(K - S, 0)
            chooser_floor_val = max(call_intr, put_intr)  # conservative floor

            call_delta_val   = bs_call_delta(S, K, T, sigma, r)
            chooser_delta_val = chooser_delta(S, K, T, t_c, sigma, r)

            row = {
                "scenario": scenario_name,
                "strike": K,
                "spot": S,
                "sigma": sigma,
                "t_c_days": t_c_days,
                "T_days": T_days,
                "vanilla_call_value": call_val,
                "put_at_tc": put_val_tc,
                "chooser_mid": chooser_val,
                "chooser_floor": chooser_floor_val,
                "chooser_upper": chooser_val * 1.2,  # 20% stress buffer
                "call_intrinsic": call_intr,
                "put_intrinsic": put_intr,
                "call_delta": call_delta_val,
                "chooser_delta": chooser_delta_val,
                "market_mid": market_mid,
            }

            if market_mid is not None and market_mid > 0:
                row["mispricing_vs_call"]    = market_mid - call_val
                row["mispricing_vs_chooser"] = market_mid - chooser_val
                # Positive = market is above our fair value (overpriced by market)
                # Negative = market is below (potential buy)
            else:
                row["mispricing_vs_call"]    = None
                row["mispricing_vs_chooser"] = None

            results.append(row)

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def run_analysis():
    try:
        from research.round4_loader import load_prices_raw
        prices_raw = load_prices_raw()
    except Exception:
        print("Could not load prices; using defaults.")
        prices_raw = []

    # Compute ATM sigma from data
    if prices_raw:
        T_est = 7.0 / 365.0
        atm_sigma = estimate_atm_sigma(prices_raw, T_est)
        print(f"Estimated ATM sigma: {atm_sigma:.4f} ({atm_sigma*100:.1f}%)")
    else:
        atm_sigma = 0.30
        print(f"Using default sigma: {atm_sigma:.4f}")

    # Use typical day-1 prices
    spot = 5248.0
    market_prices = {
        4000: 1245.0, 4500: 745.0, 5000: 251.0, 5100: 165.0,
        5200: 96.0, 5300: 47.0, 5400: 17.0, 5500: 7.5,
        6000: 0.5, 6500: 0.5,
    }

    print(f"\nSpot={spot}, Market prices: {market_prices}")
    print("\n=== Chooser Scenario Analysis ===\n")

    results = run_scenarios(spot, market_prices)

    # Group by scenario for readability
    scenario_names = list(dict.fromkeys(r["scenario"] for r in results))
    for sn in scenario_names:
        print(f"\n{'─'*70}")
        print(f"  Scenario: {sn}")
        print(f"  {'Strike':>7} {'Vanilla':>8} {'ChooserMid':>12} {'ChooserFloor':>13} "
              f"{'MktMid':>8} {'Edge_v_Call':>12} {'Edge_v_Chooser':>15} {'CDelta':>8}")
        for r in results:
            if r["scenario"] != sn:
                continue
            evac = r.get("mispricing_vs_call")
            evac_str = f"{evac:+.2f}" if evac is not None else "  n/a"
            evch = r.get("mispricing_vs_chooser")
            evch_str = f"{evch:+.2f}" if evch is not None else "  n/a"
            mm = r["market_mid"]
            mm_str = f"{mm:.1f}" if mm is not None else "  n/a"
            # Flag high-strike opportunities
            flag = ""
            if evch is not None and evch < -5:
                flag = " <<< BUY"
            print(f"  {r['strike']:>7}  {r['vanilla_call_value']:>8.2f}  "
                  f"{r['chooser_mid']:>12.2f}  {r['chooser_floor']:>13.2f}  "
                  f"{mm_str:>8}  {evac_str:>12}  {evch_str:>15}  "
                  f"{r['chooser_delta']:>8.4f}{flag}")

    print("\n=== High-Strike Opportunity Summary ===")
    high_strikes = [5300, 5400, 5500, 6000, 6500]
    for K in high_strikes:
        print(f"\nStrike {K} (put_intrinsic={max(spot-K,0):.0f}, "
              f"call_intrinsic={max(spot-K,0) if K < spot else 0:.0f}):")
        for r in results:
            if r["strike"] == K and r["scenario"] == "base_t2_T7_s030":
                evch = r.get("mispricing_vs_chooser")
                print(f"  chooser_mid={r['chooser_mid']:.2f}  "
                      f"chooser_floor={r['chooser_floor']:.2f}  "
                      f"market_mid={r['market_mid']}  "
                      f"edge_vs_chooser={evch:+.2f if evch else 'n/a'}")


if __name__ == "__main__":
    run_analysis()
