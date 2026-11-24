from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .errors import DomainError, InputError
from .fixed import decimal_from_json, quantize_decimal
from .jsonio import digest_value, load_json
from .models import (
    BoolKey,
    BranchDef,
    Bundle,
    Condition,
    Effect,
    EventDef,
    FixedKey,
    Layer,
    RunConfig,
    SceneDef,
    SceneVisual,
    StateKey,
    StateSchema,
    Story,
    Storyboard,
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

FIXED_OPS = {"eq", "neq", "gt", "gte", "lt", "lte"}
BOOL_OPS = {"eq", "neq", "is"}
FIXED_EFFECT_OPS = {"add", "set"}
BOOL_EFFECT_OPS = {"set", "toggle"}


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(
            "E_SCHEMA", f"{field} must be an object", {"field": field}
        )
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise InputError(
            "E_SCHEMA", f"{field} must be an array", {"field": field}
        )
    return value


def _require_str(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InputError(
            "E_SCHEMA", f"{field} must be a string", {"field": field}
        )
    if not allow_empty and not value.strip():
        raise InputError(
            "E_SCHEMA", f"{field} must not be empty", {"field": field}
        )
    return value


def _require_id(value: Any, field: str) -> str:
    text = _require_str(value, field)
    if not _ID_RE.fullmatch(text):
        raise InputError(
            "E_SCHEMA",
            f"{field} contains unsupported characters",
            {"field": field, "value": text},
        )
    return text


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise InputError(
            "E_SCHEMA", f"{field} must be a boolean", {"field": field}
        )
    return value


def _require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(
            "E_SCHEMA", f"{field} must be an integer", {"field": field}
        )
    if minimum is not None and value < minimum:
        raise InputError(
            "E_TIME" if "time" in field else "E_SCHEMA",
            f"{field} must be >= {minimum}",
            {"field": field, "value": value, "minimum": minimum},
        )
    return value


def _normalize_decimal_text(value: Any, field: str) -> str:
    number = decimal_from_json(value, field=field)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def _parse_state(raw: dict[str, Any]) -> StateSchema:
    root = _require_dict(raw, "state")
    scale = _require_int(root.get("fixed_scale"), "state.fixed_scale", minimum=1)
    if scale > 1_000_000_000:
        raise InputError(
            "E_FIXED",
            "state.fixed_scale is unreasonably large",
            {"fixed_scale": scale},
        )
    tolerance_value = root.get("residual_tolerance")
    if tolerance_value is None:
        tolerance_value = "0"
    tolerance_text = _normalize_decimal_text(
        tolerance_value, "state.residual_tolerance"
    )
    tolerance = Decimal(tolerance_text)
    if tolerance < 0:
        raise InputError(
            "E_DOMAIN",
            "state.residual_tolerance must be non-negative",
            {"residual_tolerance": tolerance_text},
        )

    raw_keys = root.get("keys")
    if raw_keys is None:
        raw_keys = root.get("state_keys")
    if isinstance(raw_keys, dict):
        key_items: list[tuple[str, Any]] = [
            (str(key), value) for key, value in raw_keys.items()
        ]
    elif isinstance(raw_keys, list):
        key_items = []
        for index, item in enumerate(_require_list(raw_keys, "state.keys")):
            obj = _require_dict(item, f"state.keys[{index}]")
            key_items.append((str(obj.get("id", "")), obj))
    else:
        raise InputError(
            "E_SCHEMA",
            "state.keys must be an object or array",
            {"field": "state.keys"},
        )

    keys: dict[str, StateKey] = {}
    for index, (mapping_id, item) in enumerate(key_items):
        field = f"state.keys[{index}]"
        obj = _require_dict(item, field)
        key_id = _require_id(obj.get("id", mapping_id), f"{field}.id")
        if key_id in keys:
            raise InputError(
                "E_STATE_DUP",
                f"duplicate state key: {key_id}",
                {"state_key": key_id},
            )
        label = _require_str(obj.get("label", key_id), f"{field}.label")
        kind = _require_str(obj.get("type", "fixed"), f"{field}.type").lower()
        if kind in {"fixed", "relation", "number", "numeric"}:
            domain = _require_dict(obj.get("domain"), f"{field}.domain")
            min_text = _normalize_decimal_text(
                domain.get("min", "-1000000"), f"{field}.domain.min"
            )
            max_text = _normalize_decimal_text(
                domain.get("max", "1000000"), f"{field}.domain.max"
            )
            minimum = quantize_decimal(min_text, scale, field=f"{field}.domain.min")
            maximum = quantize_decimal(max_text, scale, field=f"{field}.domain.max")
            if minimum > maximum:
                raise InputError(
                    "E_DOMAIN",
                    f"{field}.domain has min greater than max",
                    {"state_key": key_id},
                )
            initial_value = obj.get("initial", "0")
            initial_text = _normalize_decimal_text(initial_value, f"{field}.initial")
            initial = quantize_decimal(
                initial_text, scale, field=f"{field}.initial"
            )
            if initial < minimum or initial > maximum:
                raise InputError(
                    "E_DOMAIN",
                    f"initial value for {key_id} is outside its domain",
                    {
                        "state_key": key_id,
                        "initial": initial_text,
                        "domain": [min_text, max_text],
                    },
                )
            keys[key_id] = FixedKey(
                id=key_id,
                label=label,
                minimum=minimum,
                maximum=maximum,
                initial=initial,
                initial_text=initial_text,
            )
        elif kind in {"bool", "boolean"}:
            initial = _require_bool(obj.get("initial", False), f"{field}.initial")
            keys[key_id] = BoolKey(id=key_id, label=label, initial=initial)
        else:
            raise InputError(
                "E_SCHEMA",
                f"unsupported state key type: {kind}",
                {"state_key": key_id, "type": kind},
            )

    if not keys:
        raise InputError("E_SCHEMA", "state must declare at least one key")
    return StateSchema(
        keys=keys,
        fixed_scale=scale,
        residual_tolerance=tolerance,
        residual_tolerance_text=tolerance_text,
    )


def _parse_condition(
    item: Any,
    field: str,
    state: StateSchema,
) -> Condition:
    obj = _require_dict(item, field)
    key_id = _require_id(obj.get("key"), f"{field}.key")
    key = state.keys.get(key_id)
    if key is None:
        raise InputError(
            "E_STATE_KEY",
            f"condition references undeclared state key: {key_id}",
            {"state_key": key_id, "field": field},
        )
    op = _require_str(obj.get("op"), f"{field}.op")
    if isinstance(key, FixedKey):
        if op not in FIXED_OPS:
            raise InputError(
                "E_OP",
                f"operator {op!r} is not valid for fixed key {key_id}",
                {"state_key": key_id, "op": op},
            )
        if "value" not in obj:
            raise InputError(
                "E_SCHEMA", f"{field}.value is required", {"field": field}
            )
        text = _normalize_decimal_text(obj["value"], f"{field}.value")
        value = quantize_decimal(text, state.fixed_scale, field=f"{field}.value")
        if value < key.minimum or value > key.maximum:
            raise InputError(
                "E_DOMAIN",
                f"threshold for {key_id} is outside its domain",
                {
                    "state_key": key_id,
                    "threshold": text,
                    "minimum": key.minimum,
                    "maximum": key.maximum,
                },
            )
        return Condition(key=key_id, op=op, value=value, threshold_text=text)
    if op not in BOOL_OPS:
        raise InputError(
            "E_OP",
            f"operator {op!r} is not valid for boolean key {key_id}",
            {"state_key": key_id, "op": op},
        )
    value = _require_bool(obj.get("value"), f"{field}.value")
    return Condition(
        key=key_id, op=op, value=value, threshold_text="true" if value else "false"
    )


def _parse_effect(item: Any, field: str, state: StateSchema) -> Effect:
    obj = _require_dict(item, field)
    key_id = _require_id(obj.get("key"), f"{field}.key")
    key = state.keys.get(key_id)
    if key is None:
        raise InputError(
            "E_STATE_KEY",
            f"effect references undeclared state key: {key_id}",
            {"state_key": key_id, "field": field},
        )
    op = _require_str(obj.get("op"), f"{field}.op")
    if isinstance(key, FixedKey):
        if op not in FIXED_EFFECT_OPS:
            raise InputError(
                "E_OP",
                f"operator {op!r} is not valid for fixed key {key_id}",
                {"state_key": key_id, "op": op},
            )
        if "value" not in obj:
            raise InputError(
                "E_SCHEMA", f"{field}.value is required", {"field": field}
            )
        text = _normalize_decimal_text(obj["value"], f"{field}.value")
        value = quantize_decimal(text, state.fixed_scale, field=f"{field}.value")
        if op == "set" and (value < key.minimum or value > key.maximum):
            raise InputError(
                "E_DOMAIN",
                f"set value for {key_id} is outside its domain",
                {"state_key": key_id, "value": text},
            )
        return Effect(key=key_id, op=op, value=value, value_text=text)
    if op not in BOOL_EFFECT_OPS:
        raise InputError(
            "E_OP",
            f"operator {op!r} is not valid for boolean key {key_id}",
            {"state_key": key_id, "op": op},
        )
    value = _require_bool(obj.get("value", True), f"{field}.value")
    return Effect(
        key=key_id, op=op, value=value, value_text="true" if value else "false"
    )


def _parse_conditions(
    value: Any, field: str, state: StateSchema
) -> tuple[Condition, ...]:
    if value is None:
        return ()
    return tuple(
        _parse_condition(item, f"{field}[{index}]", state)
        for index, item in enumerate(_require_list(value, field))
    )


def _parse_effects(value: Any, field: str, state: StateSchema) -> tuple[Effect, ...]:
    if value is None:
        return ()
    return tuple(
        _parse_effect(item, f"{field}[{index}]", state)
        for index, item in enumerate(_require_list(value, field))
    )


def _parse_story(raw: dict[str, Any], state: StateSchema) -> Story:
    root = _require_dict(raw, "story")
    declared_version = _require_str(
        root.get("version_id", "story-v1"), "story.version_id"
    )
    start_scene = _require_id(root.get("start_scene"), "story.start_scene")
    raw_scenes = _require_list(root.get("scenes"), "story.scenes")
    if not raw_scenes:
        raise InputError("E_SCHEMA", "story must contain at least one scene")

    scenes: dict[str, SceneDef] = {}
    event_ids: set[str] = set()
    branch_ids: set[str] = set()
    branch_refs: list[tuple[str, str, str | None]] = []
    for scene_index, item in enumerate(raw_scenes):
        field = f"story.scenes[{scene_index}]"
        obj = _require_dict(item, field)
        scene_id = _require_id(obj.get("id"), f"{field}.id")
        if scene_id in scenes:
            raise InputError(
                "E_SCENE_DUP",
                f"duplicate scene id: {scene_id}",
                {"scene_id": scene_id},
            )
        title = _require_str(obj.get("title", scene_id), f"{field}.title")
        start_time = _require_int(
            obj.get("start_time", 0), f"{field}.start_time", minimum=0
        )
        events: list[EventDef] = []
        raw_events = obj.get("events", [])
        for event_index, event_item in enumerate(
            _require_list(raw_events, f"{field}.events")
        ):
            event_field = f"{field}.events[{event_index}]"
            event_obj = _require_dict(event_item, event_field)
            event_id = _require_id(event_obj.get("id"), f"{event_field}.id")
            if event_id in event_ids:
                raise InputError(
                    "E_EVENT_DUP",
                    f"duplicate event id: {event_id}",
                    {"event_id": event_id},
                )
            event_ids.add(event_id)
            priority = _require_int(
                event_obj.get("priority", 0), f"{event_field}.priority"
            )
            preconditions = _parse_conditions(
                event_obj.get("preconditions", event_obj.get("when")),
                f"{event_field}.preconditions",
                state,
            )
            effects = _parse_effects(
                event_obj.get("effects", []), f"{event_field}.effects", state
            )
            events.append(
                EventDef(
                    id=event_id,
                    priority=priority,
                    preconditions=preconditions,
                    effects=effects,
                    declaration_index=event_index,
                )
            )
        branches: list[BranchDef] = []
        raw_branches = obj.get("branches", [])
        for branch_index, branch_item in enumerate(
            _require_list(raw_branches, f"{field}.branches")
        ):
            branch_field = f"{field}.branches[{branch_index}]"
            branch_obj = _require_dict(branch_item, branch_field)
            branch_id = _require_id(branch_obj.get("id"), f"{branch_field}.id")
            if branch_id in branch_ids:
                raise InputError(
                    "E_BRANCH_DUP",
                    f"duplicate branch id: {branch_id}",
                    {"branch_id": branch_id},
                )
            branch_ids.add(branch_id)
            preconditions = _parse_conditions(
                branch_obj.get("preconditions", branch_obj.get("when")),
                f"{branch_field}.preconditions",
                state,
            )
            effects = _parse_effects(
                branch_obj.get("effects", []), f"{branch_field}.effects", state
            )
            next_scene = branch_obj.get("next")
            if next_scene is not None:
                next_scene = _require_id(next_scene, f"{branch_field}.next")
            is_default = _require_bool(
                branch_obj.get("default", False), f"{branch_field}.default"
            )
            branch_refs.append((branch_id, scene_id, next_scene))
            branches.append(
                BranchDef(
                    id=branch_id,
                    preconditions=preconditions,
                    effects=effects,
                    next_scene=next_scene,
                    is_default=is_default,
                    declaration_index=branch_index,
                )
            )
        scenes[scene_id] = SceneDef(
            id=scene_id,
            title=title,
            start_time=start_time,
            events=tuple(events),
            branches=tuple(branches),
            declaration_index=scene_index,
        )
    if start_scene not in scenes:
        raise InputError(
            "E_BRANCH_REF",
            f"start_scene does not exist: {start_scene}",
            {"scene_id": start_scene},
        )
    for branch_id, scene_id, next_scene in branch_refs:
        if next_scene is not None and next_scene not in scenes:
            raise InputError(
                "E_BRANCH_REF",
                f"branch {branch_id} references missing scene {next_scene}",
                {
                    "branch_id": branch_id,
                    "scene_id": scene_id,
                    "next_scene": next_scene,
                },
            )
    return Story(
        version_id=declared_version, start_scene=start_scene, scenes=scenes
    )


def _parse_rect(value: Any, field: str, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise InputError(
            "E_RECT",
            f"{field} must be [x, y, width, height]",
            {"field": field},
        )
    x = _require_int(value[0], f"{field}[0]", minimum=0)
    y = _require_int(value[1], f"{field}[1]", minimum=0)
    w = _require_int(value[2], f"{field}[2]", minimum=1)
    h = _require_int(value[3], f"{field}[3]", minimum=1)
    if x + w > width or y + h > height:
        raise InputError(
            "E_RECT",
            f"{field} lies outside the storyboard canvas",
            {"field": field, "rect": [x, y, w, h], "canvas": [width, height]},
        )
    return (x, y, w, h)


def _gray_from_object(obj: dict[str, Any], field: str) -> int:
    if "gray" in obj:
        gray = _require_int(obj["gray"], f"{field}.gray", minimum=0)
        if gray > 255:
            raise InputError(
                "E_COLOR",
                f"{field}.gray must be in [0, 255]",
                {"field": f"{field}.gray", "value": gray},
            )
        return gray
    color = obj.get("color")
    if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
        raise InputError(
            "E_COLOR",
            f"{field} must define gray or a #RRGGBB color",
            {"field": field},
        )
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return (2126 * red + 7152 * green + 722 * blue + 5000) // 10000


def _parse_layer(
    item: Any, field: str, width: int, height: int
) -> Layer:
    obj = _require_dict(item, field)
    kind = _require_str(obj.get("type", "rect"), f"{field}.type").lower()
    if kind not in {"rect", "text", "highlight"}:
        raise InputError(
            "E_STORYBOARD",
            f"unsupported storyboard layer type: {kind}",
            {"field": field, "type": kind},
        )
    rect = _parse_rect(obj.get("rect"), f"{field}.rect", width, height)
    gray = _gray_from_object(obj, field)
    if kind == "text":
        text = _require_str(obj.get("text"), f"{field}.text", allow_empty=True)
        scale = _require_int(obj.get("scale", 1), f"{field}.scale", minimum=1)
        if scale > 8:
            raise InputError(
                "E_STORYBOARD",
                f"{field}.scale is too large",
                {"field": f"{field}.scale"},
            )
    else:
        text = ""
        scale = 1
    return Layer(kind=kind, rect=rect, gray=gray, text=text, scale=scale)


def _parse_storyboard(
    raw: dict[str, Any], story: Story, state: StateSchema
) -> Storyboard:
    root = _require_dict(raw, "storyboard")
    canvas = _require_dict(root.get("canvas"), "storyboard.canvas")
    width = _require_int(canvas.get("width"), "storyboard.canvas.width", minimum=1)
    height = _require_int(canvas.get("height"), "storyboard.canvas.height", minimum=1)
    if width > 2048 or height > 2048:
        raise InputError(
            "E_STORYBOARD",
            "storyboard canvas exceeds the supported 2048x2048 limit",
            {"width": width, "height": height},
        )
    background = _gray_from_object(canvas, "storyboard.canvas")
    raw_scenes = root.get("scenes")
    scene_items: Iterable[tuple[str | None, Any]]
    if isinstance(raw_scenes, dict):
        scene_items = raw_scenes.items()
    elif isinstance(raw_scenes, list):
        scene_items = ((None, item) for item in raw_scenes)
    else:
        raise InputError(
            "E_STORYBOARD",
            "storyboard.scenes must be an object or array",
            {"field": "storyboard.scenes"},
        )
    scenes: dict[str, SceneVisual] = {}
    for index, (mapping_id, item) in enumerate(scene_items):
        field = f"storyboard.scenes[{index}]"
        obj = _require_dict(item, field)
        scene_id = _require_id(
            obj.get("scene_id", obj.get("id", mapping_id)), f"{field}.scene_id"
        )
        if scene_id in scenes:
            raise InputError(
                "E_STORYBOARD",
                f"duplicate storyboard scene id: {scene_id}",
                {"scene_id": scene_id},
            )
        if scene_id not in story.scenes:
            raise InputError(
                "E_SCENE_REF",
                f"storyboard references missing scene: {scene_id}",
                {"scene_id": scene_id},
            )
        template_raw = obj.get("template", obj.get("layers", []))
        template = tuple(
            _parse_layer(layer, f"{field}.template[{layer_index}]", width, height)
            for layer_index, layer in enumerate(
                _require_list(template_raw, f"{field}.template")
            )
        )
        raw_markers = obj.get("markers", {})
        markers_obj = _require_dict(raw_markers, f"{field}.markers")
        markers: dict[str, tuple[int, int, int, int]] = {}
        for key_id, marker_value in markers_obj.items():
            key_id = _require_id(key_id, f"{field}.markers key")
            if key_id not in state.keys:
                raise InputError(
                    "E_STATE_KEY",
                    f"storyboard marker references undeclared state key: {key_id}",
                    {"scene_id": scene_id, "state_key": key_id},
                )
            marker_obj = _require_dict(
                marker_value, f"{field}.markers.{key_id}"
            )
            markers[key_id] = _parse_rect(
                marker_obj.get("rect"),
                f"{field}.markers.{key_id}.rect",
                width,
                height,
            )
        scenes[scene_id] = SceneVisual(
            scene_id=scene_id, template=template, markers=markers
        )
    missing = sorted(set(story.scenes) - set(scenes))
    if missing:
        raise InputError(
            "E_STORYBOARD",
            "storyboard is missing one or more story scenes",
            {"missing_scenes": missing},
        )
    return Storyboard(width=width, height=height, background=background, scenes=scenes)


def _parse_run(raw: dict[str, Any] | None, story: Story) -> RunConfig:
    if raw is None:
        return RunConfig(choices={})
    root = _require_dict(raw, "run")
    raw_choices = root.get("choices", {})
    choices_obj = _require_dict(raw_choices, "run.choices")
    choices: dict[str, str] = {}
    for scene_id, branch_id in choices_obj.items():
        scene_id = _require_id(scene_id, "run.choices key")
        branch_id = _require_id(branch_id, f"run.choices.{scene_id}")
        scene = story.scenes.get(scene_id)
        if scene is None:
            raise InputError(
                "E_CHOICE",
                f"choice references missing scene: {scene_id}",
                {"scene_id": scene_id},
            )
        if not any(branch.id == branch_id for branch in scene.branches):
            raise InputError(
                "E_CHOICE",
                f"branch {branch_id} does not belong to scene {scene_id}",
                {"scene_id": scene_id, "branch_id": branch_id},
            )
        choices[scene_id] = branch_id
    max_steps = _require_int(root.get("max_steps", 20000), "run.max_steps", minimum=1)
    if max_steps > 1_000_000:
        raise InputError(
            "E_SCHEMA", "run.max_steps exceeds supported maximum", {"max_steps": max_steps}
        )
    return RunConfig(choices=choices, max_steps=max_steps)


def load_bundle(
    *,
    state_path: str | Path,
    story_path: str | Path,
    storyboard_path: str | Path,
    run_path: str | Path | None = None,
) -> Bundle:
    state_raw = load_json(state_path)
    story_raw = load_json(story_path)
    storyboard_raw = load_json(storyboard_path)
    run_raw = load_json(run_path) if run_path is not None else None

    state = _parse_state(state_raw)
    story = _parse_story(story_raw, state)
    storyboard = _parse_storyboard(storyboard_raw, story, state)
    run = _parse_run(run_raw, story)

    source_payload = {
        "state": state_raw,
        "story": story_raw,
        "storyboard": storyboard_raw,
    }
    version_hash = digest_value(source_payload)[:20]
    version_id = f"v-{version_hash}"
    run_hash = digest_value(
        {"version_id": version_id, "choices": run.choices, "max_steps": run.max_steps}
    )[:20]
    run_id = f"run-{run_hash}"
    source_digests = {
        "state": digest_value(state_raw),
        "story": digest_value(story_raw),
        "storyboard": digest_value(storyboard_raw),
        "run": digest_value(run_raw) if run_raw is not None else digest_value(None),
    }
    return Bundle(
        state=state,
        story=story,
        storyboard=storyboard,
        run=run,
        version_id=version_id,
        run_id=run_id,
        source_digests=source_digests,
    )


def validate_paths(
    *,
    state_path: str | Path,
    story_path: str | Path,
    storyboard_path: str | Path,
    run_path: str | Path | None = None,
) -> dict[str, Any]:
    bundle = load_bundle(
        state_path=state_path,
        story_path=story_path,
        storyboard_path=storyboard_path,
        run_path=run_path,
    )
    return {
        "ok": True,
        "version_id": bundle.version_id,
        "run_id": bundle.run_id,
        "fixed_scale": bundle.state.fixed_scale,
        "residual_tolerance": bundle.state.residual_tolerance_text,
        "scene_count": len(bundle.story.scenes),
        "event_count": sum(len(scene.events) for scene in bundle.story.scenes.values()),
        "storyboard_size": [bundle.storyboard.width, bundle.storyboard.height],
        "choices": dict(sorted(bundle.run.choices.items())),
    }

