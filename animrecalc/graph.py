from __future__ import annotations

from typing import Any

from .evaluator import find_key_bounds


def descendant_bone_indices(project: dict[str, Any], root_names: set[str]) -> set[int]:
    skeleton = project["skeleton"]
    selected = {skeleton["name_to_index"][name] for name in root_names if name in skeleton["name_to_index"]}
    result = set(selected)
    frontier = list(selected)
    while frontier:
        parent = frontier.pop()
        for child in skeleton["children"][parent]:
            if child not in result:
                result.add(child)
                frontier.append(child)
    return result


def build_chunk_dependency(
    project: dict[str, Any],
    trace: list[dict[str, Any]],
    start_frame: int,
    end_frame: int,
) -> dict[str, Any]:
    span_map: dict[str, dict[str, Any]] = {}
    occurrences: dict[tuple[str, int], dict[str, Any]] = {}
    transition_ids: set[str] = set()
    state_names: set[str] = set()
    clip_ids: set[str] = set()
    events: list[dict[str, Any]] = []

    clips = {clip["id"]: clip for clip in project["clips"]}

    def add_track_spans(clip_id: str, time_ms: int, role: str) -> None:
        clip = clips[clip_id]
        for track in clip["tracks"]:
            if not track["keys"]:
                continue
            low, high = find_key_bounds(track, time_ms)
            key = f"{clip_id}\u001f{track['bone']}\u001f{track['channel']}"
            if key not in span_map:
                span_map[key] = {
                    "clip_id": clip_id,
                    "bone": track["bone"],
                    "channel": track["channel"],
                    "interpolation": track["interpolation"],
                    "span_start": low,
                    "span_end": high,
                    "roles": {role},
                }
            else:
                item = span_map[key]
                item["span_start"] = min(item["span_start"], low)
                item["span_end"] = max(item["span_end"], high)
                item["roles"].add(role)

    for frame in range(start_frame, end_frame + 1):
        context = trace[frame]
        clip_id = context["clip_id"]
        clip_ids.add(clip_id)
        state_names.add(context["state"])
        transition_ids.update(context.get("candidate_transition_ids", []))
        if context.get("event"):
            events.append(context["event"])
            transition_ids.add(context["event"]["transition_id"])

        occurrence_key = (clip_id, int(context["clip_start_ms"]))
        occurrence = occurrences.get(occurrence_key)
        if occurrence is None:
            occurrences[occurrence_key] = {
                "clip_id": clip_id,
                "state": context["state"],
                "clip_start_ms": int(context["clip_start_ms"]),
                "first_frame": frame,
                "last_frame": frame,
                "first_ms": context["time_ms"],
                "last_ms": context["time_ms"],
                "sample_min_ms": int(context["sample_ms"]),
                "sample_max_ms": int(context["sample_ms"]),
                "loop_indices": [int(context["loop_index"])],
            }
        else:
            occurrence["last_frame"] = frame
            occurrence["last_ms"] = context["time_ms"]
            occurrence["sample_min_ms"] = min(occurrence["sample_min_ms"], context["sample_ms"])
            occurrence["sample_max_ms"] = max(occurrence["sample_max_ms"], context["sample_ms"])
            occurrence["loop_indices"].append(int(context["loop_index"]))

        add_track_spans(clip_id, int(context["sample_ms"]), "primary")
        blend = context.get("blend")
        if blend:
            clip_ids.add(blend["from_clip"])
            add_track_spans(blend["from_clip"], int(blend["from_sample_ms"]), "blend_source")

    bone_channels = []
    for item in span_map.values():
        item = dict(item)
        item["roles"] = sorted(item["roles"])
        bone_channels.append(item)
    bone_channels.sort(key=lambda x: (x["clip_id"], x["bone"], x["channel"]))

    clip_windows = list(occurrences.values())
    for window in clip_windows:
        window["loop_indices"] = sorted(set(window["loop_indices"]))
    clip_windows.sort(key=lambda x: (x["clip_start_ms"], x["clip_id"]))

    return {
        "frame_start": start_frame,
        "frame_end": end_frame,
        "bone_channels": bone_channels,
        "clip_ids": sorted(clip_ids),
        "state_names": sorted(state_names),
        "transition_ids": sorted(transition_ids),
        "clip_windows": clip_windows,
        "events": events,
    }


def trace_signature(context: dict[str, Any]) -> dict[str, Any]:
    """Schedule fields that can affect events or sampled poses."""
    blend = context.get("blend")
    blend_sig = None
    if blend:
        blend_sig = {
            "from_clip": blend["from_clip"],
            "from_sample_ms": blend["from_sample_ms"],
            "alpha": round(float(blend["alpha"]), 12),
            "transition_id": blend["transition_id"],
        }
    return {
        "state": context["state"],
        "clip_id": context["clip_id"],
        "clip_start_ms": context["clip_start_ms"],
        "loop_index": context["loop_index"],
        "sample_ms": context["sample_ms"],
        "finished": context["finished"],
        "event": context["event"],
        "blend": blend_sig,
    }


def diff_trace_chunks(
    old_trace: list[dict[str, Any]],
    new_trace: list[dict[str, Any]],
    layout: Any,
) -> tuple[set[int], int | None]:
    dirty: set[int] = set()
    first_different_frame: int | None = None
    limit = min(len(old_trace), len(new_trace))
    for frame in range(limit):
        if trace_signature(old_trace[frame]) != trace_signature(new_trace[frame]):
            if first_different_frame is None:
                first_different_frame = frame
            dirty.add(layout.chunk_index_for_frame(frame))
    if len(old_trace) != len(new_trace):
        start = min(len(old_trace), len(new_trace))
        if first_different_frame is None:
            first_different_frame = start
        for frame in range(start, max(len(old_trace), len(new_trace))):
            dirty.add(layout.chunk_index_for_frame(min(frame, layout.total_frames - 1)))
    return dirty, first_different_frame


def affected_frames_for_local_interval(
    trace: list[dict[str, Any]],
    clip_id: str,
    interval: tuple[int, int],
) -> set[int]:
    low, high = interval
    frames: set[int] = set()
    for context in trace:
        frame = int(context["frame"])
        if context["clip_id"] == clip_id and low <= int(context["sample_ms"]) <= high:
            frames.add(frame)
        blend = context.get("blend")
        if blend and blend["from_clip"] == clip_id and low <= int(blend["from_sample_ms"]) <= high:
            frames.add(frame)
    return frames
