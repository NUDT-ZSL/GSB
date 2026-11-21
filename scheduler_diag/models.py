from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Iterable, Mapping

from .canonical import decimal_text, digest_json
from .errors import BusinessError, bad_input


FILTER_PLUGINS = (
    "ResourceFit",
    "TaintTolerance",
    "NodeAffinity",
    "PodAntiAffinity",
    "TopologySpread",
)
SCORE_PLUGINS = (
    "ResourceBinPack",
    "NodeAffinityMatch",
    "TopologySpreadBalance",
)
TAINT_EFFECTS = ("NoSchedule", "PreferNoSchedule", "NoExecute")
TOLERATION_OPERATORS = ("Equal", "Exists")
AFFINITY_OPERATORS = (
    "In",
    "NotIn",
    "Exists",
    "DoesNotExist",
    "Gt",
    "Lt",
)
PHASES = ("filter", "score")
INJECTION_TYPES = (
    "node_label_conflict",
    "resource_overcommit",
    "invalid_weight",
    "plugin_missing",
)
INJECTION_CODES = {
    "node_label_conflict": "E_INJECT_NODE_LABEL_CONFLICT",
    "resource_overcommit": "E_INJECT_RESOURCE_OVERCOMMIT",
    "invalid_weight": "E_INJECT_INVALID_WEIGHT",
    "plugin_missing": "E_INJECT_PLUGIN_MISSING",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")
_LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")


def _fail(code: str, message: str, **details: Any) -> BusinessError:
    return bad_input(code, message, **details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(value: Any, code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(code, f"{path} must be an object", path=path)
    return value


def _require_list(value: Any, code: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(code, f"{path} must be an array", path=path)
    return value


def _require_str(
    value: Any,
    code: str,
    path: str,
    *,
    allow_empty: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _fail(code, f"{path} must be a non-empty string", path=path)
    if pattern is not None and value and not pattern.fullmatch(value):
        raise _fail(code, f"{path} has invalid format", path=path, value=value)
    return value


def _require_int(
    value: Any,
    code: str,
    path: str,
    *,
    minimum: int | None = None,
) -> int:
    if not _is_int(value):
        raise _fail(code, f"{path} must be an integer", path=path, value=value)
    if minimum is not None and value < minimum:
        raise _fail(
            code,
            f"{path} must be >= {minimum}",
            path=path,
            value=value,
            minimum=minimum,
        )
    return value


def _as_decimal(value: Any, code: str, path: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _fail(code, f"{path} must be a finite decimal number", path=path)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise _fail(
                code, f"{path} must be a finite decimal number", path=path
            ) from None
    else:
        raise _fail(code, f"{path} must be a finite decimal number", path=path)
    if not result.is_finite():
        raise _fail(code, f"{path} must be a finite decimal number", path=path)
    return result


def _parse_labels(
    value: Any,
    code: str,
    path: str,
    *,
    require_nonempty: bool = False,
) -> dict[str, str]:
    raw = _require_dict(value, code, path)
    if require_nonempty and not raw:
        raise _fail(code, f"{path} must not be empty", path=path)
    labels: dict[str, str] = {}
    for key, label_value in raw.items():
        _require_str(key, code, f"{path}.key", pattern=_LABEL_KEY_RE)
        _require_str(
            label_value,
            code,
            f"{path}.{key}",
            pattern=_LABEL_VALUE_RE,
        )
        labels[key] = label_value
    return dict(sorted(labels.items()))


@dataclass(frozen=True)
class Taint:
    key: str
    value: str | None
    effect: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "effect": self.effect}


@dataclass(frozen=True)
class Toleration:
    key: str
    operator: str
    value: str | None
    effect: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operator": self.operator,
            "value": self.value,
            "effect": self.effect,
        }


@dataclass(frozen=True)
class AffinityExpression:
    key: str
    operator: str
    values: tuple[str, ...]
    weight: int = 1

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "operator": self.operator,
            "values": list(self.values),
        }
        if self.weight != 1:
            result["weight"] = self.weight
        return result


@dataclass(frozen=True)
class AffinityRule:
    required: tuple[AffinityExpression, ...] = ()
    preferred: tuple[AffinityExpression, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": [item.to_dict() for item in self.required],
            "preferred": [item.to_dict() for item in self.preferred],
        }


@dataclass(frozen=True)
class PodAntiAffinityRule:
    label_selector: Mapping[str, str]
    topology_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_selector": dict(self.label_selector),
            "topology_key": self.topology_key,
        }


@dataclass(frozen=True)
class TopologySpreadConstraint:
    max_skew: int
    topology_key: str
    when_unsatisfiable: str
    label_selector: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_skew": self.max_skew,
            "topology_key": self.topology_key,
            "when_unsatisfiable": self.when_unsatisfiable,
            "label_selector": dict(self.label_selector),
        }


@dataclass(frozen=True)
class NodeSpec:
    name: str
    cpu_millicores: int
    memory_mib: int
    labels: Mapping[str, str]
    taints: tuple[Taint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cpu_millicores": self.cpu_millicores,
            "memory_mib": self.memory_mib,
            "labels": dict(self.labels),
            "taints": [taint.to_dict() for taint in self.taints],
        }


@dataclass(frozen=True)
class PodSpec:
    name: str
    cpu_millicores: int
    memory_mib: int
    priority: int
    arrival_round: int
    labels: Mapping[str, str] = field(default_factory=dict)
    node_affinity: AffinityRule = field(default_factory=AffinityRule)
    node_anti_affinity: AffinityRule = field(default_factory=AffinityRule)
    pod_anti_affinity: PodAntiAffinityRule | None = None
    topology_spread: tuple[TopologySpreadConstraint, ...] = ()
    tolerations: tuple[Toleration, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cpu_millicores": self.cpu_millicores,
            "memory_mib": self.memory_mib,
            "priority": self.priority,
            "arrival_round": self.arrival_round,
            "labels": dict(self.labels),
            "node_affinity": self.node_affinity.to_dict(),
            "node_anti_affinity": self.node_anti_affinity.to_dict(),
            "pod_anti_affinity": (
                self.pod_anti_affinity.to_dict()
                if self.pod_anti_affinity is not None
                else None
            ),
            "topology_spread": [
                constraint.to_dict() for constraint in self.topology_spread
            ],
            "tolerations": [
                toleration.to_dict() for toleration in self.tolerations
            ],
        }


@dataclass(frozen=True)
class LogicalClockConfig:
    start_ms: int = 0
    plugin_tick_ms: int = 1

    def to_dict(self) -> dict[str, int]:
        return {
            "start_ms": self.start_ms,
            "plugin_tick_ms": self.plugin_tick_ms,
        }


@dataclass(frozen=True)
class SchedulingConfig:
    filter_plugins: tuple[str, ...]
    score_weights: Mapping[str, Decimal]
    round_time_budget_ms: int
    retry_budget: int
    logical_clock: LogicalClockConfig = field(default_factory=LogicalClockConfig)

    @property
    def weight_sum(self) -> Decimal:
        return sum(self.score_weights.values(), Decimal(0))

    @property
    def weight_sum_fraction(self) -> Fraction:
        return sum(
            (Fraction(weight) for weight in self.score_weights.values()),
            Fraction(0),
        )

    @property
    def weight_sum_is_integer(self) -> bool:
        return self.weight_sum == self.weight_sum.to_integral_value()

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_plugins": list(self.filter_plugins),
            "score_weights": {
                key: decimal_text(value)
                for key, value in sorted(self.score_weights.items())
            },
            "round_time_budget_ms": self.round_time_budget_ms,
            "retry_budget": self.retry_budget,
            "logical_clock": self.logical_clock.to_dict(),
        }


@dataclass(frozen=True)
class Injection:
    id: str
    round: int
    pod: str
    phase: str
    type: str
    node: str
    plugin: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def error_code(self) -> str:
        return INJECTION_CODES[self.type]

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        phase_order = 0 if self.phase == "filter" else 1
        return (self.round, phase_order, self.pod, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "round": self.round,
            "pod": self.pod,
            "phase": self.phase,
            "type": self.type,
            "node": self.node,
            "plugin": self.plugin,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Bundle:
    nodes: tuple[NodeSpec, ...]
    pods: tuple[PodSpec, ...]
    config: SchedulingConfig
    injections: tuple[Injection, ...]
    digest: str
    payload: Mapping[str, Any]

    @property
    def node_by_name(self) -> dict[str, NodeSpec]:
        return {node.name: node for node in self.nodes}

    @property
    def pod_by_name(self) -> dict[str, PodSpec]:
        return {pod.name: pod for pod in self.pods}

    @property
    def injection_by_id(self) -> dict[str, Injection]:
        return {injection.id: injection for injection in self.injections}

    @property
    def injections_by_pod_round(self) -> dict[tuple[str, int], Injection]:
        return {
            (injection.pod, injection.round): injection
            for injection in self.injections
        }

    @property
    def max_round(self) -> int:
        rounds = [pod.arrival_round for pod in self.pods]
        rounds.extend(injection.round for injection in self.injections)
        return max(rounds, default=0)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "nodes": len(self.nodes),
            "pods": len(self.pods),
            "injections": len(self.injections),
            "rounds": self.max_round,
        }


def _parse_affinity_expression(
    raw: Any, code: str, path: str
) -> AffinityExpression:
    item = _require_dict(raw, code, path)
    key = _require_str(item.get("key"), code, f"{path}.key", pattern=_LABEL_KEY_RE)
    operator = _require_str(item.get("operator"), code, f"{path}.operator")
    if operator not in AFFINITY_OPERATORS:
        raise _fail(
            code,
            f"{path}.operator must be one of {', '.join(AFFINITY_OPERATORS)}",
            path=f"{path}.operator",
            value=operator,
        )
    raw_values = item.get("values", [])
    values_raw = _require_list(raw_values, code, f"{path}.values")
    values: list[str] = []
    for index, value in enumerate(values_raw):
        values.append(
            _require_str(
                value,
                code,
                f"{path}.values[{index}]",
                pattern=_LABEL_VALUE_RE,
            )
        )
    if operator in ("In", "NotIn") and not values:
        raise _fail(
            code,
            f"{path}.values must not be empty for {operator}",
            path=f"{path}.values",
        )
    if operator in ("Exists", "DoesNotExist") and values:
        raise _fail(
            code,
            f"{path}.values must be empty for {operator}",
            path=f"{path}.values",
        )
    if operator in ("Gt", "Lt"):
        if len(values) != 1:
            raise _fail(
                code,
                f"{path}.values must contain exactly one integer for {operator}",
                path=f"{path}.values",
            )
        try:
            int(values[0])
        except ValueError:
            raise _fail(
                code,
                f"{path}.values[0] must be an integer for {operator}",
                path=f"{path}.values[0]",
                value=values[0],
            ) from None
    weight = item.get("weight", 1)
    weight = _require_int(weight, code, f"{path}.weight", minimum=1)
    if weight > 100:
        raise _fail(
            code,
            f"{path}.weight must be <= 100",
            path=f"{path}.weight",
            value=weight,
        )
    return AffinityExpression(
        key=key,
        operator=operator,
        values=tuple(values),
        weight=weight,
    )


def _parse_affinity_rule(raw: Any, code: str, path: str) -> AffinityRule:
    if raw is None:
        return AffinityRule()
    item = _require_dict(raw, code, path)
    required_raw = _require_list(item.get("required", []), code, f"{path}.required")
    preferred_raw = _require_list(
        item.get("preferred", []), code, f"{path}.preferred"
    )
    required = tuple(
        _parse_affinity_expression(value, code, f"{path}.required[{index}]")
        for index, value in enumerate(required_raw)
    )
    preferred = tuple(
        _parse_affinity_expression(value, code, f"{path}.preferred[{index}]")
        for index, value in enumerate(preferred_raw)
    )
    return AffinityRule(required=required, preferred=preferred)


def parse_node(raw: Any, index: int) -> NodeSpec:
    code = "E_BAD_NODE"
    path = f"nodes[{index}]"
    item = _require_dict(raw, code, path)
    name = _require_str(item.get("name"), code, f"{path}.name", pattern=_NAME_RE)
    cpu = _require_int(
        item.get("cpu_millicores"),
        code,
        f"{path}.cpu_millicores",
        minimum=1,
    )
    memory = _require_int(
        item.get("memory_mib"),
        code,
        f"{path}.memory_mib",
        minimum=1,
    )
    labels = _parse_labels(item.get("labels", {}), code, f"{path}.labels")
    taints_raw = _require_list(item.get("taints", []), code, f"{path}.taints")
    taints: list[Taint] = []
    seen_taints: set[tuple[str, str | None, str]] = set()
    for taint_index, raw_taint in enumerate(taints_raw):
        taint_path = f"{path}.taints[{taint_index}]"
        taint_obj = _require_dict(raw_taint, code, taint_path)
        key = _require_str(
            taint_obj.get("key"),
            code,
            f"{taint_path}.key",
            pattern=_LABEL_KEY_RE,
        )
        raw_value = taint_obj.get("value")
        value = None
        if raw_value is not None:
            value = _require_str(
                raw_value,
                code,
                f"{taint_path}.value",
                pattern=_LABEL_VALUE_RE,
            )
        effect = _require_str(taint_obj.get("effect"), code, f"{taint_path}.effect")
        if effect not in TAINT_EFFECTS:
            raise _fail(
                code,
                f"{taint_path}.effect must be one of {', '.join(TAINT_EFFECTS)}",
                path=f"{taint_path}.effect",
                value=effect,
            )
        marker = (key, value, effect)
        if marker in seen_taints:
            raise _fail(
                code,
                f"{taint_path} is duplicated",
                path=taint_path,
            )
        seen_taints.add(marker)
        taints.append(Taint(key=key, value=value, effect=effect))
    taints.sort(key=lambda taint: (taint.key, taint.effect, taint.value or ""))
    return NodeSpec(
        name=name,
        cpu_millicores=cpu,
        memory_mib=memory,
        labels=labels,
        taints=tuple(taints),
    )


def parse_toleration(raw: Any, path: str) -> Toleration:
    code = "E_BAD_POD"
    item = _require_dict(raw, code, path)
    key = _require_str(item.get("key"), code, f"{path}.key", pattern=_LABEL_KEY_RE)
    operator = item.get("operator", "Equal")
    operator = _require_str(operator, code, f"{path}.operator")
    if operator not in TOLERATION_OPERATORS:
        raise _fail(
            code,
            f"{path}.operator must be one of {', '.join(TOLERATION_OPERATORS)}",
            path=f"{path}.operator",
            value=operator,
        )
    raw_value = item.get("value")
    value: str | None = None
    if raw_value is not None:
        value = _require_str(
            raw_value, code, f"{path}.value", pattern=_LABEL_VALUE_RE
        )
    if operator == "Equal" and value is None:
        raise _fail(
            code,
            f"{path}.value is required when operator is Equal",
            path=f"{path}.value",
        )
    if operator == "Exists" and value is not None:
        raise _fail(
            code,
            f"{path}.value must be omitted when operator is Exists",
            path=f"{path}.value",
        )
    raw_effect = item.get("effect")
    effect: str | None = None
    if raw_effect is not None:
        effect = _require_str(raw_effect, code, f"{path}.effect")
        if effect not in TAINT_EFFECTS:
            raise _fail(
                code,
                f"{path}.effect must be one of {', '.join(TAINT_EFFECTS)}",
                path=f"{path}.effect",
                value=effect,
            )
    return Toleration(key=key, operator=operator, value=value, effect=effect)


def parse_pod(raw: Any, index: int) -> PodSpec:
    code = "E_BAD_POD"
    path = f"pods[{index}]"
    item = _require_dict(raw, code, path)
    name = _require_str(item.get("name"), code, f"{path}.name", pattern=_NAME_RE)
    cpu = _require_int(
        item.get("cpu_millicores"),
        code,
        f"{path}.cpu_millicores",
        minimum=1,
    )
    memory = _require_int(
        item.get("memory_mib"), code, f"{path}.memory_mib", minimum=1
    )
    priority = _require_int(item.get("priority", 0), code, f"{path}.priority")
    arrival_round = _require_int(
        item.get("arrival_round", 1),
        code,
        f"{path}.arrival_round",
        minimum=1,
    )
    labels = _parse_labels(item.get("labels", {}), code, f"{path}.labels")
    node_affinity = _parse_affinity_rule(
        item.get("node_affinity"), code, f"{path}.node_affinity"
    )
    node_anti_affinity = _parse_affinity_rule(
        item.get("node_anti_affinity"), code, f"{path}.node_anti_affinity"
    )
    pod_anti_affinity: PodAntiAffinityRule | None = None
    raw_pod_anti = item.get("pod_anti_affinity")
    if raw_pod_anti is not None:
        anti_path = f"{path}.pod_anti_affinity"
        anti_obj = _require_dict(raw_pod_anti, code, anti_path)
        selector = _parse_labels(
            anti_obj.get("label_selector"),
            code,
            f"{anti_path}.label_selector",
            require_nonempty=True,
        )
        topology_key = _require_str(
            anti_obj.get("topology_key"),
            code,
            f"{anti_path}.topology_key",
            pattern=_LABEL_KEY_RE,
        )
        pod_anti_affinity = PodAntiAffinityRule(selector, topology_key)
    spread_raw = _require_list(
        item.get("topology_spread", []), code, f"{path}.topology_spread"
    )
    spread: list[TopologySpreadConstraint] = []
    for spread_index, raw_constraint in enumerate(spread_raw):
        constraint_path = f"{path}.topology_spread[{spread_index}]"
        constraint_obj = _require_dict(raw_constraint, code, constraint_path)
        max_skew = _require_int(
            constraint_obj.get("max_skew"),
            code,
            f"{constraint_path}.max_skew",
            minimum=1,
        )
        topology_key = _require_str(
            constraint_obj.get("topology_key"),
            code,
            f"{constraint_path}.topology_key",
            pattern=_LABEL_KEY_RE,
        )
        when_unsatisfiable = _require_str(
            constraint_obj.get("when_unsatisfiable", "DoNotSchedule"),
            code,
            f"{constraint_path}.when_unsatisfiable",
        )
        if when_unsatisfiable not in ("DoNotSchedule", "ScheduleAnyway"):
            raise _fail(
                code,
                f"{constraint_path}.when_unsatisfiable is invalid",
                path=f"{constraint_path}.when_unsatisfiable",
                value=when_unsatisfiable,
            )
        selector = _parse_labels(
            constraint_obj.get("label_selector"),
            code,
            f"{constraint_path}.label_selector",
            require_nonempty=True,
        )
        spread.append(
            TopologySpreadConstraint(
                max_skew=max_skew,
                topology_key=topology_key,
                when_unsatisfiable=when_unsatisfiable,
                label_selector=selector,
            )
        )
    tolerations_raw = _require_list(
        item.get("tolerations", []), code, f"{path}.tolerations"
    )
    tolerations = tuple(
        parse_toleration(value, f"{path}.tolerations[{toleration_index}]")
        for toleration_index, value in enumerate(tolerations_raw)
    )
    return PodSpec(
        name=name,
        cpu_millicores=cpu,
        memory_mib=memory,
        priority=priority,
        arrival_round=arrival_round,
        labels=labels,
        node_affinity=node_affinity,
        node_anti_affinity=node_anti_affinity,
        pod_anti_affinity=pod_anti_affinity,
        topology_spread=tuple(spread),
        tolerations=tolerations,
    )


def parse_config(raw: Any) -> SchedulingConfig:
    code = "E_BAD_CONFIG"
    item = _require_dict(raw, code, "config")
    filter_raw = _require_list(item.get("filter_plugins"), code, "config.filter_plugins")
    filters: list[str] = []
    for index, plugin in enumerate(filter_raw):
        plugin_name = _require_str(
            plugin, code, f"config.filter_plugins[{index}]"
        )
        if plugin_name not in FILTER_PLUGINS:
            raise _fail(
                code,
                f"config.filter_plugins[{index}] is not a declared filter plugin",
                path=f"config.filter_plugins[{index}]",
                plugin=plugin_name,
            )
        if plugin_name in filters:
            raise _fail(
                code,
                f"config.filter_plugins[{index}] is duplicated",
                path=f"config.filter_plugins[{index}]",
                plugin=plugin_name,
            )
        filters.append(plugin_name)
    if set(filters) != set(FILTER_PLUGINS):
        missing = sorted(set(FILTER_PLUGINS) - set(filters))
        raise _fail(
            code,
            "config.filter_plugins must cover every declared filter plugin",
            missing=missing,
        )
    enabled_filters = set(filters)
    filters = [plugin for plugin in FILTER_PLUGINS if plugin in enabled_filters]
    weights_raw = _require_dict(
        item.get("score_weights"), code, "config.score_weights"
    )
    weights: dict[str, Decimal] = {}
    for key, raw_weight in weights_raw.items():
        if key not in SCORE_PLUGINS:
            raise _fail(
                code,
                f"config.score_weights.{key} is not a declared scoring plugin",
                path=f"config.score_weights.{key}",
                plugin=key,
            )
        weight = _as_decimal(raw_weight, code, f"config.score_weights.{key}")
        if weight < 0:
            raise _fail(
                code,
                f"config.score_weights.{key} must be non-negative",
                path=f"config.score_weights.{key}",
                value=decimal_text(weight),
            )
        weights[key] = weight
    missing = sorted(set(SCORE_PLUGINS) - set(weights))
    if missing:
        raise _fail(
            code,
            "config.score_weights must cover every declared scoring plugin",
            missing=missing,
        )
    weight_sum = sum(weights.values(), Decimal(0))
    if weight_sum <= 0:
        raise _fail(
            code,
            "config.score_weights must have a positive sum",
            weight_sum=decimal_text(weight_sum),
        )
    budget = _require_int(
        item.get("round_time_budget_ms"),
        code,
        "config.round_time_budget_ms",
        minimum=1,
    )
    retry_budget = _require_int(
        item.get("retry_budget", 0),
        code,
        "config.retry_budget",
        minimum=0,
    )
    clock_raw = item.get("logical_clock", {})
    clock_obj = _require_dict(clock_raw, code, "config.logical_clock")
    start_ms = _require_int(
        clock_obj.get("start_ms", 0),
        code,
        "config.logical_clock.start_ms",
        minimum=0,
    )
    tick_ms = _require_int(
        clock_obj.get("plugin_tick_ms", 1),
        code,
        "config.logical_clock.plugin_tick_ms",
        minimum=1,
    )
    return SchedulingConfig(
        filter_plugins=tuple(filters),
        score_weights=dict(sorted(weights.items())),
        round_time_budget_ms=budget,
        retry_budget=retry_budget,
        logical_clock=LogicalClockConfig(start_ms=start_ms, plugin_tick_ms=tick_ms),
    )


def parse_injection(
    raw: Any,
    index: int,
    *,
    pod_by_name: Mapping[str, PodSpec],
    node_by_name: Mapping[str, NodeSpec],
    config: SchedulingConfig,
) -> Injection:
    code = "E_BAD_INJECT"
    path = f"injections[{index}]"
    item = _require_dict(raw, code, path)
    injection_id = _require_str(
        item.get("id"), code, f"{path}.id", pattern=_NAME_RE
    )
    round_number = _require_int(
        item.get("round"), code, f"{path}.round", minimum=1
    )
    pod_name = _require_str(item.get("pod"), code, f"{path}.pod")
    pod = pod_by_name.get(pod_name)
    if pod is None:
        raise _fail(
            code,
            f"{path}.pod does not reference a known Pod",
            path=f"{path}.pod",
            pod=pod_name,
        )
    if pod.arrival_round != round_number:
        raise _fail(
            code,
            f"{path}.round does not match the Pod arrival round",
            path=f"{path}.round",
            round=round_number,
            pod=pod_name,
            pod_round=pod.arrival_round,
        )
    phase = _require_str(item.get("phase"), code, f"{path}.phase")
    if phase not in PHASES:
        raise _fail(
            code,
            f"{path}.phase must be filter or score",
            path=f"{path}.phase",
            phase=phase,
        )
    injection_type = _require_str(item.get("type"), code, f"{path}.type")
    if injection_type not in INJECTION_TYPES:
        raise _fail(
            code,
            f"{path}.type is not supported",
            path=f"{path}.type",
            type=injection_type,
        )
    expected_phase = {
        "node_label_conflict": "filter",
        "resource_overcommit": "filter",
        "invalid_weight": "score",
        "plugin_missing": None,
    }[injection_type]
    if expected_phase is not None and phase != expected_phase:
        raise _fail(
            code,
            f"{path}.phase must be {expected_phase} for {injection_type}",
            path=f"{path}.phase",
            type=injection_type,
            phase=phase,
        )
    node_name = _require_str(item.get("node"), code, f"{path}.node")
    if node_name not in node_by_name:
        raise _fail(
            code,
            f"{path}.node does not reference a known node",
            path=f"{path}.node",
            node=node_name,
        )
    plugin = _require_str(item.get("plugin"), code, f"{path}.plugin")
    allowed_plugins = (
        set(config.filter_plugins) if phase == "filter" else set(SCORE_PLUGINS)
    )
    if plugin not in allowed_plugins:
        raise _fail(
            code,
            f"{path}.plugin is not enabled in the {phase} phase",
            path=f"{path}.plugin",
            plugin=plugin,
            phase=phase,
        )
    message = _require_str(
        item.get("message", f"controlled {injection_type}"),
        code,
        f"{path}.message",
    )
    details_raw = item.get("details", {})
    details = _require_dict(details_raw, code, f"{path}.details")
    return Injection(
        id=injection_id,
        round=round_number,
        pod=pod_name,
        phase=phase,
        type=injection_type,
        node=node_name,
        plugin=plugin,
        message=message,
        details=details,
    )


def _ensure_unique_names(items: Iterable[Any], code: str, kind: str) -> None:
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        if item.name in seen:
            raise _fail(
                code,
                f"{kind} name must be unique",
                name=item.name,
                first_index=seen[item.name],
                duplicate_index=index,
            )
        seen[item.name] = index


def parse_bundle(
    nodes_raw: Any,
    pods_raw: Any,
    config_raw: Any,
    injections_raw: Any,
) -> Bundle:
    nodes_list = _require_list(nodes_raw, "E_BAD_NODE", "nodes")
    pods_list = _require_list(pods_raw, "E_BAD_POD", "pods")
    injections_list = _require_list(injections_raw, "E_BAD_INJECT", "injections")
    if not nodes_list:
        raise _fail("E_BAD_NODE", "nodes must not be empty")
    if not pods_list:
        raise _fail("E_BAD_POD", "pods must not be empty")
    nodes = tuple(parse_node(raw, index) for index, raw in enumerate(nodes_list))
    _ensure_unique_names(nodes, "E_BAD_NODE", "node")
    pods = tuple(parse_pod(raw, index) for index, raw in enumerate(pods_list))
    _ensure_unique_names(pods, "E_BAD_POD", "Pod")
    nodes = tuple(sorted(nodes, key=lambda node: node.name))
    pods = tuple(sorted(pods, key=lambda pod: pod.name))
    node_by_name = {node.name: node for node in nodes}
    pod_by_name = {pod.name: pod for pod in pods}
    config = parse_config(config_raw)

    topology_keys: set[str] = set()
    for pod in pods:
        if pod.pod_anti_affinity is not None:
            topology_keys.add(pod.pod_anti_affinity.topology_key)
        for constraint in pod.topology_spread:
            topology_keys.add(constraint.topology_key)
    for topology_key in sorted(topology_keys):
        if any(topology_key not in node.labels for node in nodes):
            raise _fail(
                "E_BAD_POD",
                "a topology key must be present on every node",
                topology_key=topology_key,
            )

    injections = tuple(
        parse_injection(
            raw,
            index,
            pod_by_name=pod_by_name,
            node_by_name=node_by_name,
            config=config,
        )
        for index, raw in enumerate(injections_list)
    )
    seen_ids: set[str] = set()
    seen_pod_round: set[tuple[str, int]] = set()
    for injection in injections:
        if injection.id in seen_ids:
            raise _fail(
                "E_BAD_INJECT",
                "injection id must be unique",
                injection_id=injection.id,
            )
        seen_ids.add(injection.id)
        marker = (injection.pod, injection.round)
        if marker in seen_pod_round:
            raise _fail(
                "E_BAD_INJECT",
                "at most one injection may target a Pod in a round",
                pod=injection.pod,
                round=injection.round,
            )
        seen_pod_round.add(marker)
    injections = tuple(sorted(injections, key=lambda item: item.sort_key))

    payload = {
        "schema": "scheduler-diag/v1",
        "nodes": [node.to_dict() for node in nodes],
        "pods": [pod.to_dict() for pod in pods],
        "config": config.to_dict(),
        "injections": [injection.to_dict() for injection in injections],
    }
    digest = digest_json(payload)
    return Bundle(
        nodes=nodes,
        pods=pods,
        config=config,
        injections=injections,
        digest=digest,
        payload=payload,
    )
