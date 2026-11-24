from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import AnimRecalcError
from .hashing import canonical_digest
from .validation import (
    fail,
    finite_number,
    finite_vector,
    is_int,
    require_dict,
    require_list,
    require_nonnegative_int,
    require_positive_int,
)


CHANNEL_COMPONENTS = {
    "tx": 1,
    "ty": 1,
    "tz": 1,
    "translation": 3,
    "rotation": 4,
    "scale": 3,
}

CHANNEL_ALIASES = {
    "location": "translation",
    "position": "translation",
    "pos": "translation",
    "location.x": "tx",
    "location.y": "ty",
    "location.z": "tz",
    "position.x": "tx",
    "position.y": "ty",
    "position.z": "tz",
    "quaternion": "rotation",
    "quat": "rotation",
    "scaling": "scale",
}


def canonical_channel(channel: Any) -> str:
    if not isinstance(channel, str) or not channel:
        raise fail("E_SCHEMA", "channel must be a nonempty string", channel=channel)
    normalized = CHANNEL_ALIASES.get(channel, channel)
    if normalized not in CHANNEL_COMPONENTS:
        raise fail(
            "E_SCHEMA",
            "unsupported channel; expected tx/ty/tz/translation/rotation/scale",
            channel=channel,
        )
    return normalized


def load_json_file(path: str | Path, label: str | None = None) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnimRecalcError("E_IO", f"cannot read {label or path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnimRecalcError(
            "E_PARSE",
            f"invalid JSON in {label or path}: {exc.msg}",
            {"line": exc.lineno, "column": exc.colno},
        ) from exc


def _time_of(key: dict[str, Any]) -> int:
    for field in ("time", "time_ms", "t", "frame"):
        if field in key:
            return require_nonnegative_int(key[field], f"key.{field}")
    raise fail("E_SCHEMA", "keyframe is missing time", key=key)


def _value_of(key: dict[str, Any]) -> Any:
    for field in ("value", "v", "values"):
        if field in key:
            return key[field]
    raise fail("E_SCHEMA", "keyframe is missing value", key=key)


def validate_skeleton(doc: Any) -> dict[str, Any]:
    root = require_dict(doc, "skeleton")
    raw_bones = require_list(root.get("bones"), "skeleton.bones")
    name_to_index: dict[str, int] = {}
    bones: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_bones):
        bone = require_dict(raw, f"skeleton.bones[{index}]")
        name = bone.get("name")
        if not isinstance(name, str) or not name:
            raise fail("E_SCHEMA", "bone name must be a nonempty string", index=index)
        if name in name_to_index:
            raise fail("E_DUPLICATE_BONE", f"duplicate bone name: {name}", bone=name)
        name_to_index[name] = index

        parent = bone.get("parent")
        if parent == "":
            parent = None
        if parent is not None and not isinstance(parent, str):
            raise fail("E_SCHEMA", f"bone {name} parent must be a string or null")

        rest = bone.get("rest_transform") if isinstance(bone.get("rest_transform"), dict) else bone
        translation = finite_vector(
            rest.get("rest_translation", rest.get("translation", [0.0, 0.0, 0.0])),
            3,
            f"bone {name} rest_translation",
        )
        rotation = finite_vector(
            rest.get("rest_rotation", rest.get("rotation", [1.0, 0.0, 0.0, 0.0])),
            4,
            f"bone {name} rest_rotation",
        )
        scale = finite_vector(
            rest.get("rest_scale", rest.get("scale", [1.0, 1.0, 1.0])),
            3,
            f"bone {name} rest_scale",
        )
        length_value = bone.get("length", 0.0)
        length = finite_number(length_value, f"bone {name} length")
        if length < 0.0:
            raise fail("E_SCHEMA", f"bone {name} length must be nonnegative", length=length)

        bones.append(
            {
                "name": name,
                "parent": parent,
                "rest_translation": translation,
                "rest_rotation": rotation,
                "rest_scale": scale,
                "length": length,
            }
        )

    for index, bone in enumerate(bones):
        parent = bone["parent"]
        if parent is not None and parent not in name_to_index:
            raise fail("E_PARENT_NOT_FOUND", f"bone {bone['name']} references missing parent {parent}")
        bone["parent_index"] = None if parent is None else name_to_index[parent]
        bone["index"] = index

    children: list[list[int]] = [[] for _ in bones]
    indegree = [0 for _ in bones]
    for index, bone in enumerate(bones):
        parent_index = bone["parent_index"]
        if parent_index is not None:
            children[parent_index].append(index)
            indegree[index] += 1

    queue = [index for index, degree in enumerate(indegree) if degree == 0]
    order: list[int] = []
    while queue:
        index = queue.pop(0)
        order.append(index)
        for child in children[index]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(order) != len(bones):
        cyclic = [bones[i]["name"] for i, degree in enumerate(indegree) if degree > 0]
        raise fail("E_CYCLE", "skeleton parent graph contains a cycle", bones=cyclic)

    return {
        "name": root.get("name", "skeleton"),
        "bones": bones,
        "topological_order": order,
        "children": children,
        "name_to_index": name_to_index,
    }


def _normalize_track(raw_track: Any, index: int, bone_names: set[str]) -> dict[str, Any]:
    track = require_dict(raw_track, f"tracks[{index}]")
    bone = track.get("bone")
    if bone not in bone_names:
        raise fail("E_SCHEMA", f"track references unknown bone {bone!r}", bone=bone)
    channel = canonical_channel(track.get("channel"))
    component_count = CHANNEL_COMPONENTS[channel]
    interpolation = track.get("interpolation")
    if interpolation is None:
        interpolation = "slerp" if channel == "rotation" else "linear"
    if interpolation not in ("linear", "slerp"):
        raise fail("E_SCHEMA", "interpolation must be linear or slerp", interpolation=interpolation)
    if interpolation == "slerp" and channel != "rotation":
        raise fail("E_SCHEMA", "slerp interpolation is only valid for rotation channels")

    raw_keys = require_list(track.get("keys", []), f"track {bone}/{channel}.keys")
    keys: list[dict[str, Any]] = []
    previous_time: int | None = None
    for key_index, raw_key in enumerate(raw_keys):
        key = require_dict(raw_key, f"track {bone}/{channel}.keys[{key_index}]")
        time = _time_of(key)
        if previous_time is not None and time <= previous_time:
            raise fail(
                "E_KEY_ORDER",
                f"keyframes for {bone}/{channel} must be strictly increasing",
                previous_time=previous_time,
                time=time,
            )
        previous_time = time
        value = _value_of(key)
        if component_count == 1:
            numeric_value = finite_number(value, f"{bone}/{channel} key {time}")
            normalized_value: Any = numeric_value
        else:
            normalized_value = finite_vector(
                value,
                component_count,
                f"{bone}/{channel} key {time} value",
            )
            if channel == "rotation":
                norm = sum(item * item for item in normalized_value) ** 0.5
                if norm < 1e-12:
                    raise fail("E_CHANNEL_VALUE", "rotation quaternion must not be zero", time=time)
        keys.append({"time": time, "value": normalized_value})

    return {
        "bone": bone,
        "channel": channel,
        "interpolation": interpolation,
        "keys": keys,
    }


def normalize_clips(doc: Any, skeleton: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        raw_clips = doc
    else:
        root = require_dict(doc, "clips")
        raw = root.get("clips", root)
        if isinstance(raw, dict):
            raw_clips = [dict(value, id=key) if isinstance(value, dict) else value for key, value in raw.items()]
        else:
            raw_clips = require_list(raw, "clips.clips")

    bone_names = set(skeleton["name_to_index"])
    clips: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_clip in enumerate(raw_clips):
        clip = require_dict(raw_clip, f"clips[{index}]")
        clip_id = clip.get("id", clip.get("name"))
        if not isinstance(clip_id, str) or not clip_id:
            raise fail("E_SCHEMA", "clip id/name must be a nonempty string", index=index)
        if clip_id in seen:
            raise fail("E_SCHEMA", f"duplicate clip id {clip_id}")
        seen.add(clip_id)
        duration = require_positive_int(clip.get("duration_ms", clip.get("duration")), f"clip {clip_id}.duration_ms")
        loop_count = clip.get("loop_count", clip.get("loops", 1))
        loop_count = require_positive_int(loop_count, f"clip {clip_id}.loop_count")
        raw_tracks = clip.get("tracks", clip.get("channels", []))
        tracks = [_normalize_track(item, track_index, bone_names) for track_index, item in enumerate(require_list(raw_tracks, f"clip {clip_id}.tracks"))]
        clips.append(
            {
                "id": clip_id,
                "duration_ms": duration,
                "loop_count": loop_count,
                "tracks": tracks,
            }
        )
    if not clips:
        raise fail("E_SCHEMA", "at least one animation clip is required")
    return clips


def _normalize_parameter(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise fail("E_SCHEMA", f"parameters[{index}] must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise fail("E_SCHEMA", "parameter name must be a nonempty string", index=index)
    param_type = raw.get("type", "float")
    if param_type not in ("float", "int", "bool", "trigger"):
        raise fail("E_SCHEMA", f"parameter {name} has unsupported type {param_type}")
    default = raw.get("value", raw.get("default", 0.0 if param_type != "bool" else False))
    if param_type == "bool" or param_type == "trigger":
        if not isinstance(default, bool):
            if param_type == "trigger":
                default = bool(default)
            else:
                raise fail("E_SCHEMA", f"parameter {name} default must be bool")
    else:
        default = finite_number(default, f"parameter {name} default")
        if param_type == "int" and not float(default).is_integer():
            raise fail("E_SCHEMA", f"int parameter {name} requires integer default")
        if param_type == "int":
            default = int(default)

    keys: list[dict[str, Any]] = []
    for key_index, raw_key in enumerate(raw.get("keys", raw.get("track", [])) or []):
        key = require_dict(raw_key, f"parameter {name}.keys[{key_index}]")
        time = _time_of(key)
        value = _value_of(key)
        if param_type == "bool" or param_type == "trigger":
            if not isinstance(value, bool):
                raise fail("E_SCHEMA", f"bool parameter {name} key value must be bool")
            normalized: Any = value
        else:
            normalized = finite_number(value, f"parameter {name} key {time}")
        if keys and time <= keys[-1]["time"]:
            raise fail("E_KEY_ORDER", f"parameter {name} key times must be strictly increasing")
        keys.append({"time": time, "value": normalized})

    return {"name": name, "type": param_type, "default": default, "keys": keys}


def normalize_state_machine(doc: Any, clips: list[dict[str, Any]]) -> dict[str, Any]:
    root = require_dict(doc, "state_machine")
    clip_ids = {clip["id"] for clip in clips}
    raw_states = require_list(root.get("states"), "state_machine.states")
    states: list[dict[str, Any]] = []
    state_names: set[str] = set()
    for index, raw_state in enumerate(raw_states):
        state = require_dict(raw_state, f"states[{index}]")
        name = state.get("name")
        if not isinstance(name, str) or not name:
            raise fail("E_SCHEMA", "state name must be a nonempty string", index=index)
        if name in state_names:
            raise fail("E_SCHEMA", f"duplicate state {name}")
        state_names.add(name)
        clip_id = state.get("clip", state.get("clip_id"))
        if clip_id not in clip_ids:
            raise fail("E_SCHEMA", f"state {name} references unknown clip {clip_id!r}")
        states.append({"name": name, "clip": clip_id})

    initial = root.get("initial_state", root.get("initial"))
    if initial not in state_names:
        raise fail("E_SCHEMA", "initial_state must reference a declared state", initial=initial)

    raw_parameters = root.get("parameters", [])
    if isinstance(raw_parameters, dict):
        parameters = [
            {"name": name, **(value if isinstance(value, dict) else {"value": value})}
            for name, value in raw_parameters.items()
        ]
    else:
        parameters = require_list(raw_parameters, "state_machine.parameters")
    normalized_parameters = [_normalize_parameter(item, i) for i, item in enumerate(parameters)]
    parameter_names = {item["name"] for item in normalized_parameters}

    raw_transitions = root.get("transitions", [])
    transitions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_transition in enumerate(require_list(raw_transitions, "state_machine.transitions")):
        tr = require_dict(raw_transition, f"transitions[{index}]")
        tr_id = tr.get("id", f"tr_{index}")
        if not isinstance(tr_id, str) or not tr_id:
            raise fail("E_SCHEMA", "transition id must be a nonempty string")
        if tr_id in seen_ids:
            raise fail("E_SCHEMA", f"duplicate transition id {tr_id}")
        seen_ids.add(tr_id)
        source = tr.get("from", tr.get("source"))
        target = tr.get("to", tr.get("target"))
        if source not in state_names:
            raise fail("E_SCHEMA", f"transition {tr_id} source state {source!r} is undeclared")
        if target not in state_names:
            raise fail("E_SCHEMA", f"transition {tr_id} target state {target!r} is undeclared")
        operator = tr.get("trigger", "condition")
        if operator not in ("condition", "clip_finished", "always"):
            raise fail("E_SCHEMA", f"transition {tr_id} has unsupported trigger {operator!r}")
        conditions: list[dict[str, Any]] = []
        for c_index, raw_condition in enumerate(require_list(tr.get("conditions", []), f"transition {tr_id}.conditions")):
            cond = require_dict(raw_condition, f"transition {tr_id}.conditions[{c_index}]")
            param = cond.get("param", cond.get("parameter"))
            if param not in parameter_names:
                raise fail("E_PARAM_UNDECLARED", f"condition references undeclared parameter {param!r}", transition=tr_id)
            op = cond.get("op", cond.get("operator", ">="))
            if op not in (">=", "<=", ">", "<", "==", "!="):
                raise fail("E_SCHEMA", f"unsupported comparison operator {op!r}")
            value = cond.get("value")
            if not isinstance(value, bool):
                finite_number(value, f"transition {tr_id}.conditions[{c_index}].value")
            conditions.append({"param": param, "op": op, "value": value})
        if operator == "condition" and not conditions:
            raise fail("E_SCHEMA", f"transition {tr_id} with trigger=condition requires conditions")
        priority = tr.get("priority", 0)
        if not is_int(priority):
            raise fail("E_SCHEMA", f"transition {tr_id} priority must be an integer")
        interruptible = tr.get("interruptible", True)
        if not isinstance(interruptible, bool):
            raise fail("E_SCHEMA", f"transition {tr_id} interruptible must be bool")
        transitions.append(
            {
                "id": tr_id,
                "from": source,
                "to": target,
                "priority": int(priority),
                "trigger": operator,
                "conditions": conditions,
                "interruptible": interruptible,
            }
        )

    return {
        "initial_state": initial,
        "states": states,
        "parameters": normalized_parameters,
        "transitions": transitions,
        "parameter_names": sorted(parameter_names),
    }


def normalize_spec(doc: Any | None) -> dict[str, Any]:
    root = {} if doc is None else require_dict(doc, "spec/chunk_spec")
    step_ms = require_positive_int(root.get("step_ms", root.get("frame_step_ms", 1)), "spec.step_ms")
    chunk_frames = require_positive_int(root.get("chunk_frames", root.get("frames_per_chunk", 64)), "spec.chunk_frames")
    timeline_ms = require_nonnegative_int(root.get("timeline_ms", root.get("duration_ms", 1024)), "spec.timeline_ms")
    tolerance = finite_number(root.get("pose_tolerance", 1e-8), "spec.pose_tolerance")
    if tolerance < 0:
        raise fail("E_SCHEMA", "pose_tolerance must be nonnegative")
    max_workers = root.get("max_workers", 4)
    max_workers = require_positive_int(max_workers, "spec.max_workers")
    memory_budget = root.get("memory_budget_bytes", 64 * 1024 * 1024)
    memory_budget = require_positive_int(memory_budget, "spec.memory_budget_bytes")
    entry_blend_ms = require_nonnegative_int(root.get("entry_blend_ms", 0), "spec.entry_blend_ms")
    return {
        "step_ms": step_ms,
        "chunk_frames": chunk_frames,
        "timeline_ms": timeline_ms,
        "pose_tolerance": float(tolerance),
        "max_workers": max_workers,
        "memory_budget_bytes": memory_budget,
        "entry_blend_ms": entry_blend_ms,
    }


def normalize_project(
    skeleton_doc: Any,
    clips_doc: Any,
    state_machine_doc: Any,
    spec_doc: Any | None = None,
) -> dict[str, Any]:
    skeleton = validate_skeleton(skeleton_doc)
    clips = normalize_clips(clips_doc, skeleton)
    state_machine = normalize_state_machine(state_machine_doc, clips)
    spec = normalize_spec(spec_doc)
    return {
        "schema": "animrecalc/v1",
        "skeleton": skeleton,
        "clips": clips,
        "state_machine": state_machine,
        "spec": spec,
    }


def project_version(project: dict[str, Any]) -> str:
    """Global version from normalized skeleton, tracks and state rules."""
    payload = {
        "skeleton": project["skeleton"],
        "clips": project["clips"],
        "state_machine": project["state_machine"],
        "spec": {
            # Sampling rules change values even when chunk sizing differs.
            "step_ms": project["spec"]["step_ms"],
            "pose_tolerance": project["spec"]["pose_tolerance"],
            "entry_blend_ms": project["spec"]["entry_blend_ms"],
        },
    }
    return canonical_digest(payload)


def load_project_files(
    skeleton_path: str | Path,
    clips_path: str | Path,
    state_machine_path: str | Path,
    spec_path: str | Path | None = None,
) -> dict[str, Any]:
    skeleton_doc = load_json_file(skeleton_path, "skeleton")
    clips_doc = load_json_file(clips_path, "clips")
    sm_doc = load_json_file(state_machine_path, "state_machine")
    spec_doc = load_json_file(spec_path, "chunk_spec") if spec_path else None
    return normalize_project(skeleton_doc, clips_doc, sm_doc, spec_doc)


def deep_copy_project(project: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(project)


# Re-exported for a compact public import path.
__all__ = [
    "canonical_channel",
    "deep_copy_project",
    "load_json_file",
    "load_project_files",
    "normalize_project",
    "project_version",
]
