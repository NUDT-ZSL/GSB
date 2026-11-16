"""Dual-threshold hysteresis state machine.

States: SILENCE -> SUSPECT -> SPEECH -> TRAILING -> (SILENCE | SPEECH).
Every transition depends only on the current state and the current frame's
features. Ties at threshold boundaries use fixed rule ordering, so output is
fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass

SILENCE = "SILENCE"
SUSPECT = "SUSPECT"
SPEECH = "SPEECH"
TRAILING = "TRAILING"
STATES = (SILENCE, SUSPECT, SPEECH, TRAILING)


@dataclass(frozen=True)
class Thresholds:
    low: float          # adaptive noise-floor based low energy threshold
    high: float         # configured high energy threshold
    release: float      # high - hysteresis, release boundary for trailing state
    zcr: float
    flatness: float
    suspect_frames: int
    trailing_frames: int


def build_thresholds(noise_floor: float, config) -> Thresholds:
    return Thresholds(
        low=noise_floor + config.hysteresis,
        high=config.energy_high_threshold,
        release=config.energy_high_threshold - config.hysteresis,
        zcr=config.zcr_threshold,
        flatness=config.flatness_threshold,
        suspect_frames=config.suspect_frames,
        trailing_frames=config.trailing_frames,
    )


def _classify_energy(row: dict, th: Thresholds) -> tuple[bool, bool]:
    """Return (strong_evidence, weak_evidence) with fixed priority on ties."""
    energy = row["log_energy"]
    # Rule P1 (highest): at/above high threshold is always strong evidence.
    strong = energy >= th.high
    # Rule P2: above adaptive low threshold AND low flatness (tonal) is strong too.
    tonal = row["flatness"] <= th.flatness
    if not strong and energy >= th.low and tonal:
        strong = True
    # Rule P3: above low threshold with low ZCR counts as weak evidence.
    weak = (not strong) and energy >= th.low and row["zcr"] <= th.zcr
    return strong, weak


@dataclass
class MachineState:
    name: str = SILENCE
    suspect_count: int = 0
    trailing_count: int = 0
    speech_start: int | None = None


def transition(state: MachineState, row: dict, th: Thresholds) -> tuple[str, str]:
    """Return (new_state_name, decision_reason). Pure function of state + frame."""
    strong, weak = _classify_energy(row, th)
    active = strong or weak
    current = state.name

    if current == SILENCE:
        if strong:
            return SUSPECT, "STRONG_EVIDENCE_ENTER_SUSPECT"
        if weak:
            return SUSPECT, "WEAK_EVIDENCE_ENTER_SUSPECT"
        return SILENCE, "REMAIN_SILENCE_LOW_ENERGY"

    if current == SUSPECT:
        state.suspect_count += 1
        if strong:
            return SPEECH, "STRONG_EVIDENCE_CONFIRM_SPEECH"
        if state.suspect_count >= th.suspect_frames:
            return SPEECH, "SUSPECT_PERSISTENCE_CONFIRM_SPEECH"
        if not active:
            return SILENCE, "EVIDENCE_LOST_BACK_TO_SILENCE"
        return SUSPECT, "WAIT_SUSPECT_PERSISTENCE"

    if current == SPEECH:
        if strong:
            return SPEECH, "REMAIN_SPEECH_STRONG"
        if weak:
            return SPEECH, "REMAIN_SPEECH_WEAK"
        return TRAILING, "ENERGY_DROP_ENTER_TRAILING"

    # TRAILING
    state.trailing_count += 1
    if strong:
        return SPEECH, "STRONG_EVIDENCE_RESUME_SPEECH"
    if weak:
        return SPEECH, "WEAK_EVIDENCE_RESUME_SPEECH"
    if state.trailing_count >= th.trailing_frames:
        return SILENCE, "TRAILING_TIMEOUT_CLOSE_SPEECH"
    return TRAILING, "WAIT_TRAILING_TIMEOUT"


def run_state_machine(feature_rows: list[dict], th: Thresholds) -> list[dict]:
    state = MachineState()
    trace: list[dict] = []
    for row in feature_rows:
        previous = state.name
        new_name, reason = transition(state, row, th)
        if new_name != previous:
            if new_name == SPEECH and state.speech_start is None:
                state.speech_start = row["frame"]
            if new_name == SPEECH:
                state.suspect_count = 0
            if new_name == TRAILING:
                state.trailing_count = 0
            if new_name == SILENCE:
                state.speech_start = None
                state.suspect_count = 0
                state.trailing_count = 0
            if new_name == SUSPECT:
                state.suspect_count = 1 if previous == SILENCE else state.suspect_count
                state.trailing_count = 0
            state.name = new_name
        trace.append({
            "frame": row["frame"],
            "timestamp_ms": row["timestamp_ms"],
            "state": state.name,
            "previous_state": previous,
            "reason": reason,
        })
    return trace
