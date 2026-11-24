from __future__ import annotations

from typing import Any

import numpy as np

from .chunks import ChunkLayout
from .math3d import quat_slerp, trs_matrix


def compare_values(actual: Any, op: str, expected: Any) -> bool:
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    raise ValueError(f"unsupported op {op}")


def sample_parameter_values(param: dict[str, Any], times_ms: list[int]) -> list[Any]:
    keys = param["keys"]
    if not keys:
        return [param["default"] for _ in times_ms]
    values: list[Any] = []
    for time_ms in times_ms:
        if time_ms <= keys[0]["time"]:
            values.append(keys[0]["value"])
            continue
        if time_ms >= keys[-1]["time"]:
            values.append(keys[-1]["value"])
            continue
        for index in range(len(keys) - 1):
            left = keys[index]
            right = keys[index + 1]
            if left["time"] <= time_ms <= right["time"]:
                if param["type"] in ("bool", "trigger"):
                    values.append(left["value"] if time_ms < right["time"] else right["value"])
                else:
                    span = right["time"] - left["time"]
                    alpha = 0.0 if span == 0 else (time_ms - left["time"]) / span
                    values.append(float(left["value"] + (right["value"] - left["value"]) * alpha))
                break
    return values


def condition_text(condition: dict[str, Any]) -> str:
    return f"{condition['param']} {condition['op']} {condition['value']!r}"


def build_state_trace(project: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    spec = project["spec"]
    layout = ChunkLayout(spec)
    times = [frame * layout.step_ms for frame in range(layout.total_frames)]
    parameters = {
        param["name"]: sample_parameter_values(param, times)
        for param in project["state_machine"]["parameters"]
    }
    clips = {clip["id"]: clip for clip in project["clips"]}
    states = {state["name"]: state for state in project["state_machine"]["states"]}
    transitions_by_source: dict[str, list[dict[str, Any]]] = {}
    for transition in project["state_machine"]["transitions"]:
        transitions_by_source.setdefault(transition["from"], []).append(transition)
    for items in transitions_by_source.values():
        items.sort(key=lambda tr: (-int(tr["priority"]), tr["id"]))

    trace: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    current_state = project["state_machine"]["initial_state"]
    clip_start_ms = 0
    active_blend: dict[str, Any] | None = None

    for frame_index, time_ms in enumerate(times):
        state = states[current_state]
        clip = clips[state["clip"]]
        elapsed = time_ms - clip_start_ms
        total_play_ms = clip["duration_ms"] * clip["loop_count"]
        finished = elapsed >= total_play_ms
        candidate_transitions = transitions_by_source.get(current_state, [])
        candidate_ids = [tr["id"] for tr in candidate_transitions]
        event: dict[str, Any] | None = None

        selected: dict[str, Any] | None = None
        for transition in candidate_transitions:
            if not transition["interruptible"] and not finished:
                continue
            trigger_ok = True
            if transition["trigger"] == "clip_finished":
                trigger_ok = finished
            elif transition["trigger"] == "always":
                trigger_ok = True
            conditions_ok = all(
                compare_values(
                    parameters[cond["param"]][frame_index],
                    cond["op"],
                    cond["value"],
                )
                for cond in transition["conditions"]
            )
            if trigger_ok and conditions_ok:
                selected = transition
                break

        if selected is not None:
            source_state = current_state
            source_clip_id = state["clip"]
            source_sample_ms = clip["duration_ms"] if finished else elapsed % clip["duration_ms"]
            condition_summaries = [condition_text(cond) for cond in selected["conditions"]]
            decisive = (
                " AND ".join(condition_summaries)
                if condition_summaries
                else selected["trigger"]
            )
            event = {
                "frame": frame_index,
                "time_ms": time_ms,
                "source_state": source_state,
                "target_state": selected["to"],
                "source_clip": source_clip_id,
                "target_clip": states[selected["to"]]["clip"],
                "transition_id": selected["id"],
                "decisive_condition": decisive,
                "priority": selected["priority"],
            }
            events.append(event)
            current_state = selected["to"]
            clip_start_ms = time_ms
            elapsed = 0
            state = states[current_state]
            clip = clips[state["clip"]]
            finished = False
            if spec["entry_blend_ms"] > 0:
                active_blend = {
                    "from_clip": source_clip_id,
                    "from_state": source_state,
                    "from_sample_ms": source_sample_ms,
                    "start_ms": time_ms,
                    "blend_ms": int(spec["entry_blend_ms"]),
                    "transition_id": selected["id"],
                }
            else:
                active_blend = None

        if finished:
            loop_index = clip["loop_count"] - 1
            local_ms = clip["duration_ms"]
            sample_ms = clip["duration_ms"]
        else:
            loop_index = elapsed // clip["duration_ms"]
            local_ms = elapsed % clip["duration_ms"]
            sample_ms = local_ms

        blend_context = None
        if active_blend is not None:
            blend_elapsed = time_ms - active_blend["start_ms"]
            alpha = min(1.0, max(0.0, blend_elapsed / active_blend["blend_ms"]))
            if alpha < 1.0:
                blend_context = {**active_blend, "alpha": alpha}
            else:
                active_blend = None

        trace.append(
            {
                "frame": frame_index,
                "time_ms": time_ms,
                "state": current_state,
                "clip_id": clip["id"],
                "clip_start_ms": clip_start_ms,
                "elapsed_ms": elapsed,
                "loop_index": int(loop_index),
                "local_ms": int(local_ms),
                "sample_ms": int(sample_ms),
                "finished": bool(finished),
                "candidate_transition_ids": candidate_ids,
                "event": event,
                "blend": blend_context,
            }
        )

    return trace, events


def find_key_bounds(track: dict[str, Any], time_ms: int) -> tuple[int, int]:
    keys = track["keys"]
    if not keys:
        return time_ms, time_ms
    if time_ms <= keys[0]["time"]:
        return keys[0]["time"], keys[0]["time"]
    if time_ms >= keys[-1]["time"]:
        return keys[-1]["time"], keys[-1]["time"]
    for index in range(len(keys) - 1):
        left_time = keys[index]["time"]
        right_time = keys[index + 1]["time"]
        if left_time <= time_ms <= right_time:
            return left_time, right_time
    return keys[-1]["time"], keys[-1]["time"]


def sample_track_value(track: dict[str, Any], time_ms: int) -> Any:
    keys = track["keys"]
    if not keys:
        return None
    if time_ms <= keys[0]["time"]:
        return keys[0]["value"]
    if time_ms >= keys[-1]["time"]:
        return keys[-1]["value"]
    for index in range(len(keys) - 1):
        left = keys[index]
        right = keys[index + 1]
        if left["time"] <= time_ms <= right["time"]:
            span = right["time"] - left["time"]
            alpha = 0.0 if span == 0 else (time_ms - left["time"]) / span
            if track["channel"] == "rotation":
                from .math3d import quat_slerp

                return quat_slerp(np.asarray(left["value"], dtype=float), np.asarray(right["value"], dtype=float), alpha)
            if isinstance(left["value"], list):
                a = np.asarray(left["value"], dtype=float)
                b = np.asarray(right["value"], dtype=float)
                return a + (b - a) * alpha
            return float(left["value"] + (right["value"] - left["value"]) * alpha)
    return keys[-1]["value"]


def _apply_track_to_trs(trs: dict[str, np.ndarray], track: dict[str, Any], time_ms: int) -> None:
    value = sample_track_value(track, time_ms)
    if value is None:
        return
    channel = track["channel"]
    if channel == "tx":
        trs["translation"][0] = float(value)
    elif channel == "ty":
        trs["translation"][1] = float(value)
    elif channel == "tz":
        trs["translation"][2] = float(value)
    elif channel == "translation":
        trs["translation"] = np.asarray(value, dtype=float).copy()
    elif channel == "rotation":
        trs["rotation"] = np.asarray(value, dtype=float).copy()
    elif channel == "scale":
        trs["scale"] = np.asarray(value, dtype=float).copy()


def sample_local_trs(project: dict[str, Any], clip_id: str, bone_name: str, time_ms: int) -> dict[str, np.ndarray]:
    skeleton = project["skeleton"]
    bone = skeleton["bones"][skeleton["name_to_index"][bone_name]]
    trs = {
        "translation": np.asarray(bone["rest_translation"], dtype=float).copy(),
        "rotation": np.asarray(bone["rest_rotation"], dtype=float).copy(),
        "scale": np.asarray(bone["rest_scale"], dtype=float).copy(),
    }
    clip = next(clip for clip in project["clips"] if clip["id"] == clip_id)
    for track in clip["tracks"]:
        if track["bone"] == bone_name:
            _apply_track_to_trs(trs, track, time_ms)
    return trs


def sample_local_matrix(project: dict[str, Any], context: dict[str, Any], bone_name: str) -> np.ndarray:
    trs = sample_local_trs(project, context["clip_id"], bone_name, context["sample_ms"])
    blend = context.get("blend")
    if blend is not None:
        target = trs
        source = sample_local_trs(project, blend["from_clip"], bone_name, blend["from_sample_ms"])
        alpha = float(blend["alpha"])
        trs = {
            "translation": source["translation"] + (target["translation"] - source["translation"]) * alpha,
            "scale": source["scale"] + (target["scale"] - source["scale"]) * alpha,
            "rotation": quat_slerp(
                source["rotation"], target["rotation"], alpha
            ),
        }
    return trs_matrix(trs["translation"], trs["rotation"], trs["scale"])


def evaluate_range(
    project: dict[str, Any],
    trace: list[dict[str, Any]],
    start_frame: int,
    end_frame: int,
    reuse_pose: np.ndarray | None = None,
    dirty_bone_indices: set[int] | None = None,
) -> np.ndarray:
    bone_count = len(project["skeleton"]["bones"])
    frame_count = end_frame - start_frame + 1
    if reuse_pose is not None:
        expected_shape = (frame_count, bone_count, 16)
        if reuse_pose.shape != expected_shape or reuse_pose.dtype != np.float64:
            reuse_pose = np.asarray(reuse_pose, dtype=np.float64).reshape(expected_shape).copy()
        pose = reuse_pose.copy()
    else:
        pose = np.zeros((frame_count, bone_count, 16), dtype=np.float64)

    order = project["skeleton"]["topological_order"]
    name_to_index = project["skeleton"]["name_to_index"]

    for local_frame, global_frame in enumerate(range(start_frame, end_frame + 1)):
        context = trace[global_frame]
        for bone_index in order:
            if dirty_bone_indices is not None and bone_index not in dirty_bone_indices:
                continue
            bone = project["skeleton"]["bones"][bone_index]
            local_matrix = sample_local_matrix(project, context, bone["name"])
            parent_index = bone["parent_index"]
            if parent_index is None:
                parent_matrix = np.eye(4, dtype=np.float64)
            else:
                parent_matrix = pose[local_frame, parent_index].reshape(4, 4)
            global_matrix = parent_matrix @ local_matrix
            pose[local_frame, bone_index, :] = global_matrix.flatten()
    return pose


def evaluate_full_pose(project: dict[str, Any], trace: list[dict[str, Any]] | None = None) -> tuple[np.ndarray, list[dict[str, Any]]]:
    if trace is None:
        trace, events = build_state_trace(project)
    else:
        events = [item["event"] for item in trace if item.get("event")]
    layout = ChunkLayout(project["spec"])
    poses = []
    for bounds in layout.all_bounds():
        poses.append(evaluate_range(project, trace, bounds.start_frame, bounds.end_frame))
    return np.concatenate(poses, axis=0), events
