from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical import sha256_value
from .concurrency import precompute_clip_cache
from .models import (
    ChunkResult,
    MachineState,
    ReplayFrame,
    ReplayResult,
    Spec,
    Transition,
    TransitionEvent,
)
from .sampler import (
    ClipPose,
    ClipPoseSet,
    conditions_match,
    frame_from_pose,
    pose_for_state,
    sample_parameter,
)
from .schema import version_id


@dataclass(frozen=True)
class SimulationSlice:
    frames: list[ReplayFrame]
    events: list[TransitionEvent]
    state_at_end: MachineState


def _clip_finished(
    spec: Spec,
    state: MachineState,
    t: int,
) -> bool:
    clip = spec.clips[state.active_clip]
    if state.target_loop_count <= 0:
        return False
    return t - state.enter_ms >= state.target_loop_count * clip.duration_ms


def candidate_transitions(spec: Spec, active_clip: str) -> tuple[Transition, ...]:
    return tuple(
        transition
        for transition in spec.transitions
        if "*" in transition.sources or active_clip in transition.sources
    )


def _choose_transition(
    spec: Spec,
    state: MachineState,
    t: int,
    parameter_values: Mapping[str, Any],
    finished: bool,
) -> Transition | None:
    if t == 0:
        return None
    elapsed = t - state.enter_ms
    choices: list[Transition] = []
    for transition in candidate_transitions(spec, state.active_clip):
        if transition.on_finish and not finished:
            continue
        if not transition.interrupt and not finished:
            continue
        if elapsed < transition.min_hold_ms:
            continue
        if not conditions_match(transition, parameter_values):
            continue
        choices.append(transition)
    if not choices:
        return None
    return max(choices, key=lambda item: (item.priority, item.id))


def _parameter_values(spec: Spec, t: int) -> dict[str, Any]:
    return {
        name: sample_parameter(parameter, t)
        for name, parameter in spec.parameters.items()
    }


def simulate_frames(
    spec: Spec,
    cache: Mapping[str, ClipPoseSet],
    times: Sequence[int],
    start_state: MachineState,
) -> SimulationSlice:
    state = start_state
    frames: list[ReplayFrame] = []
    events: list[TransitionEvent] = []
    for t in times:
        values = _parameter_values(spec, t)
        finished = _clip_finished(spec, state, t)
        transition = _choose_transition(spec, state, t, values, finished)
        if transition is not None:
            source_clip = state.active_clip
            target_clip = transition.target
            target_loop_count = (
                transition.loop_count
                if transition.loop_count is not None
                else spec.clips[target_clip].loop_count
            )
            state = MachineState(
                active_clip=target_clip,
                enter_ms=t,
                target_loop_count=target_loop_count,
                generation=state.generation + 1,
            )
            events.append(
                TransitionEvent(
                    t=t,
                    rule_id=transition.id,
                    source_clip=source_clip,
                    target_clip=target_clip,
                    priority=transition.priority,
                    state_generation=state.generation,
                )
            )
        pose, local_ms, _finished_after_switch = pose_for_state(spec, cache, state, t)
        frames.append(frame_from_pose(t, state, local_ms, pose))
    return SimulationSlice(frames=frames, events=events, state_at_end=state)


def _merge_interval(
    intervals: list[list[int]],
    lo: int,
    hi: int,
) -> None:
    if lo > hi:
        lo, hi = hi, lo
    for interval in intervals:
        if interval[0] == lo and interval[1] == hi:
            return
    intervals.append([lo, hi])
    intervals.sort()


def build_chunk_dependencies(
    spec: Spec,
    frames: Sequence[ReplayFrame],
    events: Sequence[TransitionEvent],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    clips: dict[str, dict[str, Any]] = {}
    tracks: dict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
    bones: set[str] = set()
    rules: dict[str, dict[str, Any]] = {}
    selected_rules = sorted({event.rule_id for event in events})
    selected_at: set[tuple[int, str]] = {(event.t, event.rule_id) for event in events}

    for frame in frames:
        clip_entry = clips.setdefault(
            frame.clip,
            {"frames": [], "local_min_ms": frame.local_ms, "local_max_ms": frame.local_ms},
        )
        clip_entry["frames"].append(frame.t)
        clip_entry["local_min_ms"] = min(clip_entry["local_min_ms"], frame.local_ms)
        clip_entry["local_max_ms"] = max(clip_entry["local_max_ms"], frame.local_ms)
        for sample in frame.track_samples:
            key = (frame.clip, sample.bone, sample.channel)
            track_entry = tracks.setdefault(
                key,
                {
                    "clip": frame.clip,
                    "bone": sample.bone,
                    "channel": sample.channel,
                    "intervals": [],
                    "frames": [],
                    "local_min_ms": frame.local_ms,
                    "local_max_ms": frame.local_ms,
                },
            )
            _merge_interval(track_entry["intervals"], sample.lo_ms, sample.hi_ms)
            track_entry["frames"].append(frame.t)
            track_entry["local_min_ms"] = min(track_entry["local_min_ms"], frame.local_ms)
            track_entry["local_max_ms"] = max(track_entry["local_max_ms"], frame.local_ms)
            bones.add(sample.bone)
        for transition in candidate_transitions(spec, frame.clip):
            entry = rules.setdefault(
                transition.id,
                {
                    "id": transition.id,
                    "source": list(transition.sources),
                    "target": transition.target,
                    "priority": transition.priority,
                    "frames": [],
                    "selected": False,
                },
            )
            entry["frames"].append(frame.t)
            if (frame.t, transition.id) in selected_at:
                entry["selected"] = True

    for event in events:
        transition = next((item for item in spec.transitions if item.id == event.rule_id), None)
        if transition is None:
            continue
        entry = rules.setdefault(
            transition.id,
            {
                "id": transition.id,
                "source": list(transition.sources),
                "target": transition.target,
                "priority": transition.priority,
                "frames": [],
                "selected": True,
            },
        )
        entry["selected"] = True
        if event.t not in entry["frames"]:
            entry["frames"].append(event.t)
            entry["frames"].sort()

    return {
        "logical_start_ms": start_ms,
        "logical_end_ms": end_ms,
        "frame_count": len(frames),
        "clips": {clip_id: clips[clip_id] for clip_id in sorted(clips)},
        "tracks": sorted(
            tracks.values(),
            key=lambda item: (item["clip"], item["bone"], item["channel"]),
        ),
        "bones": sorted(bones),
        "transition_rules": sorted(rules.values(), key=lambda item: item["id"]),
        "selected_transition_rules": selected_rules,
    }


def chunk_times(spec: Spec, chunk_index: int) -> tuple[int, ...]:
    start = chunk_index * spec.timeline.chunk_ms
    end = start + spec.timeline.chunk_ms
    return tuple(t for t in spec.timeline.times if start <= t < end)


def chunk_bounds(spec: Spec, chunk_index: int) -> tuple[int, int]:
    start = chunk_index * spec.timeline.chunk_ms
    return start, start + spec.timeline.chunk_ms


def chunk_count(spec: Spec) -> int:
    return spec.timeline.duration_ms // spec.timeline.chunk_ms + 1


def build_replay(
    spec: Spec,
    *,
    max_workers: int = 1,
    memory_budget_bytes: int = 64 * 1024 * 1024,
) -> ReplayResult:
    cache, concurrency_report = precompute_clip_cache(
        spec,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )
    initial = spec.clips[spec.initial_clip]
    state = MachineState(
        active_clip=spec.initial_clip,
        enter_ms=0,
        target_loop_count=initial.loop_count,
        generation=0,
    )
    chunks: list[ChunkResult] = []
    for index in range(chunk_count(spec)):
        times = chunk_times(spec, index)
        start_ms, end_ms = chunk_bounds(spec, index)
        state_at_start = state
        slice_result = simulate_frames(spec, cache, times, state)
        state = slice_result.state_at_end
        chunks.append(
            ChunkResult(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                frames=slice_result.frames,
                events=slice_result.events,
                state_at_start=state_at_start,
                state_at_end=state,
                dependencies=build_chunk_dependencies(
                    spec,
                    slice_result.frames,
                    slice_result.events,
                    start_ms,
                    end_ms,
                ),
            )
        )
    return ReplayResult(
        spec=spec,
        version_id=version_id(spec),
        chunks=chunks,
        concurrency=concurrency_report,
    )


def _flat_pose(frame: ReplayFrame) -> dict[str, list[float]]:
    return frame.bones_flat()


def compare_replays(
    expected: ReplayResult,
    actual: ReplayResult,
    *,
    tolerance: float = 1e-6,
    max_report: int = 20,
) -> dict[str, Any]:
    diffs: list[dict[str, Any]] = []
    expected_frames = {frame.t: frame for frame in expected.frames}
    actual_frames = {frame.t: frame for frame in actual.frames}
    all_times = sorted(set(expected_frames) | set(actual_frames))
    first_over: dict[str, Any] | None = None
    max_abs = 0.0
    for t in all_times:
        left = expected_frames.get(t)
        right = actual_frames.get(t)
        if left is None or right is None:
            item = {"t": t, "reason": "missing_frame"}
            diffs.append(item)
            first_over = first_over or item
            continue
        if left.clip != right.clip or left.generation != right.generation:
            item = {
                "t": t,
                "reason": "state_mismatch",
                "expected": {"clip": left.clip, "generation": left.generation},
                "actual": {"clip": right.clip, "generation": right.generation},
            }
            diffs.append(item)
            first_over = first_over or item
        left_flat = _flat_pose(left)
        right_flat = _flat_pose(right)
        for key in sorted(set(right_flat) - set(left_flat)):
            item = {"t": t, "reason": "unexpected_channel", "bone_channel": key}
            diffs.append(item)
            first_over = first_over or item
        for key in sorted(left_flat):
            if key not in right_flat:
                item = {"t": t, "reason": "missing_channel", "bone_channel": key}
                diffs.append(item)
                first_over = first_over or item
                continue
            for component, (a, b) in enumerate(zip(left_flat[key], right_flat[key])):
                difference = abs(a - b)
                max_abs = max(max_abs, difference)
                if difference > tolerance:
                    item = {
                        "t": t,
                        "reason": "pose_tolerance",
                        "clip": left.clip,
                        "bone_channel": key,
                        "component": component,
                        "expected": a,
                        "actual": b,
                        "abs_diff": difference,
                    }
                    diffs.append(item)
                    if first_over is None:
                        first_over = item
                    if len(diffs) >= max_report:
                        break
            if len(diffs) >= max_report:
                break
        if len(diffs) >= max_report:
            break

    expected_events = [event.to_dict() for event in expected.events]
    actual_events = [event.to_dict() for event in actual.events]
    events_equal = expected_events == actual_events
    return {
        "ok": first_over is None and events_equal,
        "tolerance": tolerance,
        "max_abs_diff": max_abs,
        "first_over_tolerance": first_over,
        "differences": diffs,
        "events_equal": events_equal,
        "expected_event_count": len(expected_events),
        "actual_event_count": len(actual_events),
    }


def result_payload(result: ReplayResult, *, include_dependencies: bool = True) -> dict[str, Any]:
    return {
        "format": "anim-replay-result-v1",
        "version_id": result.version_id,
        "frame_step_ms": result.spec.timeline.frame_step_ms,
        "chunk_ms": result.spec.timeline.chunk_ms,
        "frame_count": len(result.frames),
        "frames": [
            frame.to_dict(include_dependencies=include_dependencies)
            for frame in result.frames
        ],
        "events": [event.to_dict() for event in result.events],
        "concurrency": result.concurrency,
    }


def replay_content_digest(result: ReplayResult) -> str:
    return sha256_value(result_payload(result))

