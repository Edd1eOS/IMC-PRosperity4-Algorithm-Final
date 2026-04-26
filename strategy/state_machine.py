"""
strategy/state_machine.py - Phase state machine for chooser lifecycle.

Manages:
  - PRE_CHOICE / AT_CHOICE / POST_CHOICE transitions
  - External override capability
  - Per-phase parameter switching
  - Trade logic routing
"""

from collections import deque

# ── Phase constants ───────────────────────────────────────────────────────────
PRE_CHOICE       = "PRE_CHOICE"
AT_CHOICE        = "AT_CHOICE"
POST_CHOICE_CALL = "POST_CHOICE_CALL"
POST_CHOICE_PUT  = "POST_CHOICE_PUT"
VALID_PHASES = {PRE_CHOICE, AT_CHOICE, POST_CHOICE_CALL, POST_CHOICE_PUT}


class PhaseMachine:
    """
    Unified phase state manager.

    State transition sources (in priority order):
      1. override_phase: hard external override (e.g., from config or manual flag)
      2. detector: PhaseTransitionDetector output
      3. default: PRE_CHOICE

    Once POST_CHOICE is reached, it is IRREVERSIBLE (can't go back to PRE).
    AT_CHOICE is transient (can move to POST_CHOICE_CALL or POST_CHOICE_PUT).
    """

    def __init__(self, config):
        self.config = config
        self._phase = PRE_CHOICE
        self._confidence = 0.5
        self._override = config.get("phase_override", None)
        self._history = deque(maxlen=50)
        self._current_ts = 0

    def update_from_detector(self, detector_query):
        """
        Accept output from PhaseTransitionDetector.query().
        Only transition forward (PRE -> AT -> POST), never backwards.
        """
        if self._override:
            return  # hard override ignores detector

        new_phase  = detector_query.get("phase", PRE_CHOICE)
        confidence = detector_query.get("confidence", 0.5)
        threshold  = self.config.get("phase_confidence_threshold", 0.65)

        if confidence < threshold:
            return  # not confident enough to switch

        # Enforce irreversible forward progression
        if not self._can_transition_to(new_phase):
            return

        if new_phase != self._phase:
            self._history.append((self._current_ts, self._phase, new_phase, confidence))
            self._phase = new_phase

        self._confidence = confidence

    def manual_override(self, phase):
        """Force phase. Used for debugging or when we know choice has been made."""
        assert phase in VALID_PHASES, f"Invalid phase: {phase}"
        self._override = phase
        self._phase = phase
        self._confidence = 1.0

    def clear_override(self):
        self._override = None

    def _can_transition_to(self, new_phase):
        """
        Phase progression rules:
          PRE_CHOICE -> AT_CHOICE or POST_CHOICE_*
          AT_CHOICE  -> POST_CHOICE_CALL or POST_CHOICE_PUT
          POST_CHOICE_* -> never go back
        """
        if self._phase == PRE_CHOICE:
            return new_phase in {AT_CHOICE, POST_CHOICE_CALL, POST_CHOICE_PUT}
        elif self._phase == AT_CHOICE:
            return new_phase in {POST_CHOICE_CALL, POST_CHOICE_PUT}
        else:
            # Already post choice: only allow clarification between call/put
            return (self._phase == POST_CHOICE_CALL and new_phase == POST_CHOICE_PUT) or \
                   (self._phase == POST_CHOICE_PUT  and new_phase == POST_CHOICE_CALL)

    def tick(self, ts):
        self._current_ts = ts

    @property
    def phase(self):
        if self._override:
            return self._override
        return self._phase

    @property
    def confidence(self):
        return self._confidence

    def is_pre_choice(self):
        return self.phase == PRE_CHOICE

    def is_post_choice(self):
        return self.phase in {POST_CHOICE_CALL, POST_CHOICE_PUT}

    def is_post_call(self):
        return self.phase == POST_CHOICE_CALL

    def is_post_put(self):
        return self.phase == POST_CHOICE_PUT

    def is_at_choice(self):
        return self.phase == AT_CHOICE

    def phase_params(self):
        """
        Return phase-specific parameter overrides for models.
        In PRE_CHOICE: use chooser pricing, be aggressive on high strikes.
        In POST_CHOICE: use vanilla pricing, focus on delta.
        """
        if self.is_pre_choice():
            return {
                "option_model": "chooser",
                "high_strike_priority": True,
                "use_chooser_floor": True,
                "delta_hedge_aggressive": False,
                "take_edge_mult": self.config.get("pre_take_mult", 1.0),
            }
        elif self.is_post_call():
            return {
                "option_model": "vanilla_call",
                "high_strike_priority": False,
                "use_chooser_floor": False,
                "delta_hedge_aggressive": True,
                "take_edge_mult": self.config.get("post_take_mult", 1.5),
            }
        elif self.is_post_put():
            return {
                "option_model": "vanilla_put",
                "high_strike_priority": True,  # high strike puts are now ITM!
                "use_chooser_floor": False,
                "delta_hedge_aggressive": True,
                "take_edge_mult": self.config.get("post_take_mult", 1.5),
            }
        else:  # AT_CHOICE
            return {
                "option_model": "chooser",
                "high_strike_priority": True,
                "use_chooser_floor": True,
                "delta_hedge_aggressive": False,
                "take_edge_mult": self.config.get("at_take_mult", 0.5),  # cautious
            }

    def summary(self):
        return {
            "phase": self.phase,
            "confidence": self._confidence,
            "override": self._override,
            "transitions": list(self._history),
            "ts": self._current_ts,
        }


# ── Strike priority router ────────────────────────────────────────────────────

class StrikePriorityRouter:
    """
    Decides which VEV strikes to trade and in what order given current phase.

    PRE_CHOICE:
      Priority 1 (aggressive take): 5300, 5400, 5500, 6000, 6500 (chooser floor >> ask)
      Priority 2 (passive): 5000, 5100, 5200 (near ATM)
      Priority 3 (skip or minimal): 4000, 4500 (deep ITM, less chooser premium)

    POST_CHOICE_CALL:
      Priority 1: ITM calls (4000, 4500, 5000) for potential take
      Priority 2: Near ATM (5100-5300) for making
      Skip high OTM calls (5400+)

    POST_CHOICE_PUT:
      Priority 1: high strikes (5400-6500) now ITM puts
      Priority 2: near ATM (5100-5300)
      Priority 3: low strikes (deep OTM puts)
    """

    STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]

    def ordered_strikes(self, phase):
        if phase == PRE_CHOICE:
            # High strikes first (biggest chooser edge), then ATM, then deep ITM
            return [5300, 5400, 5500, 6000, 6500, 5200, 5100, 5000, 4500, 4000]
        elif phase == POST_CHOICE_CALL:
            # ITM calls first (well-defined value), then ATM, skip deep OTM
            return [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
        elif phase == POST_CHOICE_PUT:
            # High-strike ITM puts first
            return [6500, 6000, 5500, 5400, 5300, 5200, 5100, 5000, 4500, 4000]
        else:  # AT_CHOICE
            # All strikes, ATM first (highest gamma)
            return [5200, 5100, 5300, 5000, 5400, 4500, 5500, 4000, 6000, 6500]

    def should_take(self, strike, phase, edge):
        """
        True if we should aggressively take at this strike given edge.
        edge = our_fv - market_ask (positive = we see it as cheap).
        """
        min_edge = 2.0  # default

        if phase == PRE_CHOICE and strike >= 5300:
            # High strikes: lower threshold because chooser floor > market
            min_edge = 1.0
        elif phase == POST_CHOICE_PUT and strike >= 5300:
            # These are now deep ITM puts: very aggressive
            min_edge = 0.5
        elif phase == POST_CHOICE_CALL and strike >= 5500:
            # Deep OTM calls: skip, edge must be huge
            min_edge = 10.0

        return edge >= min_edge

    def should_skip(self, strike, phase):
        """True if we should not trade this strike at all in current phase."""
        if phase == POST_CHOICE_CALL and strike >= 6000:
            return True  # deep OTM calls worthless
        return False
