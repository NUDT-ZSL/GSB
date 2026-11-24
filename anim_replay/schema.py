from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .canonical import sha256_value
from .errors import SchemaError
from .math3d import (
    Quat,
    Transform,
    Vec3,
    is_finite_number,
    quat,
    quat_norm,
    vec3,
)
from .models import (
    CHANNELS,
    Bone,
    Clip,
    Condition,
    Keyframe,
    Parameter,
    Spec,
    Timeline,
    Track,
    Transition,
)

OPS = {"<", "<=", ">", ">=", "==", "!="}
NUMERIC_OPS = {"<", "<=", ">", ">="}
EDIT_OPS = {
    "set_keyframe_value",
    "add_keyframe",
    "delete_keyframe",
    "move_keyframe",
    "set_clip_duration",
    "set_clip_loop_count",
    "set_transition_condition",
    "add_transition",
    "delete_transition",
}


@dataclass
class LoadedInput:
    spec: Spec
    raw: dict[str, Any]
    source_path: str | None = None
    source_digest: str = ""


@dataclass
class EditRecord:
    index: int
    op: str
    target: dict[str, Any]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edit_index": self.index,
            "op": self.op,
            "target": self.target,
            "details": self.details,
        }


def _fail(message: str, location: str, **details: Any) -> None:
    raise SchemaError(message, location=location, details=details)


def _obj(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("expected JSON object", location)
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("expected JSON array", location)
    return value


def _str(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _fail("expected non-empty string", location)
    return value


def _int(value: Any, location: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail("expected integer", location)
    if minimum is not None and value < minimum:
        _fail(f"integer must be >= {minimum}", location, value=value)
    return value


def _bool(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        _fail("expected boolean", location)
    return value


def _finite(value: Any, location: str) -> float:
    if not is_finite_number(value):
        _fail("expected finite number", location, value=repr(value))
    return float(value)


def _vector(value: Any, length: int, location: str) -> tuple[float, ...]:
    values = _list(value, location)
    if len(values) != length:
        _fail(f"expected vector of length {length}", location, length=len(values))
    return tuple(_finite(item, f"{location}[{index}]") for index, item in enumerate(values))


def _quat(value: Any, location: str) -> Quat:
    values = _vector(value, 4, location)
    q = quat(values)
    norm = quat_norm(q)
    if not math.isfinite(norm) or norm <= 1e-12:
        _fail("rotation quaternion must have a finite non-zero norm", location)
    return q


def _rest_transform(value: Any, location: str) -> Transform:
    data = _obj(value, location)
    if "translation" not in data:
        _fail("rest pose is missing translation", f"{location}.translation")
    if "rotation" not in data:
        _fail("rest pose is missing rotation", f"{location}.rotation")
    translation = vec3(_vector(data["translation"], 3, f"{location}.translation"))
    rotation = _quat(data["rotation"], f"{location}.rotation")
    scale = vec3(_vector(data.get("scale", [1.0, 1.0, 1.0]), 3, f"{location}.scale"))
    return (translation, rotation, scale)


def _validate_bones(value: Any) -> tuple[tuple[Bone, ...], tuple[str, ...]]:
    data = _obj(value, "$.skeleton")
    bones_raw = _list(data.get("bones"), "$.skeleton.bones")
    if not bones_raw:
        _fail("skeleton must contain at least one bone", "$.skeleton.bones")

    bones: dict[str, Bone] = {}
    for index, item in enumerate(bones_raw):
        location = f"$.skeleton.bones[{index}]"
        raw = _obj(item, location)
        name = _str(raw.get("name"), f"{location}.name")
        if "." in name:
            _fail("bone name must not contain '.' (used by the flat v1 format)", f"{location}.name")
        if name in bones:
            _fail("duplicate bone name", f"{location}.name", bone=name)
        parent_raw = raw.get("parent")
        parent: str | None
        if parent_raw is None:
            parent = None
        else:
            parent = _str(parent_raw, f"{location}.parent")
        if parent == name:
            _fail("bone cannot be its own parent", f"{location}.parent", bone=name)
        rest = _rest_transform(raw.get("rest"), f"{location}.rest")
        bones[name] = Bone(name=name, parent=parent, rest=rest)

    for bone in bones.values():
        if bone.parent is not None and bone.parent not in bones:
            _fail(
                "parent bone is not declared",
                f"$.skeleton.bones[{bone.name}].parent",
                parent=bone.parent,
            )

    children: dict[str, list[str]] = {name: [] for name in bones}
    indegree: dict[str, int] = {name: 0 for name in bones}
    for bone in bones.values():
        if bone.parent is not None:
            children[bone.parent].append(bone.name)
            indegree[bone.name] += 1

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in sorted(children[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(bones):
        cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
        _fail("skeleton contains a parent cycle", "$.skeleton.bones", bones=cyclic)

    return tuple(bones[name] for name in sorted(bones)), tuple(order)


def _validate_parameters(value: Any) -> dict[str, Parameter]:
    data = _obj(value if value is not None else {}, "$.parameters")
    parameters: dict[str, Parameter] = {}
    for name, item in data.items():
        location = f"$.parameters.{name}"
        _str(name, location)
        raw = _obj(item, location)
        if "default" not in raw:
            _fail("parameter is missing default", f"{location}.default")
        default = _parameter_value(raw["default"], f"{location}.default")
        track_raw = raw.get("track", [])
        track_items = _list(track_raw, f"{location}.track")
        track: list[tuple[int, Any]] = []
        previous = -1
        for index, key_raw in enumerate(track_items):
            key_location = f"{location}.track[{index}]"
            key = _obj(key_raw, key_location)
            t = _int(key.get("t"), f"{key_location}.t", minimum=0)
            if t <= previous:
                _fail(
                    "parameter keyframe times must be strictly increasing",
                    f"{key_location}.t",
                )
            previous = t
            param_value = _parameter_value(key.get("value"), f"{key_location}.value")
            if _parameter_kind(param_value) != _parameter_kind(default):
                _fail(
                    "parameter track value type differs from default",
                    f"{key_location}.value",
                )
            track.append((t, param_value))
        parameters[name] = Parameter(name=name, default=default, track=tuple(track))
    return parameters


def _parameter_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string"
    if is_finite_number(value):
        return "number"
    return "invalid"


def _parameter_value(value: Any, location: str) -> Any:
    kind = _parameter_kind(value)
    if kind == "invalid":
        _fail("expected finite number, boolean, or string", location, value=repr(value))
    if kind == "number":
        return float(value)
    return value


def _validate_condition(value: Any, location: str, parameters: Mapping[str, Parameter]) -> Condition:
    raw = _obj(value, location)
    param = _str(raw.get("param"), f"{location}.param")
    if param not in parameters:
        _fail("transition condition references an undeclared parameter", f"{location}.param", parameter=param)
    op = _str(raw.get("op"), f"{location}.op")
    if op not in OPS:
        _fail("unsupported condition operator", f"{location}.op", operator=op)
    if "value" not in raw:
        _fail("condition is missing threshold value", f"{location}.value")
    threshold = _parameter_value(raw["value"], f"{location}.value")
    if op in NUMERIC_OPS and (
        _parameter_kind(parameters[param].default) != "number" or _parameter_kind(threshold) != "number"
    ):
        _fail("ordered comparison requires numeric parameter and threshold", location)
    return Condition(param=param, op=op, value=threshold)


def _validate_clips(
    value: Any,
    bones: Mapping[str, Bone],
    parameters: Mapping[str, Parameter],
) -> dict[str, Clip]:
    clips_raw = _list(value, "$.clips")
    if not clips_raw:
        _fail("at least one clip is required", "$.clips")
    clips: dict[str, Clip] = {}
    for index, item in enumerate(clips_raw):
        location = f"$.clips[{index}]"
        raw = _obj(item, location)
        clip_id = _str(raw.get("id"), f"{location}.id")
        if clip_id in clips:
            _fail("duplicate clip id", f"{location}.id", clip=clip_id)
        duration = _int(raw.get("duration_ms"), f"{location}.duration_ms", minimum=1)
        loop_count = _int(raw.get("loop_count", 0), f"{location}.loop_count", minimum=0)
        tracks_raw = _obj(raw.get("tracks", {}), f"{location}.tracks")
        tracks: dict[str, dict[str, Track]] = {}
        for bone_name, channels_raw in tracks_raw.items():
            if bone_name not in bones:
                _fail("track references an undeclared bone", f"{location}.tracks.{bone_name}", bone=bone_name)
            channels = _obj(channels_raw, f"{location}.tracks.{bone_name}")
            bone_tracks: dict[str, Track] = {}
            for channel, keys_raw in channels.items():
                channel_location = f"{location}.tracks.{bone_name}.{channel}"
                if channel not in CHANNELS:
                    _fail("unsupported animation channel", channel_location, channel=channel)
                key_items = _list(keys_raw, channel_location)
                keys: list[Keyframe] = []
                previous = -1
                vector_length = 4 if channel == "rotation" else 3
                for key_index, key_raw in enumerate(key_items):
                    key_location = f"{channel_location}[{key_index}]"
                    key = _obj(key_raw, key_location)
                    t = _int(key.get("t"), f"{key_location}.t", minimum=0)
                    if t <= previous:
                        _fail(
                            "keyframe times must be strictly increasing within a bone channel",
                            f"{key_location}.t",
                            previous=previous,
                            current=t,
                        )
                    if t > duration:
                        _fail("keyframe time exceeds clip duration", f"{key_location}.t")
                    previous = t
                    values = _vector(key.get("value"), vector_length, f"{key_location}.value")
                    if channel == "rotation":
                        norm = quat_norm(quat(values))
                        if not math.isfinite(norm) or norm <= 1e-12:
                            _fail("rotation keyframe must have a finite non-zero norm", f"{key_location}.value")
                    keys.append(Keyframe(t=t, value=values))
                bone_tracks[channel] = Track(
                    bone=bone_name,
                    channel=channel,
                    keyframes=tuple(keys),
                )
            tracks[bone_name] = bone_tracks
        clips[clip_id] = Clip(
            id=clip_id,
            duration_ms=duration,
            loop_count=loop_count,
            tracks=tracks,
        )
    return clips


def _validate_transitions(
    value: Any,
    clips: Mapping[str, Clip],
    parameters: Mapping[str, Parameter],
    initial_clip: str,
) -> tuple[Transition, ...]:
    raw = _obj(value, "$.state_machine")
    initial = _str(raw.get("initial_clip", initial_clip), "$.state_machine.initial_clip")
    if initial not in clips:
        _fail("initial clip is not declared", "$.state_machine.initial_clip", clip=initial)
    items = _list(raw.get("transitions", []), "$.state_machine.transitions")
    transitions: list[Transition] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        location = f"$.state_machine.transitions[{index}]"
        transition_raw = _obj(item, location)
        transition_id = _str(transition_raw.get("id"), f"{location}.id")
        if transition_id in seen:
            _fail("duplicate transition id", f"{location}.id", transition=transition_id)
        seen.add(transition_id)
        source_raw = transition_raw.get("from")
        if isinstance(source_raw, list):
            sources = tuple(_str(source, f"{location}.from[{idx}]") for idx, source in enumerate(source_raw))
        else:
            sources = (_str(source_raw, f"{location}.from"),)
        if not sources:
            _fail("transition must declare at least one source clip", f"{location}.from")
        for source in sources:
            if source != "*" and source not in clips:
                _fail("transition source clip is not declared", f"{location}.from", clip=source)
        target = _str(transition_raw.get("to"), f"{location}.to")
        if target not in clips:
            _fail("transition target clip is not declared", f"{location}.to", clip=target)
        when_raw = transition_raw.get("when", [])
        when = tuple(
            _validate_condition(condition, f"{location}.when[{idx}]", parameters)
            for idx, condition in enumerate(_list(when_raw, f"{location}.when"))
        )
        priority = _int(transition_raw.get("priority", 0), f"{location}.priority")
        interrupt = _bool(transition_raw.get("interrupt", False), f"{location}.interrupt")
        on_finish = _bool(transition_raw.get("on_finish", False), f"{location}.on_finish")
        loop_count_raw = transition_raw.get("loop_count")
        loop_count = (
            None
            if loop_count_raw is None
            else _int(loop_count_raw, f"{location}.loop_count", minimum=0)
        )
        min_hold_ms = _int(
            transition_raw.get("min_hold_ms", 0),
            f"{location}.min_hold_ms",
            minimum=0,
        )
        transitions.append(
            Transition(
                id=transition_id,
                sources=sources,
                target=target,
                when=when,
                priority=priority,
                interrupt=interrupt,
                loop_count=loop_count,
                min_hold_ms=min_hold_ms,
                on_finish=on_finish,
            )
        )
    return tuple(transitions)


def _validate_timeline(value: Any) -> Timeline:
    raw = _obj(value, "$.timeline")
    duration = _int(raw.get("duration_ms"), "$.timeline.duration_ms", minimum=0)
    step = _int(raw.get("frame_step_ms"), "$.timeline.frame_step_ms", minimum=1)
    chunk = _int(raw.get("chunk_ms", step), "$.timeline.chunk_ms", minimum=1)
    if duration % step != 0:
        _fail("timeline duration must be a multiple of frame step", "$.timeline.duration_ms")
    if chunk % step != 0:
        _fail("chunk size must be a multiple of frame step", "$.timeline.chunk_ms")
    return Timeline(duration_ms=duration, frame_step_ms=step, chunk_ms=chunk)


def validate_input(data: Any, *, source_path: str | None = None) -> LoadedInput:
    raw = _obj(data, "$")
    if raw.get("format") != "anim-replay-input-v1":
        _fail("unsupported input format", "$.format", expected="anim-replay-input-v1")
    seed_raw = raw.get("seed")
    seed = None if seed_raw is None else _int(seed_raw, "$.seed")
    bones, order = _validate_bones(raw.get("skeleton"))
    bone_map = {bone.name: bone for bone in bones}
    parameters = _validate_parameters(raw.get("parameters", {}))
    clips = _validate_clips(raw.get("clips"), bone_map, parameters)
    machine = _obj(raw.get("state_machine"), "$.state_machine")
    initial = _str(machine.get("initial_clip"), "$.state_machine.initial_clip")
    transitions = _validate_transitions(machine, clips, parameters, initial)
    timeline = _validate_timeline(raw.get("timeline"))

    spec = Spec(
        seed=seed,
        skeleton=bones,
        bone_order=order,
        clips=clips,
        parameters=parameters,
        transitions=transitions,
        initial_clip=initial,
        timeline=timeline,
        source_digest=sha256_value(raw),
    )
    return LoadedInput(
        spec=spec,
        raw=raw,
        source_path=source_path,
        source_digest=spec.source_digest,
    )


def read_json(path: str | Path, *, label: str = "input") -> Any:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read {label}", location=str(file_path), details={"reason": str(exc)}) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"invalid JSON in {label}",
            location=str(file_path),
            details={"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc


def load_input(path: str | Path) -> LoadedInput:
    data = read_json(path, label="input")
    return validate_input(data, source_path=str(path))


def load_edits(path: str | Path) -> dict[str, Any]:
    data = read_json(path, label="edit script")
    raw = _obj(data, "$")
    if raw.get("format") != "anim-replay-edits-v1":
        _fail("unsupported edit script format", "$.format", expected="anim-replay-edits-v1")
    edits = _list(raw.get("edits"), "$.edits")
    for index, item in enumerate(edits):
        edit = _obj(item, f"$.edits[{index}]")
        op = _str(edit.get("op"), f"$.edits[{index}].op")
        if op not in EDIT_OPS:
            _fail("unsupported edit operation", f"$.edits[{index}].op", op=op)
    return raw


def _find_clip(raw: dict[str, Any], clip_id: str, location: str) -> dict[str, Any]:
    for clip in raw["clips"]:
        if clip["id"] == clip_id:
            return clip
    _fail("edit references an undeclared clip", location, clip=clip_id)
    raise AssertionError("unreachable")


def _find_transition(raw: dict[str, Any], transition_id: str, location: str) -> dict[str, Any]:
    for transition in raw["state_machine"].get("transitions", []):
        if transition.get("id") == transition_id:
            return transition
    _fail("edit references an undeclared transition", location, transition=transition_id)
    raise AssertionError("unreachable")


def _track_for_edit(
    raw: dict[str, Any],
    *,
    clip_id: str,
    bone: str,
    channel: str,
    location: str,
    create: bool,
) -> list[dict[str, Any]]:
    clip = _find_clip(raw, clip_id, f"{location}.clip")
    tracks = clip.setdefault("tracks", {})
    bone_tracks = tracks.setdefault(bone, {})
    if not create and channel not in bone_tracks:
        _fail("edit references a channel with no keyframe track", f"{location}.channel")
    if create:
        bone_tracks.setdefault(channel, [])
    return bone_tracks[channel]


def _keyframe_index(keys: list[dict[str, Any]], t: int) -> int:
    for index, key in enumerate(keys):
        if key.get("t") == t:
            return index
    return -1


def _apply_one(raw: dict[str, Any], edit: Mapping[str, Any], index: int) -> EditRecord:
    location = f"$.edits[{index}]"
    op = _str(edit.get("op"), f"{location}.op")
    target: dict[str, Any] = {}
    details: dict[str, Any] = {}

    if op in {"set_keyframe_value", "add_keyframe", "delete_keyframe", "move_keyframe"}:
        clip_id = _str(edit.get("clip"), f"{location}.clip")
        bone = _str(edit.get("bone"), f"{location}.bone")
        channel = _str(edit.get("channel"), f"{location}.channel")
        target = {"clip": clip_id, "bone": bone, "channel": channel}
        keys = _track_for_edit(
            raw,
            clip_id=clip_id,
            bone=bone,
            channel=channel,
            location=location,
            create=op == "add_keyframe",
        )
        if op == "add_keyframe":
            t = _int(edit.get("t"), f"{location}.t", minimum=0)
            if _keyframe_index(keys, t) >= 0:
                _fail("add_keyframe target time already exists", f"{location}.t", t=t)
            if "value" not in edit:
                _fail("add_keyframe is missing value", f"{location}.value")
            keys.append({"t": t, "value": edit["value"]})
            keys.sort(key=lambda item: item["t"])
            details = {"t": t, "value": edit["value"]}
        elif op == "delete_keyframe":
            t = _int(edit.get("t"), f"{location}.t", minimum=0)
            key_index = _keyframe_index(keys, t)
            if key_index < 0:
                _fail("delete_keyframe target time does not exist", f"{location}.t", t=t)
            old = keys.pop(key_index)
            details = {"t": t, "removed_value": old.get("value")}
        elif op == "set_keyframe_value":
            t = _int(edit.get("t"), f"{location}.t", minimum=0)
            key_index = _keyframe_index(keys, t)
            if key_index < 0:
                _fail("set_keyframe_value target time does not exist", f"{location}.t", t=t)
            if "value" not in edit:
                _fail("set_keyframe_value is missing value", f"{location}.value")
            old = keys[key_index].get("value")
            keys[key_index]["value"] = edit["value"]
            details = {"t": t, "old_value": old, "new_value": edit["value"]}
        else:
            old_t = _int(edit.get("t"), f"{location}.t", minimum=0)
            new_t = _int(edit.get("new_t"), f"{location}.new_t", minimum=0)
            key_index = _keyframe_index(keys, old_t)
            if key_index < 0:
                _fail("move_keyframe source time does not exist", f"{location}.t", t=old_t)
            if _keyframe_index(keys, new_t) >= 0:
                _fail("move_keyframe destination time already exists", f"{location}.new_t", t=new_t)
            keys[key_index]["t"] = new_t
            keys.sort(key=lambda item: item["t"])
            details = {"old_t": old_t, "new_t": new_t}

    elif op == "set_clip_duration":
        clip_id = _str(edit.get("clip"), f"{location}.clip")
        clip = _find_clip(raw, clip_id, f"{location}.clip")
        old = clip.get("duration_ms")
        clip["duration_ms"] = _int(edit.get("duration_ms"), f"{location}.duration_ms", minimum=1)
        target = {"clip": clip_id}
        details = {"old_duration_ms": old, "new_duration_ms": clip["duration_ms"]}

    elif op == "set_clip_loop_count":
        clip_id = _str(edit.get("clip"), f"{location}.clip")
        clip = _find_clip(raw, clip_id, f"{location}.clip")
        old = clip.get("loop_count", 0)
        clip["loop_count"] = _int(edit.get("loop_count"), f"{location}.loop_count", minimum=0)
        target = {"clip": clip_id}
        details = {"old_loop_count": old, "new_loop_count": clip["loop_count"]}

    elif op == "set_transition_condition":
        transition_id = _str(edit.get("transition"), f"{location}.transition")
        transition = _find_transition(raw, transition_id, f"{location}.transition")
        old_when = copy.deepcopy(transition.get("when", []))
        old_on_finish = transition.get("on_finish", False)
        if "when" in edit:
            transition["when"] = edit["when"]
        if "on_finish" in edit:
            transition["on_finish"] = edit["on_finish"]
        target = {"transition": transition_id}
        details = {
            "old_when": old_when,
            "new_when": copy.deepcopy(transition.get("when", [])),
            "old_on_finish": old_on_finish,
            "new_on_finish": transition.get("on_finish", False),
        }

    elif op == "add_transition":
        transition = _obj(edit.get("transition"), f"{location}.transition")
        transition_id = _str(transition.get("id"), f"{location}.transition.id")
        _find = [
            item
            for item in raw["state_machine"].setdefault("transitions", [])
            if item.get("id") == transition_id
        ]
        if _find:
            _fail("add_transition id already exists", f"{location}.transition.id")
        raw["state_machine"]["transitions"].append(copy.deepcopy(transition))
        target = {"transition": transition_id}
        details = {"transition": copy.deepcopy(transition)}

    elif op == "delete_transition":
        transition_id = _str(edit.get("transition"), f"{location}.transition")
        _find_transition(raw, transition_id, f"{location}.transition")
        old = None
        remaining = []
        for item in raw["state_machine"].get("transitions", []):
            if item.get("id") == transition_id:
                old = copy.deepcopy(item)
            else:
                remaining.append(item)
        raw["state_machine"]["transitions"] = remaining
        target = {"transition": transition_id}
        details = {"removed_transition": old}
    else:
        _fail("unsupported edit operation", f"{location}.op", op=op)

    return EditRecord(index=index, op=op, target=target, details=details)


def apply_edits(
    loaded: LoadedInput,
    edit_script: Mapping[str, Any],
    *,
    source_path: str | None = None,
) -> tuple[LoadedInput, list[EditRecord]]:
    if edit_script.get("format") != "anim-replay-edits-v1":
        _fail("unsupported edit script format", "$.format", expected="anim-replay-edits-v1")
    edits = _list(edit_script.get("edits"), "$.edits")
    raw = copy.deepcopy(loaded.raw)
    records: list[EditRecord] = []
    current = loaded
    for index, edit in enumerate(edits):
        record = _apply_one(raw, _obj(edit, f"$.edits[{index}]"), index)
        try:
            current = validate_input(raw, source_path=source_path or loaded.source_path)
        except SchemaError as exc:
            exc.details.setdefault("edit_index", index)
            exc.details.setdefault("edit_op", record.op)
            raise
        records.append(record)
    if edits and not records:
        current = validate_input(raw, source_path=source_path or loaded.source_path)
    return current, records


def normalized_spec(spec: Spec) -> dict[str, Any]:
    bones = [
        {
            "name": bone.name,
            "parent": bone.parent,
            "rest": {
                "translation": list(bone.rest[0]),
                "rotation": list(bone.rest[1]),
                "scale": list(bone.rest[2]),
            },
        }
        for bone in sorted(spec.skeleton, key=lambda item: item.name)
    ]
    clips = []
    for clip_id in sorted(spec.clips):
        clip = spec.clips[clip_id]
        tracks = []
        for bone_name in sorted(clip.tracks):
            for channel in sorted(clip.tracks[bone_name]):
                track = clip.tracks[bone_name][channel]
                tracks.append(
                    {
                        "bone": bone_name,
                        "channel": channel,
                        "keyframes": [
                            {"t": key.t, "value": list(key.value)} for key in track.keyframes
                        ],
                    }
                )
        clips.append(
            {
                "id": clip.id,
                "duration_ms": clip.duration_ms,
                "loop_count": clip.loop_count,
                "tracks": tracks,
            }
        )
    parameters = [
        {
            "name": parameter.name,
            "default": parameter.default,
            "track": [{"t": t, "value": value} for t, value in parameter.track],
        }
        for parameter in sorted(spec.parameters.values(), key=lambda item: item.name)
    ]
    transitions = [
        {
            "id": transition.id,
            "from": list(transition.sources),
            "to": transition.target,
            "when": sorted(
                (
                    {
                        "param": condition.param,
                        "op": condition.op,
                        "value": condition.value,
                    }
                    for condition in transition.when
                ),
                key=lambda item: (item["param"], item["op"], repr(item["value"])),
            ),
            "priority": transition.priority,
            "interrupt": transition.interrupt,
            "loop_count": transition.loop_count,
            "min_hold_ms": transition.min_hold_ms,
            "on_finish": transition.on_finish,
        }
        for transition in sorted(spec.transitions, key=lambda item: item.id)
    ]
    return {
        "skeleton": bones,
        "clips": clips,
        "parameters": parameters,
        "state_machine": {
            "initial_clip": spec.initial_clip,
            "transitions": transitions,
        },
    }


def version_id(spec: Spec) -> str:
    return sha256_value(normalized_spec(spec))


def descendants(spec: Spec, bone: str) -> tuple[str, ...]:
    children: dict[str, list[str]] = {item.name: [] for item in spec.skeleton}
    for item in spec.skeleton:
        if item.parent is not None:
            children[item.parent].append(item.name)
    result: list[str] = []
    stack = [bone]
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(sorted(children[current], reverse=True))
    return tuple(result)


def ancestors(spec: Spec, bone: str) -> tuple[str, ...]:
    by_name = {item.name: item for item in spec.skeleton}
    result: list[str] = []
    current = by_name[bone].parent
    while current is not None:
        result.append(current)
        current = by_name[current].parent
    return tuple(result)

