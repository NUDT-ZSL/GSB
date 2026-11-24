from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import SchemaError
from .model import Decision, FrameEvent, Manifest, ReplayResult, ScreenState


VERDICT_ADOPTED = "adopted"
VERDICT_LATE = "late_dropped"
VERDICT_ROLLBACK = "generation_rollback"


def _initial_state(manifest: Manifest, screen_id: str) -> ScreenState:
    _session, screen = manifest.screen_index[screen_id]
    return ScreenState(screen_id=screen_id, canonical_id=screen.canonical_id, session_id=_session.session_id)


def _rebuild_from_keyframe(
    events: List[FrameEvent],
    screen_id: str,
    target_generation: int,
    through_index: int,
    manifest: Manifest,
    discarded_event_seqs: Optional[set[int]] = None,
) -> Dict[str, Any]:
    discarded_event_seqs = discarded_event_seqs or set()
    keyframe_index: Optional[int] = None
    for index in range(through_index, -1, -1):
        event = events[index]
        if (
            event.screen_id == screen_id
            and event.generation == target_generation
            and event.is_keyframe
            and event.event_seq not in discarded_event_seqs
        ):
            keyframe_index = index
            break
    if keyframe_index is None:
        raise SchemaError(
            "generation rollback has no preceding keyframe to rebuild from",
            {"screen_id": screen_id, "generation": target_generation},
        )

    session, _screen = manifest.screen_index[screen_id]
    keyframe_seq: Optional[int] = None
    decoded_seq: Optional[int] = None
    position: Optional[int] = None
    replayed_count = 0

    for index in range(keyframe_index, through_index + 1):
        event = events[index]
        if event.screen_id != screen_id or event.generation != target_generation:
            continue
        if event.event_seq in discarded_event_seqs:
            continue
        replayed_count += 1
        if event.is_keyframe:
            keyframe_seq = event.frame_seq
        decoded_seq = event.frame_seq
        position = event.sampled_at_ms - session.start_logic_ms

    keyframe_event = events[keyframe_index]
    return {
        "generation": target_generation,
        "keyframe_seq": keyframe_seq,
        "decoded_frame_seq": decoded_seq,
        "play_position_ms": position,
        "replayed_frame_count": replayed_count,
        "rebuild_keyframe_seq": keyframe_event.frame_seq,
    }


def adjudicate_stream(manifest: Manifest, events: List[FrameEvent]) -> ReplayResult:
    """Replay events in their declared list order.

    Thread pools are intentionally not used here: all verdict ordering and
    counts must remain a pure function of manifest + ordered event content.
    """

    states: Dict[str, ScreenState] = {}
    state_logs: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, Dict[str, int]] = {}

    for session in manifest.sessions:
        for screen in session.screens:
            states[screen.screen_id] = ScreenState(
                screen_id=screen.screen_id,
                canonical_id=screen.canonical_id,
                session_id=session.session_id,
            )
            state_logs[screen.screen_id] = []
            counts[screen.screen_id] = {
                "total": 0,
                "adopted": 0,
                "late_dropped": 0,
                "generation_rollback": 0,
            }

    decisions: List[Decision] = []
    tolerance = manifest.late_tolerance_ms
    late_event_seqs: set[int] = set()

    for event_index, event in enumerate(events):
        state = states[event.screen_id]
        counts[event.screen_id]["total"] += 1
        generation_before = state.generation

        if state.clock_ms is None:
            clock_ms = event.sampled_at_ms
        else:
            clock_ms = max(state.clock_ms, event.sampled_at_ms)
        state.clock_ms = clock_ms
        late_by_ms = max(0, clock_ms - event.sampled_at_ms)

        rebuild_keyframe_seq: Optional[int] = None
        replayed_frame_count = 0

        if late_by_ms > tolerance:
            verdict = VERDICT_LATE
            reason = "sampled time is older than current clock beyond late tolerance"
            counts[event.screen_id]["late_dropped"] += 1
            late_event_seqs.add(event.event_seq)
        elif event.generation < state.generation:
            rebuilt = _rebuild_from_keyframe(
                events,
                event.screen_id,
                event.generation,
                event_index,
                manifest,
                late_event_seqs,
            )
            state.generation = rebuilt["generation"]
            state.keyframe_seq = rebuilt["keyframe_seq"]
            state.decoded_frame_seq = rebuilt["decoded_frame_seq"]
            state.play_position_ms = rebuilt["play_position_ms"]
            state.status = "playing"
            rebuild_keyframe_seq = rebuilt["rebuild_keyframe_seq"]
            replayed_frame_count = rebuilt["replayed_frame_count"]
            verdict = VERDICT_ROLLBACK
            reason = "event references an older session generation; rebuilt from nearest keyframe"
            counts[event.screen_id]["generation_rollback"] += 1
        else:
            state.generation = event.generation
            if event.is_keyframe:
                state.keyframe_seq = event.frame_seq
                reason = "keyframe resets decode baseline"
            elif event.generation > generation_before:
                reason = "adopted first frame of newer generation"
            else:
                reason = "in-order frame adopted"
            state.decoded_frame_seq = event.frame_seq
            state.play_position_ms = event.sampled_at_ms - manifest.session_map[event.session_id].start_logic_ms
            state.status = "playing"
            verdict = VERDICT_ADOPTED
            counts[event.screen_id]["adopted"] += 1

        decision = Decision(
            event_seq=event.event_seq,
            screen_id=event.screen_id,
            canonical_id=state.canonical_id,
            logic_ms=clock_ms,
            verdict=verdict,
            reason=reason,
            generation_before=generation_before,
            generation_after=state.generation,
            late_by_ms=late_by_ms,
            rebuild_keyframe_seq=rebuild_keyframe_seq,
            replayed_frame_count=replayed_frame_count,
            state_after=state.copy().__dict__,
        )
        decisions.append(decision)
        state_logs[event.screen_id].append(state.copy().__dict__)

    checkpoints = build_logic_axis(manifest, ReplayResult(state_logs, decisions, counts, {}))
    return ReplayResult(
        states=state_logs,
        decisions=decisions,
        counts_by_screen=counts,
        checkpoints=checkpoints,
    )


def build_logic_axis(manifest: Manifest, result: ReplayResult) -> Dict[str, List[Dict[str, Any]]]:
    """Return one state for each strictly advancing high-water logical time."""

    axis: Dict[str, List[Dict[str, Any]]] = {}
    for screen_id, log in result.states.items():
        entries: List[Dict[str, Any]] = []
        last_clock: Optional[int] = None
        for state in log:
            clock = state.get("clock_ms")
            if not isinstance(clock, int):
                continue
            if last_clock is None or clock > last_clock:
                entries.append(state)
                last_clock = clock
        axis[screen_id] = entries
    return axis


def state_at_logic_ms(
    manifest: Manifest,
    result: ReplayResult,
    screen_id: str,
    logic_ms: int,
) -> Dict[str, Any]:
    """Return the last adjudicated state whose high-water clock is <= logic_ms."""

    session, screen = manifest.screen_index[screen_id]
    initial = ScreenState(screen_id, screen.canonical_id, session.session_id).__dict__
    chosen = initial
    for state in result.states.get(screen_id, []):
        clock = state.get("clock_ms")
        if isinstance(clock, int) and clock <= logic_ms:
            chosen = state
        elif isinstance(clock, int) and clock > logic_ms:
            break
    return chosen


def canonical_replay_digest(result: ReplayResult) -> str:
    from .hashing import sha256_hex

    return sha256_hex(
        {
            "states": result.states,
            "counts": result.counts_by_screen,
            "decisions": [decision.to_dict() for decision in result.decisions],
        }
    )
