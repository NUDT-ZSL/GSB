from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .errors import AnimRecalcError
from .pipeline import (
    canonical_channel,
    load_json_file,
    normalize_state_machine,
    _normalize_track,
)
from .validation import fail, is_int, require_dict, require_positive_int


ALLOWED_OPERATIONS = {
    "add_key",
    "insert_key",
    "upsert_key",
    "set_key",
    "update_key",
    "update_key_value",
    "set_channel_value",
    "change_key_value",
    "delete_key",
    "remove_key",
    "set_clip_duration",
    "update_clip_duration",
    "change_clip_duration",
    "add_transition",
    "update_transition",
    "delete_transition",
    "add_transition_condition",
    "update_transition_condition",
    "delete_transition_condition",
}

OP_ALIASES = {
    "insert_key": "add_key",
    "set_key": "upsert_key",
    "change_key_value": "update_key_value",
    "set_channel_value": "update_key_value",
    "remove_key": "delete_key",
    "update_clip_duration": "set_clip_duration",
    "change_clip_duration": "set_clip_duration",
}


def load_edit_script(path: str | Path) -> list[dict[str, Any]]:
    doc = load_json_file(path, "edit_script")
    return parse_edit_script(doc)


def parse_edit_script(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        raw_edits = doc
    else:
        root = require_dict(doc, "edit_script")
        raw_edits = root.get("edits", root.get("operations", []))
    if not isinstance(raw_edits, list):
        raise fail("E_SCHEMA", "edit script edits must be an array")
    edits: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_edits):
        op = require_dict(raw, f"edits[{index}]")
        op_name = op.get("type") or op.get("operation") or op.get("edit_op")
        if op_name is None:
            candidate = op.get("op")
            op_name = candidate if candidate in ALLOWED_OPERATIONS else op.get("type")
        if op_name not in ALLOWED_OPERATIONS:
            raise fail(
                "E_OP_UNSUPPORTED",
                f"edit operation {op_name!r} is not allowed",
                index=index,
                allowed=sorted(ALLOWED_OPERATIONS),
            )
        normalized = dict(op)
        normalized["op"] = OP_ALIASES.get(op_name, op_name)
        normalized.pop("type", None)
        normalized.pop("operation", None)
        edits.append(normalized)
    return edits


def _find_clip(project: dict[str, Any], clip_id: Any) -> dict[str, Any]:
    for clip in project["clips"]:
        if clip["id"] == clip_id:
            return clip
    raise fail("E_SCHEMA", f"edit references unknown clip {clip_id!r}")


def _find_track(clip: dict[str, Any], bone: Any, channel: Any) -> dict[str, Any] | None:
    canonical = canonical_channel(channel)
    for track in clip["tracks"]:
        if track["bone"] == bone and track["channel"] == canonical:
            return track
    return None


def _require_track(project: dict[str, Any], op: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    clip = _find_clip(project, op.get("clip", op.get("clip_id")))
    bone = op.get("bone")
    if bone not in project["skeleton"]["name_to_index"]:
        raise fail("E_SCHEMA", f"edit references unknown bone {bone!r}")
    track = _find_track(clip, bone, op.get("channel"))
    if track is None:
        raise fail(
            "E_SCHEMA",
            "track does not exist",
            clip=clip["id"],
            bone=bone,
            channel=op.get("channel"),
        )
    return clip, track


def _revalidate_track(project: dict[str, Any], track: dict[str, Any]) -> None:
    bone_names = set(project["skeleton"]["name_to_index"])
    index = 0
    for i, clip in enumerate(project["clips"]):
        for j, candidate in enumerate(clip["tracks"]):
            if candidate is track:
                index = j
                break
    normalized = _normalize_track(track, index, bone_names)
    track.clear()
    track.update(normalized)


def _key_time(op: dict[str, Any], field: str = "time") -> int:
    value = op.get(field, op.get(field + "_ms", op.get("t")))
    if not is_int(value) or value < 0:
        raise fail("E_KEY_TIME", f"edit {field} must be a nonnegative integer", value=value)
    return int(value)


def _value(op: dict[str, Any]) -> Any:
    if "value" in op:
        return op["value"]
    if "values" in op:
        return op["values"]
    raise fail("E_SCHEMA", "key edit requires value")


def _add_key(project: dict[str, Any], op: dict[str, Any], upsert: bool) -> None:
    clip = _find_clip(project, op.get("clip", op.get("clip_id")))
    bone = op.get("bone")
    if bone not in project["skeleton"]["name_to_index"]:
        raise fail("E_SCHEMA", f"edit references unknown bone {bone!r}")
    channel = canonical_channel(op.get("channel"))
    track = _find_track(clip, bone, channel)
    if track is None:
        track = {"bone": bone, "channel": channel, "interpolation": "slerp" if channel == "rotation" else "linear", "keys": []}
        clip["tracks"].append(track)
    time = _key_time(op)
    for key in track["keys"]:
        if key["time"] == time:
            if not upsert:
                raise fail("E_EDIT_CONFLICT", "key already exists", clip=clip["id"], bone=bone, channel=channel, time=time)
            key["value"] = _value(op)
            _revalidate_track(project, track)
            return
    track["keys"].append({"time": time, "value": _value(op)})
    track["keys"].sort(key=lambda key: key["time"])
    _revalidate_track(project, track)


def _update_key(project: dict[str, Any], op: dict[str, Any]) -> None:
    _, track = _require_track(project, op)
    time = _key_time(op)
    key = next((item for item in track["keys"] if item["time"] == time), None)
    if key is None:
        raise fail("E_EDIT_NOT_FOUND", "key not found", time=time)
    if "value" in op or "values" in op:
        key["value"] = _value(op)
    if "new_time" in op or "new_time_ms" in op or "new_t" in op:
        new_time = op.get("new_time", op.get("new_time_ms", op.get("new_t")))
        if not is_int(new_time) or new_time < 0:
            raise fail("E_KEY_TIME", "new_time must be a nonnegative integer", value=new_time)
        if any(item is not key and item["time"] == int(new_time)):
            raise fail("E_EDIT_CONFLICT", "another key already uses new_time", new_time=new_time)
        key["time"] = int(new_time)
        track["keys"].sort(key=lambda item: item["time"])
    _revalidate_track(project, track)


def _delete_key(project: dict[str, Any], op: dict[str, Any]) -> None:
    _, track = _require_track(project, op)
    time = _key_time(op)
    before = len(track["keys"])
    track["keys"] = [key for key in track["keys"] if key["time"] != time]
    if len(track["keys"]) == before:
        raise fail("E_EDIT_NOT_FOUND", "key not found", time=time)
    _revalidate_track(project, track)


def _set_duration(project: dict[str, Any], op: dict[str, Any]) -> None:
    clip = _find_clip(project, op.get("clip", op.get("clip_id")))
    duration = op.get("duration_ms", op.get("duration"))
    clip["duration_ms"] = require_positive_int(duration, "duration_ms")


def _find_transition(sm: dict[str, Any], transition_id: Any) -> dict[str, Any]:
    transition = next((item for item in sm["transitions"] if item["id"] == transition_id), None)
    if transition is None:
        raise fail("E_EDIT_NOT_FOUND", f"transition {transition_id!r} not found")
    return transition


def _renormalize_sm(project: dict[str, Any]) -> None:
    project["state_machine"] = normalize_state_machine(project["state_machine"], project["clips"])


def _transition_mutation(project: dict[str, Any], op: dict[str, Any], action: str) -> None:
    sm = project["state_machine"]
    if action == "add":
        raw = op.get("transition", op)
        if "id" not in raw and "transition_id" in op:
            raw = dict(raw, id=op["transition_id"])
        sm["transitions"].append(copy.deepcopy(raw))
        _renormalize_sm(project)
        return

    transition_id = op.get("transition_id", op.get("id"))
    transition = _find_transition(sm, transition_id)

    if action == "update":
        for field in ("from", "source", "to", "target", "priority", "interruptible", "trigger"):
            if field in op:
                transition[field] = op[field]
        if "conditions" in op:
            transition["conditions"] = copy.deepcopy(op["conditions"])
        _renormalize_sm(project)
        return

    if action == "delete":
        sm["transitions"] = [item for item in sm["transitions"] if item["id"] != transition_id]
        _renormalize_sm(project)
        return

    condition = op.get("condition") if isinstance(op.get("condition"), dict) else None
    if action == "add_condition":
        if condition is None:
            condition = {
                "param": op.get("param"),
                "op": op.get("comparison", op.get("condition_op", ">=")),
                "value": op.get("value"),
            }
        transition["conditions"].append(condition)
        _renormalize_sm(project)
        return

    index = op.get("condition_index")
    if index is None:
        param = op.get("param")
        index = next((i for i, cond in enumerate(transition["conditions"]) if cond["param"] == param), None)
    if not is_int(index) or index < 0 or index >= len(transition["conditions"]):
        raise fail("E_EDIT_NOT_FOUND", "transition condition not found", transition=transition_id)
    index = int(index)

    if action == "update_condition":
        if condition is not None:
            transition["conditions"][index] = condition
        else:
            cond = transition["conditions"][index]
            for field in ("param", "parameter"):
                if field in op:
                    cond["param"] = op[field]
            for field in ("op", "operator", "comparison"):
                if field in op:
                    cond["op"] = op[field]
            if "value" in op:
                cond["value"] = op["value"]
        _renormalize_sm(project)
        return

    if action == "delete_condition":
        transition["conditions"].pop(index)
        _renormalize_sm(project)


def apply_edit(project: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Apply one normalized operation in place and return the project."""
    operation = op["op"]
    if operation == "add_key":
        _add_key(project, op, upsert=False)
    elif operation == "upsert_key":
        _add_key(project, op, upsert=True)
    elif operation == "update_key_value" or operation == "update_key":
        if operation == "update_key_value" and "new_time" not in op and "time" not in op:
            raise fail("E_SCHEMA", "key value edit requires time")
        _update_key(project, op)
    elif operation == "delete_key":
        _delete_key(project, op)
    elif operation == "set_clip_duration":
        _set_duration(project, op)
    elif operation == "add_transition":
        _transition_mutation(project, op, "add")
    elif operation == "update_transition":
        _transition_mutation(project, op, "update")
    elif operation == "delete_transition":
        _transition_mutation(project, op, "delete")
    elif operation == "add_transition_condition":
        _transition_mutation(project, op, "add_condition")
    elif operation == "update_transition_condition":
        _transition_mutation(project, op, "update_condition")
    elif operation == "delete_transition_condition":
        _transition_mutation(project, op, "delete_condition")
    else:  # pragma: no cover - parse prevents this.
        raise fail("E_OP_UNSUPPORTED", f"unsupported operation {operation!r}")
    return project


def dry_run_edits(project: dict[str, Any], edits: list[dict[str, Any]]) -> None:
    """Apply all edits to a disposable copy to validate end-to-end semantics."""
    trial = copy.deepcopy(project)
    for index, op in enumerate(edits):
        try:
            apply_edit(trial, op)
        except AnimRecalcError as exc:
            exc.details.setdefault("edit_index", index)
            raise


def apply_edits(project: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
    for index, op in enumerate(edits):
        try:
            apply_edit(project, op)
        except AnimRecalcError as exc:
            exc.details.setdefault("edit_index", index)
            raise
    return project
