from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .encoding import short_digest
from .errors import SchemaError
from .fixedpoint import as_decimal, quantize_fixed, require_finite_number


FIXED_TYPES = {"relation", "number", "fixed"}
INTEGER_TYPES = {"integer", "int", "count"}
BOOLEAN_TYPES = {"boolean", "bool", "flag", "clue", "item"}
NUMERIC_TYPES = FIXED_TYPES | INTEGER_TYPES

OPERATOR_ALIASES = {
    "=": "==",
    "eq": "==",
    "equals": "==",
    "ne": "!=",
    "neq": "!=",
    "not_eq": "!=",
    "gt": ">",
    "gte": ">=",
    "ge": ">=",
    "lt": "<",
    "lte": "<=",
    "le": "<=",
}
COMPARISON_OPS = {"==", "!=", ">", ">=", "<", "<="}


def normalize_op(op: Any) -> str:
    if not isinstance(op, str) or not op.strip():
        raise SchemaError("condition operator must be a non-empty string")
    normalized = OPERATOR_ALIASES.get(op.strip().lower(), op.strip())
    if normalized not in COMPARISON_OPS:
        raise SchemaError(f"unsupported condition operator: {op!r}")
    return normalized


def _read_json_decimal(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal, parse_constant=_reject_constant)
    except FileNotFoundError as exc:
        raise SchemaError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise SchemaError(f"cannot read {path}: {exc}") from exc


def _reject_constant(value: str) -> None:
    raise SchemaError(f"non-finite JSON constant is not allowed: {value}")


@dataclass(frozen=True)
class StateSpec:
    key: str
    type: str
    base: str
    initial: Any
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    min_fixed: int | None = None
    max_fixed: int | None = None
    initial_fixed: int | None = None
    initial_int: int | None = None
    initial_bool: bool | None = None
    label: str | None = None

    @property
    def numeric(self) -> bool:
        return self.base in {"fixed", "integer"}


@dataclass(frozen=True)
class ConditionSpec:
    key: str
    op: str
    expected: Any
    expected_fixed: int | None = None
    expected_int: int | None = None
    expected_bool: bool | None = None
    expected_decimal: str | None = None


@dataclass(frozen=True)
class EffectSpec:
    key: str
    op: str
    value: Any = None
    fixed_value: int | None = None
    int_value: int | None = None
    bool_value: bool | None = None
    decimal_value: str | None = None


@dataclass(frozen=True)
class EventSpec:
    id: str
    node_id: str
    index: int
    priority: int
    once: bool
    conditions: tuple[ConditionSpec, ...]
    effects: tuple[EffectSpec, ...]
    description: str = ""


@dataclass(frozen=True)
class ChoiceSpec:
    id: str
    node_id: str
    index: int
    target: str
    conditions: tuple[ConditionSpec, ...]
    effects: tuple[EffectSpec, ...]
    text: str = ""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    index: int
    terminal: bool
    events: tuple[EventSpec, ...]
    choices: tuple[ChoiceSpec, ...]
    title: str = ""


@dataclass
class Script:
    scale: int
    residual_tolerance: Decimal
    states: dict[str, StateSpec]
    initial_fixed: dict[str, int]
    initial_integers: dict[str, int]
    initial_booleans: dict[str, bool]
    start_node: str
    nodes: tuple[NodeSpec, ...]
    node_map: dict[str, NodeSpec]
    event_map: dict[str, EventSpec]
    choice_map: dict[str, ChoiceSpec]
    canonical: dict[str, Any]
    version_id: str
    format: str = "interactive-narrative/1"
    script_version: str = "1.0"
    source_path: str | None = None


def load_script(script_path: str | Path, state_schema_path: str | Path | None = None) -> Script:
    path = Path(script_path)
    obj = _read_json_decimal(path)
    external_states: Any = None
    if state_schema_path is not None:
        state_path = Path(state_schema_path)
        external_states = _read_json_decimal(state_path)
    elif isinstance(obj, dict) and obj.get("state_schema_ref"):
        ref = Path(str(obj["state_schema_ref"]))
        if not ref.is_absolute():
            ref = path.parent / ref
        external_states = _read_json_decimal(ref)
    return parse_script_object(obj, state_schema_data=external_states, source_name=str(path))


def load_choice_file(path: str | Path) -> dict[str, Any]:
    choice_path = Path(path)
    obj = _read_json_decimal(choice_path)
    if not isinstance(obj, dict):
        raise SchemaError("choice file must be a JSON object")
    choices = obj.get("choices")
    if not isinstance(choices, list) or not all(isinstance(item, str) and item for item in choices):
        raise SchemaError("choice file must contain a non-empty string array at 'choices'")
    sequence_id = obj.get("id", "primary")
    if not isinstance(sequence_id, str) or not sequence_id:
        raise SchemaError("choice sequence id must be a non-empty string")
    return {
        "id": sequence_id,
        "choices": list(choices),
        "seed": obj.get("seed"),
        "raw": obj,
    }


def parse_script_object(
    obj: Any,
    *,
    state_schema_data: Any = None,
    source_name: str = "<object>",
) -> Script:
    try:
        return _parse_script_object_impl(
            obj,
            state_schema_data=state_schema_data,
            source_name=source_name,
        )
    except SchemaError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise SchemaError(
            "script structure is invalid",
            details={"source": source_name, "reason": exc.__class__.__name__, "message": str(exc)},
        ) from exc


def _parse_script_object_impl(
    obj: Any,
    *,
    state_schema_data: Any = None,
    source_name: str = "<object>",
) -> Script:
    issues: list[dict[str, str]] = []

    def issue(path: str, message: str) -> None:
        issues.append({"path": path, "message": message})

    if not isinstance(obj, dict):
        raise SchemaError("script root must be a JSON object", details={"source": source_name})

    scale_value = obj.get("fixed_point", {}).get("scale", 1_000_000) if isinstance(obj.get("fixed_point"), dict) else obj.get("fixed_point_scale", 1_000_000)
    try:
        if isinstance(scale_value, bool) or not isinstance(scale_value, int) or scale_value <= 0:
            issue("fixed_point.scale", "must be a positive integer")
            scale = 1_000_000
        else:
            scale = scale_value
    except Exception:
        scale = 1_000_000
        issue("fixed_point.scale", "must be a positive integer")

    tolerance_raw = Decimal("1e-16")
    fixed_point = obj.get("fixed_point") if isinstance(obj.get("fixed_point"), dict) else {}
    if "residual_tolerance" in fixed_point:
        try:
            tolerance_raw = require_finite_number(fixed_point["residual_tolerance"], field="fixed_point.residual_tolerance")
            if tolerance_raw < 0:
                issue("fixed_point.residual_tolerance", "must be non-negative")
        except SchemaError as exc:
            issue("fixed_point.residual_tolerance", exc.message)

    schema_data = state_schema_data if state_schema_data is not None else obj.get("state_schema")
    if isinstance(schema_data, dict) and "state_schema" in schema_data and isinstance(schema_data["state_schema"], dict):
        schema_data = schema_data["state_schema"]
    states = _parse_state_schema(schema_data, scale, issue)

    start_node = obj.get("start_node", obj.get("start"))
    if not isinstance(start_node, str) or not start_node:
        issue("start_node", "must be a non-empty string")
        start_node = ""

    raw_nodes = obj.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        issue("nodes", "must be a non-empty array")
        raw_nodes = []

    node_ids: set[str] = set()
    node_map: dict[str, NodeSpec] = {}
    event_map: dict[str, EventSpec] = {}
    choice_map: dict[str, ChoiceSpec] = {}
    nodes: list[NodeSpec] = []

    for node_index, raw_node in enumerate(raw_nodes):
        node_path = f"nodes[{node_index}]"
        if not isinstance(raw_node, dict):
            issue(node_path, "node must be an object")
            continue
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not node_id:
            issue(f"{node_path}.id", "node id must be a non-empty string")
            continue
        if node_id in node_ids:
            issue(f"{node_path}.id", f"duplicate node id: {node_id}")
            continue
        node_ids.add(node_id)

        events = _parse_events(raw_node.get("events", []), node_id, states, scale, issue)
        choices = _parse_choices(raw_node.get("choices", []), node_id, states, scale, issue)

        for event in events:
            if event.id in event_map:
                issue(f"{node_path}.events", f"duplicate event id: {event.id}")
            else:
                event_map[event.id] = event
        for choice in choices:
            if choice.id in choice_map:
                issue(f"{node_path}.choices", f"duplicate choice id: {choice.id}")
            else:
                choice_map[choice.id] = choice

        terminal = bool(raw_node.get("terminal", False))
        node = NodeSpec(
            id=node_id,
            index=node_index,
            terminal=terminal,
            events=tuple(events),
            choices=tuple(choices),
            title=str(raw_node.get("title", "")),
        )
        nodes.append(node)
        node_map[node_id] = node

    for node in nodes:
        for choice in node.choices:
            if choice.target not in node_ids:
                issue(f"nodes[{node.index}].choices.{choice.id}.target", f"target node does not exist: {choice.target}")

    if start_node and start_node not in node_ids:
        issue("start_node", f"start node does not exist: {start_node}")

    initial_fixed: dict[str, int] = {}
    initial_integers: dict[str, int] = {}
    initial_booleans: dict[str, bool] = {}
    raw_initial = obj.get("initial_state", {})
    if raw_initial is not None and not isinstance(raw_initial, dict):
        issue("initial_state", "must be an object")
    else:
        raw_initial = raw_initial or {}
        for key, spec in states.items():
            value = raw_initial.get(key, spec.initial)
            if spec.base == "fixed":
                try:
                    initial_fixed[key] = quantize_fixed(value if value is not None else 0, scale, field=f"initial_state.{key}")
                    _check_fixed_domain(initial_fixed[key], spec, f"initial_state.{key}", issue)
                except SchemaError as exc:
                    issue(f"initial_state.{key}", exc.message)
            elif spec.base == "integer":
                try:
                    int_value = _require_integer(value if value is not None else 0, f"initial_state.{key}")
                    _check_integer_domain(int_value, spec, f"initial_state.{key}", issue)
                    initial_integers[key] = int_value
                except SchemaError as exc:
                    issue(f"initial_state.{key}", exc.message)
            else:
                if not isinstance(value, bool):
                    issue(f"initial_state.{key}", "must be a boolean")
                else:
                    initial_booleans[key] = value
        for key in raw_initial:
            if key not in states:
                issue(f"initial_state.{key}", "initial state key is not declared in state_schema")

    if issues:
        raise SchemaError(
            f"script validation failed with {len(issues)} issue(s)",
            details={"issues": issues, "source": source_name},
        )

    canonical = _canonical_script(
        obj=obj,
        scale=scale,
        tolerance=tolerance_raw,
        states=states,
        initial_fixed=initial_fixed,
        initial_integers=initial_integers,
        initial_booleans=initial_booleans,
        start_node=start_node,
        nodes=nodes,
    )
    version_id = "ver-" + short_digest(canonical, 16)
    script_version = str(obj.get("version", obj.get("script_version", "1.0")))
    script_format = str(obj.get("format", "interactive-narrative/1"))

    return Script(
        scale=scale,
        residual_tolerance=tolerance_raw,
        states=states,
        initial_fixed=initial_fixed,
        initial_integers=initial_integers,
        initial_booleans=initial_booleans,
        start_node=start_node,
        nodes=tuple(nodes),
        node_map=node_map,
        event_map=event_map,
        choice_map=choice_map,
        canonical=canonical,
        version_id=version_id,
        format=script_format,
        script_version=script_version,
        source_path=source_name,
    )


def _parse_state_schema(
    data: Any,
    scale: int,
    issue: Any,
) -> dict[str, StateSpec]:
    if not isinstance(data, dict) or not data:
        issue("state_schema", "must be a non-empty object mapping state keys to declarations")
        return {}

    states: dict[str, StateSpec] = {}
    for key, raw_spec in data.items():
        path = f"state_schema.{key}"
        if not isinstance(key, str) or not key:
            issue("state_schema", "state keys must be non-empty strings")
            continue
        if not isinstance(raw_spec, dict):
            issue(path, "state declaration must be an object")
            continue
        state_type = raw_spec.get("type", raw_spec.get("kind"))
        if state_type in FIXED_TYPES:
            base = "fixed"
        elif state_type in INTEGER_TYPES:
            base = "integer"
        elif state_type in BOOLEAN_TYPES:
            base = "boolean"
        else:
            issue(f"{path}.type", f"unsupported state type: {state_type!r}")
            continue

        label = raw_spec.get("label")
        min_value = max_value = None
        min_fixed = max_fixed = None
        initial = raw_spec.get("initial", 0 if base != "boolean" else False)
        initial_fixed = None
        initial_int = None
        initial_bool = None

        if base in {"fixed", "integer"}:
            try:
                min_value = require_finite_number(raw_spec.get("min"), field=f"{path}.min")
                max_value = require_finite_number(raw_spec.get("max"), field=f"{path}.max")
                if min_value > max_value:
                    issue(f"{path}.min", "min must be <= max")
                if base == "fixed":
                    min_fixed = quantize_fixed(min_value, scale, field=f"{path}.min")
                    max_fixed = quantize_fixed(max_value, scale, field=f"{path}.max")
                    initial_fixed = quantize_fixed(initial, scale, field=f"{path}.initial")
                    if not (min_fixed <= initial_fixed <= max_fixed):
                        issue(f"{path}.initial", "initial value is outside declared domain")
                else:
                    min_int = _require_integer(min_value, f"{path}.min")
                    max_int = _require_integer(max_value, f"{path}.max")
                    min_value = Decimal(min_int)
                    max_value = Decimal(max_int)
                    initial_int = _require_integer(initial, f"{path}.initial")
                    if not (min_int <= initial_int <= max_int):
                        issue(f"{path}.initial", "initial value is outside declared domain")
            except SchemaError as exc:
                issue(path, exc.message)
        else:
            if "initial" in raw_spec and not isinstance(initial, bool):
                issue(f"{path}.initial", "boolean state initial must be true or false")
                initial = False
            initial_bool = bool(initial)

        states[key] = StateSpec(
            key=key,
            type=str(state_type),
            base=base,
            initial=initial,
            min_value=min_value,
            max_value=max_value,
            min_fixed=min_fixed,
            max_fixed=max_fixed,
            initial_fixed=initial_fixed,
            initial_int=initial_int,
            initial_bool=initial_bool,
            label=label if isinstance(label, str) else None,
        )
    return states


def _parse_conditions(raw_conditions: Any, owner_path: str, states: dict[str, StateSpec], scale: int, issue: Any) -> tuple[ConditionSpec, ...]:
    if raw_conditions is None:
        return ()
    if not isinstance(raw_conditions, list):
        issue(f"{owner_path}.conditions", "must be an array")
        return ()
    result: list[ConditionSpec] = []
    for index, raw in enumerate(raw_conditions):
        path = f"{owner_path}.conditions[{index}]"
        if not isinstance(raw, dict):
            issue(path, "condition must be an object")
            continue
        key = raw.get("key", raw.get("state"))
        if not isinstance(key, str) or key not in states:
            issue(f"{path}.key", f"condition references undeclared state key: {key!r}")
            continue
        try:
            op = normalize_op(raw.get("op", raw.get("operator", "==")))
        except SchemaError as exc:
            issue(f"{path}.op", exc.message)
            continue
        spec = states[key]
        expected = raw.get("value", raw.get("expected"))
        condition = _parse_condition_value(key, op, expected, spec, scale, path, issue)
        result.append(condition)
    return tuple(result)


def _parse_condition_value(key: str, op: str, expected: Any, spec: StateSpec, scale: int, path: str, issue: Any) -> ConditionSpec:
    if spec.base == "fixed":
        try:
            decimal_expected = require_finite_number(expected, field=f"{path}.value")
            expected_fixed = quantize_fixed(decimal_expected, scale, field=f"{path}.value")
            _check_fixed_domain(expected_fixed, spec, f"{path}.value", issue)
            return ConditionSpec(key=key, op=op, expected=expected, expected_fixed=expected_fixed, expected_decimal=format(decimal_expected, "f"))
        except SchemaError as exc:
            issue(f"{path}.value", exc.message)
            return ConditionSpec(key=key, op=op, expected=expected)
    if spec.base == "integer":
        try:
            expected_int = _require_integer(expected, f"{path}.value")
            _check_integer_domain(expected_int, spec, f"{path}.value", issue)
            return ConditionSpec(key=key, op=op, expected=expected, expected_int=expected_int, expected_decimal=str(expected_int))
        except SchemaError as exc:
            issue(f"{path}.value", exc.message)
            return ConditionSpec(key=key, op=op, expected=expected)

    if op not in {"==", "!="}:
        issue(f"{path}.op", "boolean/clue/flag/item conditions only support == or !=")
    if not isinstance(expected, bool):
        issue(f"{path}.value", "boolean condition value must be true or false")
        expected_bool = None
    else:
        expected_bool = expected
    return ConditionSpec(key=key, op=op, expected=expected, expected_bool=expected_bool)


def _parse_effects(raw_effects: Any, owner_path: str, states: dict[str, StateSpec], scale: int, issue: Any) -> tuple[EffectSpec, ...]:
    if raw_effects is None:
        return ()
    if not isinstance(raw_effects, list):
        issue(f"{owner_path}.effects", "must be an array")
        return ()
    result: list[EffectSpec] = []
    for index, raw in enumerate(raw_effects):
        path = f"{owner_path}.effects[{index}]"
        if not isinstance(raw, dict):
            issue(path, "effect must be an object")
            continue
        key = raw.get("key", raw.get("state"))
        if not isinstance(key, str) or key not in states:
            issue(f"{path}.key", f"effect references undeclared state key: {key!r}")
            continue
        op = str(raw.get("op", raw.get("operation", "set"))).strip().lower()
        op = {"=": "set", "+=": "add", "+": "add", "-=": "subtract", "-": "subtract", "sub": "subtract"}.get(op, op)
        spec = states[key]
        value = raw.get("value")
        effect = _parse_effect_value(key, op, value, spec, scale, path, issue)
        result.append(effect)
    return tuple(result)


def _parse_effect_value(key: str, op: str, value: Any, spec: StateSpec, scale: int, path: str, issue: Any) -> EffectSpec:
    if spec.base == "fixed":
        if op not in {"add", "subtract", "set"}:
            issue(f"{path}.op", f"unsupported numeric effect op: {op}")
        try:
            decimal_value = require_finite_number(value if value is not None else 0, field=f"{path}.value")
            fixed_value = quantize_fixed(decimal_value, scale, field=f"{path}.value")
            if op == "set":
                _check_fixed_domain(fixed_value, spec, f"{path}.value", issue)
            elif spec.min_fixed is not None and spec.max_fixed is not None:
                if not (spec.min_fixed <= fixed_value <= spec.max_fixed):
                    issue(f"{path}.value", "effect amount must be finite and inside declared domain")
            return EffectSpec(key=key, op=op, value=value, fixed_value=fixed_value, decimal_value=format(decimal_value, "f"))
        except SchemaError as exc:
            issue(f"{path}.value", exc.message)
            return EffectSpec(key=key, op=op, value=value)

    if spec.base == "integer":
        if op not in {"add", "subtract", "set"}:
            issue(f"{path}.op", f"unsupported integer effect op: {op}")
        try:
            int_value = _require_integer(value if value is not None else 0, f"{path}.value")
            if op == "set":
                _check_integer_domain(int_value, spec, f"{path}.value", issue)
            return EffectSpec(key=key, op=op, value=value, int_value=int_value)
        except SchemaError as exc:
            issue(f"{path}.value", exc.message)
            return EffectSpec(key=key, op=op, value=value)

    if op not in {"set", "toggle"}:
        issue(f"{path}.op", f"unsupported boolean effect op: {op}")
    bool_value = None
    if op == "set":
        if not isinstance(value, bool):
            issue(f"{path}.value", "boolean set effect requires true or false")
        else:
            bool_value = value
    return EffectSpec(key=key, op=op, value=value, bool_value=bool_value)


def _parse_events(raw_events: Any, node_id: str, states: dict[str, StateSpec], scale: int, issue: Any) -> list[EventSpec]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list):
        issue(f"node.{node_id}.events", "must be an array")
        return []
    events: list[EventSpec] = []
    for index, raw in enumerate(raw_events):
        path = f"nodes.{node_id}.events[{index}]"
        if not isinstance(raw, dict):
            issue(path, "event must be an object")
            continue
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id:
            issue(f"{path}.id", "event id must be a non-empty string")
            event_id = f"<invalid-event-{node_id}-{index}>"
        priority = raw.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            issue(f"{path}.priority", "priority must be an integer")
            priority = 0
        once = raw.get("once", True)
        if not isinstance(once, bool):
            issue(f"{path}.once", "once must be a boolean")
            once = True
        conditions = _parse_conditions(raw.get("preconditions", raw.get("conditions", [])), path, states, scale, issue)
        effects = _parse_effects(raw.get("effects", []), path, states, scale, issue)
        events.append(
            EventSpec(
                id=event_id,
                node_id=node_id,
                index=index,
                priority=priority,
                once=once,
                conditions=conditions,
                effects=effects,
                description=str(raw.get("description", "")),
            )
        )
    return events


def _parse_choices(raw_choices: Any, node_id: str, states: dict[str, StateSpec], scale: int, issue: Any) -> list[ChoiceSpec]:
    if raw_choices is None:
        return []
    if not isinstance(raw_choices, list):
        issue(f"node.{node_id}.choices", "must be an array")
        return []
    choices: list[ChoiceSpec] = []
    for index, raw in enumerate(raw_choices):
        path = f"nodes.{node_id}.choices[{index}]"
        if not isinstance(raw, dict):
            issue(path, "choice must be an object")
            continue
        choice_id = raw.get("id")
        if not isinstance(choice_id, str) or not choice_id:
            issue(f"{path}.id", "choice id must be a non-empty string")
            choice_id = f"<invalid-choice-{node_id}-{index}>"
        target = raw.get("target", raw.get("next"))
        if not isinstance(target, str) or not target:
            issue(f"{path}.target", "choice target must be a non-empty node id")
            target = ""
        conditions = _parse_conditions(raw.get("preconditions", raw.get("conditions", [])), path, states, scale, issue)
        effects = _parse_effects(raw.get("effects", []), path, states, scale, issue)
        choices.append(
            ChoiceSpec(
                id=choice_id,
                node_id=node_id,
                index=index,
                target=target,
                conditions=conditions,
                effects=effects,
                text=str(raw.get("text", "")),
            )
        )
    return choices


def _require_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise SchemaError(f"{field_name} must be an integer, not boolean")
    if isinstance(value, int):
        return value
    decimal_value = require_finite_number(value, field=field_name)
    if decimal_value != decimal_value.to_integral_value():
        raise SchemaError(f"{field_name} must be an integer")
    return int(decimal_value)


def _check_fixed_domain(value: int, spec: StateSpec, path: str, issue: Any) -> None:
    if spec.min_fixed is not None and spec.max_fixed is not None and not (spec.min_fixed <= value <= spec.max_fixed):
        issue(path, f"value is outside declared domain [{spec.min_value}, {spec.max_value}]")


def _check_integer_domain(value: int, spec: StateSpec, path: str, issue: Any) -> None:
    if spec.min_value is not None and spec.max_value is not None:
        min_int = int(spec.min_value)
        max_int = int(spec.max_value)
        if not (min_int <= value <= max_int):
            issue(path, f"value is outside declared domain [{min_int}, {max_int}]")


def _canonical_script(
    *,
    obj: dict[str, Any],
    scale: int,
    tolerance: Decimal,
    states: dict[str, StateSpec],
    initial_fixed: dict[str, int],
    initial_integers: dict[str, int],
    initial_booleans: dict[str, bool],
    start_node: str,
    nodes: list[NodeSpec],
) -> dict[str, Any]:
    state_schema = {}
    for key in sorted(states):
        spec = states[key]
        state_schema[key] = {
            "type": spec.type,
            "base": spec.base,
            "domain": [str(spec.min_value), str(spec.max_value)] if spec.numeric else None,
            "label": spec.label,
        }
    canonical_nodes = []
    for node in nodes:
        canonical_nodes.append(
            {
                "id": node.id,
                "terminal": node.terminal,
                "title": node.title,
                "events": [
                    {
                        "id": event.id,
                        "priority": event.priority,
                        "once": event.once,
                        "preconditions": [_canonical_condition(c) for c in event.conditions],
                        "effects": [_canonical_effect(e) for e in event.effects],
                        "description": event.description,
                    }
                    for event in node.events
                ],
                "choices": [
                    {
                        "id": choice.id,
                        "target": choice.target,
                        "text": choice.text,
                        "preconditions": [_canonical_condition(c) for c in choice.conditions],
                        "effects": [_canonical_effect(e) for e in choice.effects],
                    }
                    for choice in node.choices
                ],
            }
        )
    return {
        "format": str(obj.get("format", "interactive-narrative/1")),
        "script_version": str(obj.get("version", obj.get("script_version", "1.0"))),
        "fixed_point": {"scale": scale, "residual_tolerance": format(tolerance, "f")},
        "state_schema": state_schema,
        "initial_state": {
            "fixed": dict(sorted(initial_fixed.items())),
            "integers": dict(sorted(initial_integers.items())),
            "booleans": dict(sorted(initial_booleans.items())),
        },
        "start_node": start_node,
        "nodes": canonical_nodes,
    }


def _canonical_condition(condition: ConditionSpec) -> dict[str, Any]:
    return {
        "key": condition.key,
        "op": condition.op,
        "expected_fixed": condition.expected_fixed,
        "expected_int": condition.expected_int,
        "expected_bool": condition.expected_bool,
        "expected_decimal": condition.expected_decimal,
    }


def _canonical_effect(effect: EffectSpec) -> dict[str, Any]:
    return {
        "key": effect.key,
        "op": effect.op,
        "fixed_value": effect.fixed_value,
        "int_value": effect.int_value,
        "bool_value": effect.bool_value,
        "decimal_value": effect.decimal_value,
    }
