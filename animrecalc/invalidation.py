from __future__ import annotations

from typing import Any

from .graph import affected_frames_for_local_interval, descendant_bone_indices, diff_trace_chunks
from .pipeline import canonical_channel


def find_track(project: dict[str, Any], clip_id: str, bone: str, channel: str) -> dict[str, Any] | None:
    canonical = canonical_channel(channel)
    clip = next((clip for clip in project["clips"] if clip["id"] == clip_id), None)
    if clip is None:
        return None
    return next(
        (track for track in clip["tracks"] if track["bone"] == bone and track["channel"] == canonical),
        None,
    )


def interval_for_present_key(track: dict[str, Any], time_ms: int, duration_ms: int) -> tuple[int, int]:
    keys = track["keys"]
    index = next((i for i, key in enumerate(keys) if key["time"] == time_ms), None)
    if index is None:
        index = min(range(len(keys)), key=lambda i: abs(keys[i]["time"] - time_ms)) if keys else None
    if index is None:
        return 0, duration_ms
    low = keys[index - 1]["time"] if index > 0 else 0
    high = keys[index + 1]["time"] if index + 1 < len(keys) else duration_ms
    return int(low), int(high)


def interval_for_deleted_key(track: dict[str, Any], time_ms: int, duration_ms: int) -> tuple[int, int]:
    keys = track["keys"]
    index = next((i for i, key in enumerate(keys) if key["time"] == time_ms), None)
    if index is None:
        return 0, duration_ms
    low = keys[index - 1]["time"] if index > 0 else 0
    high = keys[index + 1]["time"] if index + 1 < len(keys) else duration_ms
    return int(low), int(high)


def merge_intervals(intervals: list[tuple[int, int]]) -> tuple[int, int] | None:
    intervals = [item for item in intervals if item is not None]
    if not intervals:
        return None
    return min(low for low, _ in intervals), max(high for _, high in intervals)


def _chunk_set_from_frames(frames: set[int], layout: Any) -> set[int]:
    return {layout.chunk_index_for_frame(frame) for frame in frames}


def plan_key_edit(
    op: dict[str, Any],
    old_project: dict[str, Any],
    new_project: dict[str, Any],
    old_trace: list[dict[str, Any]],
    layout: Any,
) -> dict[str, Any]:
    clip_id = op.get("clip", op.get("clip_id"))
    bone = op.get("bone")
    channel = canonical_channel(op.get("channel"))
    time_ms = int(op.get("time", op.get("time_ms", op.get("t"))))
    old_clip = next(clip for clip in old_project["clips"] if clip["id"] == clip_id)
    new_clip = next(clip for clip in new_project["clips"] if clip["id"] == clip_id)
    old_track = find_track(old_project, clip_id, bone, channel)
    new_track = find_track(new_project, clip_id, bone, channel)
    duration = int(new_clip["duration_ms"])
    intervals: list[tuple[int, int]] = []

    if op["op"] == "delete_key":
        if old_track is not None:
            intervals.append(interval_for_deleted_key(old_track, time_ms, duration))
    elif op["op"] == "update_key":
        new_time = int(op.get("new_time", op.get("new_time_ms", op.get("new_t", time_ms))))
        if old_track is not None:
            intervals.append(interval_for_present_key(old_track, time_ms, old_clip["duration_ms"]))
        if new_track is not None:
            intervals.append(interval_for_present_key(new_track, new_time, duration))
    else:
        if op["op"] == "upsert_key" and old_track is not None and any(k["time"] == time_ms for k in old_track["keys"]):
            intervals.append(interval_for_present_key(old_track, time_ms, old_clip["duration_ms"]))
        if new_track is not None:
            intervals.append(interval_for_present_key(new_track, time_ms, duration))

    interval = merge_intervals(intervals) or (0, duration)
    frames = affected_frames_for_local_interval(old_trace, clip_id, interval)
    chunks = _chunk_set_from_frames(frames, layout)
    affected_indices = descendant_bone_indices(old_project, {bone})
    affected_names = [old_project["skeleton"]["bones"][i]["name"] for i in sorted(affected_indices)]
    return {
        "kind": "keyframe_channel",
        "op": op["op"],
        "clip_id": clip_id,
        "root_bone": bone,
        "channel": channel,
        "affected_local_interval_ms": list(interval),
        "affected_frame_count": len(frames),
        "affected_bone_indices": affected_indices,
        "affected_bones": affected_names,
        "dirty_chunks": chunks,
        "ported": False,
        "summary": f"{op['op']} {clip_id}/{bone}/{channel}@{time_ms}; bones={len(affected_names)}; frames={len(frames)}",
    }


def _transition_identity(op: dict[str, Any], new_project: dict[str, Any]) -> tuple[set[str], set[str]]:
    transition_ids: set[str] = set()
    source_states: set[str] = set()
    tr_id = op.get("transition_id", op.get("id"))
    if tr_id is not None:
        transition_ids.add(tr_id)
    if op.get("from", op.get("source")):
        source_states.add(op.get("from", op.get("source")))
    raw_transition = op.get("transition") if isinstance(op.get("transition"), dict) else None
    if raw_transition:
        if raw_transition.get("id"):
            transition_ids.add(raw_transition["id"])
        if raw_transition.get("from", raw_transition.get("source")):
            source_states.add(raw_transition.get("from", raw_transition.get("source")))
    for tr in new_project["state_machine"]["transitions"]:
        if tr["id"] in transition_ids:
            source_states.add(tr["from"])
    return transition_ids, source_states


def plan_state_edit(
    op: dict[str, Any],
    old_project: dict[str, Any],
    new_project: dict[str, Any],
    old_trace: list[dict[str, Any]],
    new_trace: list[dict[str, Any]],
    old_dependencies: list[dict[str, Any]],
    layout: Any,
) -> dict[str, Any]:
    dirty, first_frame = diff_trace_chunks(old_trace, new_trace, layout)
    transition_ids, source_states = _transition_identity(op, new_project)
    candidate_chunks: set[int] = set()
    for dep in old_dependencies:
        if transition_ids.intersection(dep.get("transition_ids", [])) or source_states.intersection(dep.get("state_names", [])):
            candidate_chunks.add(dep["chunk_index"])
    dirty.update(candidate_chunks)
    first_chunk = layout.chunk_index_for_frame(first_frame) if first_frame is not None else (min(candidate_chunks) if candidate_chunks else None)
    if first_chunk is not None:
        # A changed decision can redirect all later clip ownership. The trace diff
        # already identifies exact differing chunks; keep the ripple marker explicit.
        pass
    return {
        "kind": "state_machine",
        "op": op["op"],
        "transition_ids": sorted(transition_ids),
        "source_states": sorted(source_states),
        "dirty_chunks": dirty,
        "trace_first_different_frame": first_frame,
        "ripple_start_chunk": first_chunk,
        "all_bones": True,
        "ported": False,
        "summary": f"{op['op']} transitions={sorted(transition_ids)} candidate_chunks={len(candidate_chunks)} trace_chunks={len(dirty - candidate_chunks)}",
    }


def plan_duration_edit(
    op: dict[str, Any],
    old_project: dict[str, Any],
    new_project: dict[str, Any],
    old_trace: list[dict[str, Any]],
    new_trace: list[dict[str, Any]],
    layout: Any,
) -> dict[str, Any]:
    clip_id = op.get("clip", op.get("clip_id"))
    old_duration = next(c["duration_ms"] for c in old_project["clips"] if c["id"] == clip_id)
    new_duration = next(c["duration_ms"] for c in new_project["clips"] if c["id"] == clip_id)
    dirty, first_diff = diff_trace_chunks(old_trace, new_trace, layout)

    first_occurrence_ms: int | None = None
    for context in old_trace:
        if context["clip_id"] == clip_id:
            first_occurrence_ms = int(context["clip_start_ms"])
            break
    theoretical_impact_frame: int | None = None
    if first_occurrence_ms is not None and old_duration != new_duration:
        impact_ms = first_occurrence_ms + min(old_duration, new_duration)
        impact_frame = impact_ms // layout.step_ms
        if impact_frame < layout.total_frames:
            theoretical_impact_frame = impact_frame

    ripple_start_frame = first_diff
    if first_diff is not None:
        ripple_chunk = layout.chunk_index_for_frame(first_diff)
        for index in range(ripple_chunk, layout.chunk_count):
            dirty.add(index)
    ripple_chunk = layout.chunk_index_for_frame(ripple_start_frame) if ripple_start_frame is not None else None
    ported = set(range(ripple_chunk, layout.chunk_count)) if ripple_chunk is not None else set()
    return {
        "kind": "clip_duration",
        "op": op["op"],
        "clip_id": clip_id,
        "old_duration_ms": old_duration,
        "new_duration_ms": new_duration,
        "dirty_chunks": dirty,
        "ripple_start_frame": ripple_start_frame,
        "ripple_start_chunk": ripple_chunk,
        "theoretical_first_mapping_frame": theoretical_impact_frame,
        "ported_chunks": ported,
        "all_bones": True,
        "ported": True,
        "summary": f"duration {clip_id}: {old_duration}->{new_duration}; dirty={len(dirty)}; ported={len(ported)}",
    }


def plan_edit(
    op: dict[str, Any],
    old_project: dict[str, Any],
    new_project: dict[str, Any],
    old_trace: list[dict[str, Any]],
    new_trace: list[dict[str, Any]],
    old_dependencies: list[dict[str, Any]],
    layout: Any,
) -> dict[str, Any]:
    if op["op"] in {
        "add_key",
        "upsert_key",
        "update_key",
        "update_key_value",
        "delete_key",
    }:
        return plan_key_edit(op, old_project, new_project, old_trace, layout)
    if op["op"] == "set_clip_duration":
        return plan_duration_edit(op, old_project, new_project, old_trace, new_trace, layout)
    return plan_state_edit(op, old_project, new_project, old_trace, new_trace, old_dependencies, layout)
