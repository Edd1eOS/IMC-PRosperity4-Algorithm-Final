# Round 4 Diagnostics Report

Generated from research on Days 1-3 price/trade data.
**INFERENCE** tags indicate conclusions not directly provable from data.

---

## 1. Who Is the Most Valuable Counterparty Per Product?

### VELVETFRUIT_EXTRACT
| Counterparty | Role | Net Flow | h=1 Forward PnL | Assessment |
|---|---|---|---|---|
| Mark 01 | Informed buyer | +42 | +2.87/unit | Follow on buys |
| Mark 67 | Major buyer | +1510 | ~+2/unit | Directional long |
| Mark 22 | Heavy seller | -551 | mixed | Liquidity provider |
| Mark 49 | Major seller | -956 | -1.02/unit | Consistent short |
| Mark 55 | Net seller | -43 | ~neutral | Market maker |
| Mark 14 | Nearly neutral | -2 | +2.32/unit | Informed trader |

**Best counterparties to follow:** Mark 01 (h=1 PnL +2.87), Mark 14 (h=1 PnL +2.32), Mark 67 (large directional buyer).

**Key alpha signal:** When Mark 01 or Mark 67 buys VELVET, lean bullish. When Mark 49 sells, lean bearish (consistent short seller with negative forward PnL).

### HYDROGEL_PACK
| Counterparty | h=1 PnL | Direction |
|---|---|---|
| Mark 14 | +8.11 | Buys low, informed |
| Mark 38 | -7.98 | Buys high, consistently wrong |

**INFERENCE:** Mark 14 is the informed HYDROGEL market maker. Mark 38 appears to be consistently paying the wrong prices. Mark 14's edge = ~8 ticks = roughly half the spread (mean spread ~16). Our strategy: shadow Mark 14's direction.

### VEV Options
| Counterparty | Dominant Flow | Assessment |
|---|---|---|
| Mark 01 | Massive buyer of high strikes | **Believed to hold chooser information** |
| Mark 22 | Seller of all high strikes | Treats them as worthless OTM calls |
| Mark 14 | Buyer of VEV_4000 | Knows deep ITM call value |
| Mark 38 | Seller of VEV_4000 | Consistently loses on timing |

**Critical finding:** Mark 01 bought **1,105 units of VEV_6000 at price 0** and **1,105 units of VEV_6500 at price 0** from Mark 22. These were acquired FOR FREE. This is either noise or Mark 01 has information that these are chooser options with significant put value. Given Mark 01's consistent +2.87/unit edge on VELVET (another informed trade), we favor the latter.

---

## 2. Which Strikes Are Best to Hold PRE_CHOICE?

Priority ranking based on chooser_floor vs market_ask edge:

| Strike | Chooser Floor | Chooser Mid (s=30%) | Market Ask | Floor Edge | Model Edge |
|---|---|---|---|---|---|
| VEV_6500 | 1252 | 1252 | 1 | **+1251** | **+1251** |
| VEV_6000 | 752 | 752 | 1 | **+751** | **+751** |
| VEV_5500 | 252 | 267 | 8 | **+244** | **+259** |
| VEV_5400 | 152 | 190 | 17 | **+135** | **+173** |
| VEV_5300 | 52 | 141 | 48 | **+4** | **+93** |
| VEV_5200 | 48 | 139 | 97 | -49 | **+42** |
| VEV_5100 | 148 | 185 | 167 | -19 | **+18** |

**Recommendation:** VEV_6500, VEV_6000, VEV_5500, VEV_5400 are the highest-priority buys. Even the most conservative estimate (chooser floor using only intrinsic) shows massive positive edge for the top 4 strikes.

**INFERENCE:** The market treats all of these as vanilla OTM calls (worth 0). If H1 is correct, they should be priced as choosers with significant put option value. The potential alpha from max positions:
- VEV_6500: 300 × 1251 = **375,300**
- VEV_6000: 300 × 751 = **225,300**
- VEV_5500: 300 × 244 = **73,200**
- VEV_5400: 300 × 135 = **40,500**
- **Total potential: ~714,300** (before considering lower strikes)

This is a high-conviction binary bet on H1 being correct.

---

## 3. What Does a Naive Vanilla-Call Model Miss?

### Value Risk
The vanilla-call model prices VEV_6000 at ~0 (deep OTM). A chooser model prices it at:
- **Conservative floor:** 752 (just put intrinsic)
- **Mid (sigma=30%):** 752
- **Difference:** 752 per unit

For a 300-unit position: **225,600 unrealized value completely missed**.

### Delta Risk
Vanilla-call delta for VEV_6000 = 0 (doesn't change with spot). This means:
- No VELVET hedge needed (vanilla model)
- Chooser delta for VEV_6000 = -1.0 (full put delta at this deep ITM put)
- With 300 units: -300 VELVET equivalent exposure

**If we don't hedge the chooser delta, a 100pt VELVET rally (5248→5348) reduces our put value by ~100 per unit × 300 units = 30,000 loss.** Vanilla model misses this entirely.

### Regime Risk
- Vanilla model: no concept of "choice event"
- Chooser model: value changes discontinuously at t_c
- At choice: we need to re-hedge immediately

---

## 4. What Happens If Phase Is Detected Wrong?

### Case A: We're in PRE_CHOICE but system says POST_CHOICE_CALL
- We stop buying high-strike options
- We miss the primary alpha source
- **Lost PnL: up to 714,300 per round**

### Case B: We're in POST_CHOICE_CALL but system says PRE_CHOICE
- We hold all our high-strike positions thinking they're choosers
- At expiry, they're deep OTM calls = 0
- **Lost PnL: whatever we paid for them** (~178,000 in costs for all strikes)
- The bet is wrong but the cost is bounded

**Conclusion: Being wrong in PRE_CHOICE direction is much more expensive than being wrong in POST_CHOICE direction.** Default to PRE_CHOICE with conservative exit conditions.

### Case C: POST_CHOICE_PUT (chose as puts)
- Our high-strike positions GAIN massive value
- VEV_6000 with 300 units goes from mark=0.5 to intrinsic=752
- This is the **maximum upside scenario**

---

## 5. Stable vs Ceiling Version Differences

| Parameter | Stable | Ceiling |
|---|---|---|
| take_edge_min | 2.0 | 1.0 |
| intercept_min_edge | 1.0 | 0.5 |
| high_strike_take_mult | 1.0 | 0.5 |
| chooser_floor_min_edge | 1.0 | 0.5 |
| max_per_strike | 150 | 250 |
| max_total_delta | 100 | 180 |
| risk_profile | balanced | aggressive |

**Stable:** Requires larger edge before trading. Reaches position limits more slowly. Won't chase market trades with tiny edge. **Expected PnL range:** Conservative but less variance in the vanilla-call scenario.

**Ceiling:** Takes positions aggressively, intercepts more market trades, allows larger per-strike concentration. **Expected PnL range:** Much higher ceiling if H1 is correct, higher loss if H1 is wrong.

**Recommendation:** Run stable in all rounds until we see evidence of POST_CHOICE transition (price jumps, flow reversal). The cost of a position in VEV_6000 is 300 (300 units × ask=1), not 225,300. Even under stable, the risk-reward is strongly positive if H1 holds.

---

## 6. Most Fragile Assumption and Fallback

### Most Fragile: H1 (Chooser Structure)
**Why fragile:** The entire thesis depends on VEV options being choosers. If they were resolved as calls at the start of the round (i.e., we're already POST_CHOICE_CALL), then all our "floor edge" analysis is wrong and we're just buying deep OTM calls worth 0.

**Evidence FOR H1:**
1. Mark 01 consistently buys VEV_6000/6500 at price 0 (1105 units each) - informed trader
2. Market volumes still active (Mark 22 still selling, Mark 01 still buying)
3. Mark 01 has +2.87/unit forward PnL on VELVET - they appear informed
4. Cross-sectional price shape shows VEV_5300 at 47 ≈ vanilla_call (63) not chooser_mid (141)
   → **INFERENCE:** Market may not have priced in chooser yet

**Evidence AGAINST H1 / UNCERTAINTY:**
1. VEV_6000 and VEV_6500 have been stuck at 0.5 for all 3 days
2. We don't know competition rules with certainty
3. No public documentation of "chooser" structure in official problem statement

**Fallback plan:**
1. Buy VEV_6000/6500 at ask=1 (cost: 600 total for 300 each) - extremely cheap
2. For VEV_5400/5500: buy up to 100 units (not max)
3. If prices jump in any strike: update phase detector immediately
4. If end-of-round settlement shows call payoff: stop buying high strikes

**The key risk is very small:** We can buy 300+300 = 600 units of VEV_6000 and VEV_6500 for just 600 total (at ask=1). If we're wrong, we lose 600. If we're right, we gain 225,600 + 375,600 = 601,200. Risk-reward ratio = **1000:1**. Even at 0.1% probability of being right, the expected value is still positive.

---

## 7. Top 5 Ablation Priorities

| # | Description | Key Change | What We Learn |
|---|---|---|---|
| 1 | baseline_stable | Default config | Baseline PnL without any assumptions changes |
| 2 | ceiling_aggressive | CEILING_OVERRIDES | How much more alpha from aggressive taking |
| 3 | sigma_sensitivity | sigma=0.20 vs 0.40 | How sensitive fair values are to vol assumption |
| 4 | short_choice_time | choice_time_days=0.5 | Extreme scenario: choice is imminent |
| 5 | no_intercept | intercept_min_edge=999 | How much alpha comes from market trade interception |

---

## 8. Key Empirical Facts (Directly Observed)

- VEV_6000 and VEV_6500 traded at price 0 for entire days 1-3
- Mark 01 is the sole buyer of VEV_6000/6500; Mark 22 is the sole seller
- Mark 01 accumulated **1,105 units of VEV_6000** and **1,105 units of VEV_6500** at zero cost
- Mark 22 accumulated **-1,105 units** (short) of each
- Mark 14 is the best HYDROGEL market maker (+8.11/unit edge at h=1)
- Mark 38 is consistently wrong on HYDROGEL timing (-7.98/unit)
- VELVET sigma from ATM IV: approximately 18-30% depending on time assumption
- VELVET mean: 5248, std: 18, range: 5191-5300

---

*Report generated by round4_loader.py and chooser_scenarios.py from local CSV data.*
*All "INFERENCE" sections are hypotheses not directly provable from 3-day data alone.*
