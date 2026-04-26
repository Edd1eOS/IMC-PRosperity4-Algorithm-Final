"""
phase_transition_detector.py - Detect PRE_CHOICE / AT_CHOICE / POST_CHOICE regime.

No assumptions about when choice happens. Three independent signals:

  S1. High-strike price stability
      VEV_6000/6500 stuck at 0.5 forever -> PRE_CHOICE (still priced as calls)
      Any discontinuous jump up -> AT_CHOICE or early POST_CHOICE as puts

  S2. Mark 01/Mark 22 flow persistence
      Consistent one-sided flow (Mark 01 buying, Mark 22 selling) -> PRE_CHOICE
      Flow reversal or stops -> potential state change

  S3. Cross-sectional shape test
      In PRE_CHOICE chooser: low strikes overpriced vs call, high strikes underpriced
      In POST_CHOICE (chose call): shape follows vanilla call
      Test: does VEV_5300/VEV_5400 ratio match call ratio or chooser ratio?

  S4. Regime jump detector
      Sudden large price change in any VEV -> AT_CHOICE event

Output:
  phase_state: "PRE_CHOICE" | "AT_CHOICE" | "POST_CHOICE_CALL" | "POST_CHOICE_PUT"
  phase_confidence: [0, 1]
  trigger_ts: timestamp of last regime change
"""

from collections import deque
import math

# ── Constants ─────────────────────────────────────────────────────────────────
HIGH_STRIKES = [5500, 6000, 6500]      # these are most sensitive to chooser regime
MID_STRIKES  = [5200, 5300, 5400]
JUMP_THRESHOLD = 5.0                   # absolute price change to flag as jump
STUCK_WINDOW = 200                     # ticks of consecutive same-price -> stable
FLOW_WINDOW  = 100                     # ticks for rolling flow analysis
MIN_SIGNAL_N = 3                       # minimum observations per signal


class PhaseTransitionDetector:
    """
    Online regime detector. Call update() each tick.
    query() returns current phase assessment.
    """

    def __init__(self,
                 jump_threshold=JUMP_THRESHOLD,
                 stuck_window=STUCK_WINDOW,
                 flow_window=FLOW_WINDOW,
                 override_phase=None):
        """
        override_phase: if set to one of the phase strings, hard-override all signals.
        """
        self.jump_threshold = jump_threshold
        self.stuck_window   = stuck_window
        self.flow_window    = flow_window
        self.override_phase = override_phase

        # Price history per symbol: deque of (ts, mid)
        self._price_hist  = {f"VEV_{k}": deque(maxlen=stuck_window)
                             for k in [5300,5400,5500,6000,6500]}
        self._velvet_hist = deque(maxlen=stuck_window)

        # Trade flow history: deque of (ts, buyer, seller, symbol, qty)
        self._flow_hist   = deque(maxlen=flow_window)

        # Current phase estimate
        self._phase            = "PRE_CHOICE"
        self._phase_confidence = 0.5
        self._trigger_ts       = 0
        self._current_ts       = 0

        # S1 state: track consecutive ticks each high-strike is stable at 0.5
        self._stuck_count      = {k: 0 for k in HIGH_STRIKES}
        self._prev_mids        = {}

        # S4: jump detection
        self._jump_events      = []  # list of (ts, symbol, magnitude)

    # ── Update ────────────────────────────────────────────────────────────────

    def update_prices(self, ts, mids):
        """
        mids: dict {product: mid_price}
        Call once per timestamp with full mid snapshot.
        """
        self._current_ts = ts

        velvet = mids.get("VELVETFRUIT_EXTRACT")
        if velvet:
            self._velvet_hist.append((ts, velvet))

        for k in [5300, 5400, 5500, 6000, 6500]:
            sym = f"VEV_{k}"
            mid = mids.get(sym)
            if mid is None:
                continue

            self._price_hist[sym].append((ts, mid))

            # S1: stuck at 0.5 detection (VEV_6000/6500)
            if k in HIGH_STRIKES:
                if mid == 0.5:
                    self._stuck_count[k] += 1
                else:
                    self._stuck_count[k] = 0

            # S4: jump detection
            if sym in self._prev_mids:
                delta = abs(mid - self._prev_mids[sym])
                if delta >= self.jump_threshold:
                    self._jump_events.append((ts, sym, mid - self._prev_mids[sym]))

            self._prev_mids[sym] = mid

        self._recompute_phase()

    def update_trade(self, ts, buyer, seller, symbol, price, quantity):
        """Record a market trade for flow analysis."""
        self._flow_hist.append((ts, buyer, seller, symbol, price, quantity))

    # ── Phase Computation ─────────────────────────────────────────────────────

    def _recompute_phase(self):
        """Combine all signals into a single phase estimate."""
        if self.override_phase:
            self._phase = self.override_phase
            self._phase_confidence = 1.0
            return

        s1 = self._signal_stuck_at_zero()
        s2 = self._signal_flow_consistency()
        s3 = self._signal_cross_sectional_shape()
        s4 = self._signal_jump_event()

        # Evidence for PRE_CHOICE
        pre_evidence  = s1["pre"] * 0.4 + s2["pre"] * 0.3 + s3["pre"] * 0.3

        # Evidence for POST_CHOICE
        post_evidence = s1["post"] * 0.4 + s2["post"] * 0.2 + s4["jump"] * 0.4

        total = pre_evidence + post_evidence
        if total < 0.01:
            self._phase = "PRE_CHOICE"
            self._phase_confidence = 0.5
            return

        norm_pre  = pre_evidence  / total
        norm_post = post_evidence / total

        if norm_pre > norm_post and norm_pre > 0.55:
            self._phase = "PRE_CHOICE"
            self._phase_confidence = norm_pre
        elif norm_post > 0.7 and s4["post_call"]:
            self._phase = "POST_CHOICE_CALL"
            self._phase_confidence = norm_post
        elif norm_post > 0.7:
            self._phase = "POST_CHOICE_PUT"
            self._phase_confidence = norm_post
        elif s4["jump"] > 0.5:
            self._phase = "AT_CHOICE"
            self._phase_confidence = s4["jump"]
        else:
            # Ambiguous: stay with current phase but lower confidence
            self._phase_confidence = max(0.3, self._phase_confidence * 0.95)

    def _signal_stuck_at_zero(self):
        """
        S1: If high-strike prices are stuck at 0.5 for a long time
        -> strong evidence of PRE_CHOICE (priced as worthless OTM calls).
        If any of them jump -> evidence of phase change.
        """
        stuck_n = sum(1 for k in HIGH_STRIKES
                      if self._stuck_count[k] >= self.stuck_window // 4)
        pre_score = stuck_n / len(HIGH_STRIKES)

        # Jump in high strikes -> post_choice
        recent_jumps = [e for e in self._jump_events
                        if e[1] in [f"VEV_{k}" for k in HIGH_STRIKES]]
        post_score = min(1.0, len(recent_jumps) / 3.0)

        return {"pre": pre_score, "post": post_score}

    def _signal_flow_consistency(self):
        """
        S2: Mark 01 consistently buying OTM options from Mark 22
        -> suggests PRE_CHOICE (Mark 01 is accumulating chooser value).
        """
        if not self._flow_hist:
            return {"pre": 0.5, "post": 0.5}

        m01_buy_otm = 0
        m22_sell_otm = 0
        total_otm = 0
        otm_syms = {"VEV_5400", "VEV_5500", "VEV_6000", "VEV_6500"}

        for ts, buyer, seller, symbol, price, qty in self._flow_hist:
            if symbol in otm_syms:
                total_otm += qty
                if buyer == "Mark 01":
                    m01_buy_otm += qty
                if seller == "Mark 22":
                    m22_sell_otm += qty

        if total_otm < MIN_SIGNAL_N:
            return {"pre": 0.5, "post": 0.5}

        # Strong one-sided flow = PRE_CHOICE accumulation
        consistency = (m01_buy_otm / total_otm) * 0.5 + (m22_sell_otm / total_otm) * 0.5
        pre_score  = consistency
        post_score = 1 - consistency

        return {"pre": pre_score, "post": post_score}

    def _signal_cross_sectional_shape(self):
        """
        S3: Does the cross-sectional price shape match chooser or vanilla call?

        Chooser: price(5300) / price(5400) should be ~chooser_5300/chooser_5400
        Vanilla: price(5300) / price(5400) should be ~call_5300/call_5400

        Approximate comparison: if ratio is much higher than call ratio -> chooser.
        """
        if not self._price_hist["VEV_5300"] or not self._price_hist["VEV_5400"]:
            return {"pre": 0.5, "post": 0.5}

        mid_5300 = self._price_hist["VEV_5300"][-1][1]
        mid_5400 = self._price_hist["VEV_5400"][-1][1]
        velvet = self._velvet_hist[-1][1] if self._velvet_hist else 5248.0

        if mid_5400 <= 0:
            return {"pre": 0.5, "post": 0.5}

        observed_ratio = mid_5300 / mid_5400

        # For vanilla call with sigma=0.30, T=7days: rough ratio
        # call(5248, 5300, 7d, 0.30) / call(5248, 5400, 7d, 0.30) ~ 47/17 ~ 2.76
        # For chooser with t_c=2d: chooser(5300) includes put intrinsic 52
        #   chooser(5300) ~ 47 + 20 = 67, chooser(5400) ~ 17 + 152 = 169
        #   ratio = 67/169 = 0.40  -- much LOWER ratio for chooser at high strikes!
        # So if observed_ratio is LOW (high strike has relatively high value) -> chooser

        vanilla_ratio_approx  = 2.76   # call-only model
        chooser_ratio_approx  = 0.40   # chooser model (high strikes underpriced by call model)

        # Which model fits better?
        diff_vanilla  = abs(observed_ratio - vanilla_ratio_approx)
        diff_chooser  = abs(observed_ratio - chooser_ratio_approx)

        if diff_vanilla + diff_chooser < 0.01:
            return {"pre": 0.5, "post": 0.5}

        chooser_fit = diff_vanilla / (diff_vanilla + diff_chooser)  # closer to chooser = higher
        pre_score  = chooser_fit
        post_score = 1 - chooser_fit

        return {"pre": pre_score, "post": post_score}

    def _signal_jump_event(self):
        """
        S4: A sudden large price move in ANY VEV -> AT_CHOICE or POST_CHOICE transition.
        """
        recent = [e for e in self._jump_events
                  if self._current_ts - e[0] < 500]  # within 500 ticks

        if not recent:
            return {"jump": 0.0, "post_call": False}

        max_jump = max(abs(e[2]) for e in recent)
        jump_score = min(1.0, max_jump / 20.0)

        # If jumps are positive in high strikes -> became valuable -> post as puts
        pos_jumps = sum(1 for e in recent
                        if e[1] in [f"VEV_{k}" for k in HIGH_STRIKES] and e[2] > 0)
        post_call = pos_jumps == 0  # no positive jumps in high strikes -> chose call

        return {"jump": jump_score, "post_call": post_call}

    # ── Query ─────────────────────────────────────────────────────────────────

    def query(self):
        """Return current phase state dict."""
        return {
            "phase": self._phase,
            "confidence": self._phase_confidence,
            "trigger_ts": self._trigger_ts,
            "current_ts": self._current_ts,
            "stuck_counts": dict(self._stuck_count),
            "jump_events": list(self._jump_events[-5:]),  # last 5 jumps
        }

    def is_pre_choice(self):
        return self._phase == "PRE_CHOICE"

    def is_post_choice(self):
        return self._phase.startswith("POST_CHOICE")

    def is_post_call(self):
        return self._phase == "POST_CHOICE_CALL"

    def is_post_put(self):
        return self._phase == "POST_CHOICE_PUT"


# ── Backtest over historical data ─────────────────────────────────────────────

def run_backtest_detection(prices_raw, trades_raw, verbose=False):
    """
    Run the detector over historical data and report phase estimates over time.
    Returns list of (global_ts, phase, confidence) tuples.
    """
    # Build ts-indexed data
    from collections import defaultdict
    ts_mids = defaultdict(dict)
    for row in prices_raw:
        gts = row["global_ts"]
        if row["mid_price"] is not None:
            ts_mids[gts][row["product"]] = row["mid_price"]

    ts_trades = defaultdict(list)
    for t in trades_raw:
        ts_trades[t["global_ts"]].append(t)

    detector = PhaseTransitionDetector()
    results = []

    for gts in sorted(ts_mids.keys()):
        mids = ts_mids[gts]
        detector.update_prices(gts, mids)

        for t in ts_trades.get(gts, []):
            detector.update_trade(
                gts, t["buyer"], t["seller"], t["symbol"],
                t["price"], t["quantity"]
            )

        q = detector.query()
        results.append((gts, q["phase"], q["confidence"]))

        if verbose and gts % 100_000 == 0:
            print(f"ts={gts}: phase={q['phase']}  conf={q['confidence']:.3f}  "
                  f"jumps={q['jump_events']}")

    return results


def run_report():
    try:
        from research.round4_loader import load_prices_raw, load_trades_raw
        prices_raw = load_prices_raw()
        trades_raw = load_trades_raw()
    except Exception as e:
        print(f"Could not load data: {e}")
        return

    print("Running phase detection over historical data...")
    results = run_backtest_detection(prices_raw, trades_raw)

    # Summarize phase distribution
    from collections import Counter
    phase_counts = Counter(r[1] for r in results)
    print(f"\nPhase distribution:")
    for ph, cnt in sorted(phase_counts.items()):
        print(f"  {ph}: {cnt} ({cnt/len(results)*100:.1f}%)")

    # Show transitions
    transitions = []
    prev_phase = None
    for ts, phase, conf in results:
        if phase != prev_phase:
            transitions.append((ts, phase, conf))
            prev_phase = phase

    print(f"\nPhase transitions ({len(transitions)} total):")
    for ts, phase, conf in transitions[:20]:
        day = ts // 1_000_000
        inner_ts = ts % 1_000_000
        print(f"  Day {day} ts={inner_ts}: -> {phase} (conf={conf:.3f})")


if __name__ == "__main__":
    run_report()
