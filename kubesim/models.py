from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .errors import bad_config, bad_injection, bad_node, bad_pod
from .jsonutil import decimal_text, digest


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
QUALIFIED_NAME_RE = re.compile(
    r"^(?:[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*/)?"
    r"[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$"
)
LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$")

FILTER_ORDER = (
    "resource_feasibility",
    "taint_toleration",
    "node_affinity",
    "pod_anti_affinity",
    "topology_spread",
)
SCORE_ORDER = (
    "resource_bin_packing",
    "node_affinity",
    "topology_balance",
)

FILTER_ALIASES = {
    "resource_feasibility": "resource_feasibility",
    "resource": "resource_feasibility",
    "resources": "resource_feasibility",
    "node_resources_fit": "resource_feasibility",
    "taint_toleration": "taint_toleration",
    "taint": "taint_toleration",
    "taints": "taint_toleration",
    "node_affinity": "node_affinity",
    "node-affinity": "node_affinity",
    "affinity": "node_affinity",
    "pod_anti_affinity": "pod_anti_affinity",
    "pod-anti-affinity": "pod_anti_affinity",
    "anti_affinity": "pod_anti_affinity",
    "anti-affinity": "pod_anti_affinity",
    "topology_spread": "topology_spread",
    "topology": "topology_spread",
    "pod_topology_spread": "topology_spread",
}

SCORE_ALIASES = {
    "resource_bin_packing": "resource_bin_packing",
    "resource_packing": "resource_bin_packing",
    "bin_packing": "resource_bin_packing",
    "resources": "resource_bin_packing",
    "node_affinity": "node_affinity",
    "node-affinity": "node_affinity",
    "affinity": "node_affinity",
    "topology_balance": "topology_balance",
    "topology_spread_balance": "topology_balance",
    "topology": "topology_balance",
}

VALID_EFFECTS = {"NoSchedule", "PreferNoSchedule", "NoExecute"}
VALID_OPERATORS = {
    "In",
    "NotIn",
    "Exists",
    "DoesNotExist",
    "Gt",
    "Lt",
}
SHORT_TOPOLOGY_LABELS = {
    "zone",
    "rack",
    "region",
    "datacenter",
    "dc",
    "hostname",
}


def topology_domain_keys(labels: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key in labels
            if key.startswith("topology.")
            or key in SHORT_TOPOLOGY_LABELS
        )
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    *,
    code: str,
    message: str,
    minimum: int | None = None,
) -> int:
    if not _is_int(value):
        raise _error(code, message, value=value)
    if minimum is not None and value < minimum:
        raise _error(code, message, value=value, minimum=minimum)
    return value


def _error(code: str, message: str, **details: Any):
    if code == "E_BAD_NODE":
        return bad_node(message, **details)
    if code == "E_BAD_POD":
        return bad_pod(message, **details)
    if code == "E_BAD_CONFIG":
        return bad_config(message, **details)
    return bad_injection(message, **details)


def _mapping(value: Any, *, code: str, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(code, f"{where} must be an object", where=where)
    return value


def _list(value: Any, *, code: str, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _error(code, f"{where} must be an array", where=where)
    return value


def _name(value: Any, *, code: str, where: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise _error(code, f"{where} is not a valid name", where=where, value=value)
    return value


def _label_key(value: Any, *, code: str, where: str) -> str:
    if not isinstance(value, str) or not QUALIFIED_NAME_RE.fullmatch(value):
        raise _error(
            code,
            f"{where} is not a valid qualified label key",
            where=where,
            value=value,
        )
    return value


def _label_value(value: Any, *, code: str, where: str) -> str:
    if not isinstance(value, str):
        raise _error(code, f"{where} must be a string", where=where, value=value)
    if value and not LABEL_VALUE_RE.fullmatch(value):
        raise _error(code, f"{where} is not a valid label value", where=where, value=value)
    return value


def _labels(value: Any, *, code: str, where: str) -> dict[str, str]:
    raw = _mapping(value or {}, code=code, where=where)
    result: dict[str, str] = {}
    for key, label_value in raw.items():
        valid_key = _label_key(key, code=code, where=where)
        result[valid_key] = _label_value(
            label_value,
            code=code,
            where=f"{where}.{valid_key}",
        )
    return dict(sorted(result.items()))


def _parse_decimal(
    value: Any,
    *,
    code: str,
    where: str,
    minimum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _error(code, f"{where} must be a finite number", where=where, value=value)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _error(
            code,
            f"{where} must be a finite number",
            where=where,
            value=value,
        ) from exc
    if not result.is_finite():
        raise _error(code, f"{where} must be finite", where=where, value=value)
    if minimum is not None and result < minimum:
        raise _error(
            code,
            f"{where} must be at least {decimal_text(minimum)}",
            where=where,
            value=value,
        )
    return result


def _parse_cpu_millicores(value: Any, *, code: str, where: str) -> int:
    if _is_int(value):
        return _require_int(value, code=code, message=f"{where} must be an integer", minimum=0)
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+)(m)?", value.strip())
        if match:
            number = int(match.group(1))
            return number if match.group(2) else number * 1000
    raise _error(code, f"{where} must be integer millicores or a CPU quantity", value=value)


def _parse_memory_mib(value: Any, *, code: str, where: str) -> int:
    if _is_int(value):
        return _require_int(value, code=code, message=f"{where} must be an integer", minimum=0)
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+)(Mi|Gi|M|G)?", value.strip())
        if match:
            number = int(match.group(1))
            suffix = match.group(2)
            if suffix is None or suffix == "Mi":
                return number
            if suffix == "Gi":
                return number * 1024
            if suffix == "M":
                return (number + 1_048_575) // 1_048_576
            if suffix == "G":
                return (number * 1_000_000_000 + 1_048_575) // 1_048_576
    raise _error(code, f"{where} must be integer MiB or a memory quantity", value=value)


def _parse_gpu(value: Any, *, code: str, where: str) -> int:
    if _is_int(value):
        return _require_int(value, code=code, message=f"{where} must be an integer", minimum=0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise _error(code, f"{where} must be a non-negative integer", value=value)


@dataclass(frozen=True)
class Resources:
    cpu_millicores: int
    memory_mib: int
    gpu: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_millicores": self.cpu_millicores,
            "memory_mib": self.memory_mib,
            "gpu": self.gpu,
        }


@dataclass(frozen=True)
class Taint:
    key: str
    value: str
    effect: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "value": self.value, "effect": self.effect}


@dataclass(frozen=True)
class Toleration:
    key: str
    operator: str
    value: str
    effect: str | None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"key": self.key, "operator": self.operator}
        if self.value:
            result["value"] = self.value
        if self.effect:
            result["effect"] = self.effect
        return result


@dataclass(frozen=True)
class MatchExpression:
    key: str
    operator: str
    values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operator": self.operator,
            "values": list(self.values),
        }


@dataclass(frozen=True)
class PreferredAffinity:
    weight: int
    expressions: tuple[MatchExpression, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "match_expressions": [item.to_dict() for item in self.expressions],
        }


@dataclass(frozen=True)
class NodeAffinity:
    required_terms: tuple[tuple[MatchExpression, ...], ...] = ()
    required_selector: tuple[tuple[str, str], ...] = ()
    preferred: tuple[PreferredAffinity, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_terms": [
                [expression.to_dict() for expression in term]
                for term in self.required_terms
            ],
            "required_selector": dict(self.required_selector),
            "preferred": [item.to_dict() for item in self.preferred],
        }


@dataclass(frozen=True)
class AntiAffinityRule:
    topology_key: str
    match_labels: tuple[tuple[str, str], ...]
    match_expressions: tuple[MatchExpression, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology_key": self.topology_key,
            "match_labels": dict(self.match_labels),
            "match_expressions": [
                expression.to_dict() for expression in self.match_expressions
            ],
        }


@dataclass(frozen=True)
class TopologySpreadConstraint:
    topology_key: str
    max_skew: int
    min_domains: int
    when_unsatisfiable: str
    match_labels: tuple[tuple[str, str], ...]
    match_expressions: tuple[MatchExpression, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology_key": self.topology_key,
            "max_skew": self.max_skew,
            "min_domains": self.min_domains,
            "when_unsatisfiable": self.when_unsatisfiable,
            "match_labels": dict(self.match_labels),
            "match_expressions": [
                expression.to_dict() for expression in self.match_expressions
            ],
        }


@dataclass(frozen=True)
class Node:
    name: str
    resources: Resources
    labels: dict[str, str] = field(default_factory=dict)
    taints: tuple[Taint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "resources": self.resources.to_dict(),
            "labels": dict(self.labels),
            "taints": [taint.to_dict() for taint in self.taints],
        }


@dataclass(frozen=True)
class Pod:
    name: str
    resources: Resources
    priority: int
    workload: str
    labels: dict[str, str]
    node_affinity: NodeAffinity
    anti_affinity: tuple[AntiAffinityRule, ...]
    topology_spread: tuple[TopologySpreadConstraint, ...]
    tolerations: tuple[Toleration, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "resources": self.resources.to_dict(),
            "priority": self.priority,
            "workload": self.workload,
            "labels": dict(self.labels),
            "node_affinity": self.node_affinity.to_dict(),
            "anti_affinity": [rule.to_dict() for rule in self.anti_affinity],
            "topology_spread": [rule.to_dict() for rule in self.topology_spread],
            "tolerations": [toleration.to_dict() for toleration in self.tolerations],
        }

    @property
    def version(self) -> str:
        return digest(self.to_dict())

    @property
    def topology_keys(self) -> tuple[str, ...]:
        keys = {rule.topology_key for rule in self.anti_affinity}
        keys.update(rule.topology_key for rule in self.topology_spread)
        return tuple(sorted(keys))


@dataclass(frozen=True)
class ScheduleConfig:
    filters: tuple[str, ...]
    score_weights: dict[str, Decimal]
    budget_ms: int
    max_concurrent_per_workload: int
    max_parallel_pods: int
    retry_budget: int
    clock_step_ms: int
    retry_delay_ms: int
    filter_costs: dict[str, int]
    score_costs: dict[str, int]
    bind_cost_ms: int
    precision_tolerance: Decimal
    weight_decimals: int
    score_decimals: int
    weight_rounding: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "filters": list(self.filters),
            "score_weights": {
                key: decimal_text(value)
                for key, value in sorted(self.score_weights.items())
            },
            "budget_ms": self.budget_ms,
            "max_concurrent_per_workload": self.max_concurrent_per_workload,
            "max_parallel_pods": self.max_parallel_pods,
            "retry_budget": self.retry_budget,
            "retry_delay_ms": self.retry_delay_ms,
            "clock_step_ms": self.clock_step_ms,
            "filter_costs": dict(sorted(self.filter_costs.items())),
            "score_costs": dict(sorted(self.score_costs.items())),
            "bind_cost_ms": self.bind_cost_ms,
            "precision_tolerance": decimal_text(self.precision_tolerance),
            "weight_decimals": self.weight_decimals,
            "score_decimals": self.score_decimals,
            "weight_rounding": self.weight_rounding,
        }


@dataclass(frozen=True)
class Injection:
    kind: str
    pod: str
    phase: str
    plugin: str | None = None
    ms: int = 0
    exit_code: int = 137
    reason: str = "injected active abandon"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.kind,
            "pod": self.pod,
            "phase": self.phase,
        }
        if self.plugin is not None:
            result["plugin"] = self.plugin
        if self.kind == "latency":
            result["ms"] = self.ms
        elif self.kind == "terminate":
            result["exit_code"] = self.exit_code
        elif self.kind == "abandon":
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class Dataset:
    nodes: tuple[Node, ...]
    pods: tuple[Pod, ...]
    config: ScheduleConfig
    injections: tuple[Injection, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "pods": [pod.to_dict() for pod in self.pods],
            "config": self.config.to_dict(),
            "injections": [item.to_dict() for item in self.injections],
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())


def _unwrap(document: Any, key: str, *, code: str) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and key in document:
        value = document[key]
        if isinstance(value, list):
            return value
    raise _error(code, f"top-level JSON must be an array or contain {key!r}", key=key)


def _resource_values(
    raw: dict[str, Any],
    *,
    code: str,
    where: str,
    node_capacity: bool,
) -> Resources:
    if node_capacity:
        container = raw.get("allocatable", raw.get("resources"))
    else:
        container = raw.get("resources")
        if isinstance(container, dict) and "requests" in container:
            container = container["requests"]
        if container is None:
            container = raw.get("requests")
    if container is None:
        container = raw
    if not isinstance(container, dict):
        raise _error(code, f"{where} must be an object", where=where)

    cpu_value = _first(
        container,
        "cpu_millicores",
        "cpuMillicores",
        "cpu",
        "milli_cpu",
    )
    memory_value = _first(
        container,
        "memory_mib",
        "memoryMiB",
        "memoryMiB",
        "memory",
        "mem_mib",
    )
    gpu_value = _first(
        container,
        "gpu",
        "gpu_cards",
        "gpuCards",
        "nvidia.com/gpu",
        default=0,
    )
    if cpu_value is None or memory_value is None:
        raise _error(
            code,
            f"{where} must define CPU and memory",
            where=where,
        )
    cpu = _parse_cpu_millicores(cpu_value, code=code, where=f"{where}.cpu")
    memory = _parse_memory_mib(memory_value, code=code, where=f"{where}.memory")
    gpu = _parse_gpu(gpu_value, code=code, where=f"{where}.gpu")
    if node_capacity:
        if cpu <= 0:
            raise _error(code, f"{where}.cpu must be positive", where=where)
        if memory <= 0:
            raise _error(code, f"{where}.memory must be positive", where=where)
    return Resources(cpu, memory, gpu)


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def parse_node(raw: Any, index: int) -> Node:
    code = "E_BAD_NODE"
    item = _mapping(raw, code=code, where=f"nodes[{index}]")
    name = _name(item.get("name"), code=code, where=f"nodes[{index}].name")
    resources = _resource_values(item, code=code, where=f"nodes[{index}]", node_capacity=True)
    labels = _labels(item.get("labels"), code=code, where=f"nodes[{index}].labels")
    topology = item.get("topology_domains")
    if topology is not None:
        labels.update(_labels(topology, code=code, where=f"nodes[{index}].topology_domains"))
        labels = dict(sorted(labels.items()))

    taints: list[Taint] = []
    for taint_index, taint_raw in enumerate(
        _list(item.get("taints"), code=code, where=f"nodes[{index}].taints")
    ):
        taint = _mapping(
            taint_raw,
            code=code,
            where=f"nodes[{index}].taints[{taint_index}]",
        )
        key = _label_key(
            taint.get("key"),
            code=code,
            where=f"nodes[{index}].taints[{taint_index}].key",
        )
        value = _label_value(
            taint.get("value", ""),
            code=code,
            where=f"nodes[{index}].taints[{taint_index}].value",
        )
        effect = taint.get("effect")
        if effect not in VALID_EFFECTS:
            raise _error(
                code,
                "taint effect must be NoSchedule, PreferNoSchedule, or NoExecute",
                where=f"nodes[{index}].taints[{taint_index}].effect",
                value=effect,
            )
        taints.append(Taint(key, value, effect))
    return Node(
        name=name,
        resources=resources,
        labels=labels,
        taints=tuple(sorted(taints, key=lambda item: (item.key, item.value, item.effect))),
    )


def _parse_expression(raw: Any, *, code: str, where: str) -> MatchExpression:
    item = _mapping(raw, code=code, where=where)
    key = _label_key(item.get("key"), code=code, where=f"{where}.key")
    operator = item.get("operator", "In")
    if operator not in VALID_OPERATORS:
        raise _error(
            code,
            f"{where}.operator is not supported",
            where=f"{where}.operator",
            value=operator,
        )
    values_raw = item.get("values", [])
    if not isinstance(values_raw, list):
        raise _error(code, f"{where}.values must be an array", where=f"{where}.values")
    values = tuple(
        sorted(
            {
                _label_value(value, code=code, where=f"{where}.values")
                for value in values_raw
            }
        )
    )
    if operator in {"In", "NotIn", "Gt", "Lt"} and not values:
        raise _error(
            code,
            f"{where}.values must not be empty for {operator}",
            where=f"{where}.values",
        )
    if operator in {"Exists", "DoesNotExist"} and values:
        raise _error(
            code,
            f"{where}.values must be empty for {operator}",
            where=f"{where}.values",
        )
    if operator in {"Gt", "Lt"}:
        if len(values) != 1 or not values[0].lstrip("-").isdigit():
            raise _error(
                code,
                f"{where}.values must contain one integer for {operator}",
                where=f"{where}.values",
            )
    return MatchExpression(key, operator, values)


def _expressions(raw: Any, *, code: str, where: str) -> tuple[MatchExpression, ...]:
    return tuple(
        _parse_expression(item, code=code, where=f"{where}[{index}]")
        for index, item in enumerate(_list(raw, code=code, where=where))
    )


def _selector_parts(
    raw: Any,
    *,
    code: str,
    where: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[MatchExpression, ...]]:
    item = _mapping(raw, code=code, where=where)
    label_raw = _first(item, "match_labels", "matchLabels", default={})
    labels = tuple(sorted(_labels(label_raw, code=code, where=f"{where}.match_labels").items()))
    expressions = _expressions(
        _first(item, "match_expressions", "matchExpressions", default=[]),
        code=code,
        where=f"{where}.match_expressions",
    )
    if not labels and not expressions:
        raise _error(code, f"{where} must define labels or expressions", where=where)
    return labels, expressions


def _parse_node_affinity(raw: Any, *, code: str, where: str) -> NodeAffinity:
    if raw is None:
        return NodeAffinity()
    item = _mapping(raw, code=code, where=where)

    required_selector: list[tuple[str, str]] = []
    simple_selector = _first(
        item,
        "node_selector",
        "nodeSelector",
        "required_selector",
    )
    if simple_selector is not None:
        required_selector = sorted(
            _labels(simple_selector, code=code, where=f"{where}.node_selector").items()
        )

    required_terms: list[tuple[MatchExpression, ...]] = []
    canonical_terms = item.get("required_terms")
    if canonical_terms is not None:
        for term_index, term_raw in enumerate(
            _list(
                canonical_terms,
                code=code,
                where=f"{where}.required_terms",
            )
        ):
            required_terms.append(
                _expressions(
                    term_raw,
                    code=code,
                    where=f"{where}.required_terms[{term_index}]",
                )
            )
    required_raw = _first(
        item,
        "required",
        "requiredDuringSchedulingIgnoredDuringExecution",
    )
    if required_raw is not None:
        if isinstance(required_raw, list):
            if required_raw and all(
                isinstance(entry, dict)
                and ("key" in entry)
                and not ("nodeSelectorTerms" in entry)
                for entry in required_raw
            ):
                required_terms.append(
                    _expressions(
                        required_raw,
                        code=code,
                        where=f"{where}.required",
                    )
                )
            else:
                for term_index, term_raw in enumerate(required_raw):
                    term = _mapping(
                        term_raw,
                        code=code,
                        where=f"{where}.required[{term_index}]",
                    )
                    required_terms.append(
                        _expressions(
                            _first(term, "match_expressions", "matchExpressions", default=[]),
                            code=code,
                            where=f"{where}.required[{term_index}].match_expressions",
                        )
                    )
        elif isinstance(required_raw, dict):
            terms_raw = _first(
                required_raw,
                "nodeSelectorTerms",
                "node_selector_terms",
                "terms",
                default=[],
            )
            if not isinstance(terms_raw, list):
                raise _error(
                    code,
                    f"{where}.required.nodeSelectorTerms must be an array",
                    where=where,
                )
            for term_index, term_raw in enumerate(terms_raw):
                term = _mapping(
                    term_raw,
                    code=code,
                    where=f"{where}.required[{term_index}]",
                )
                required_terms.append(
                    _expressions(
                        _first(term, "match_expressions", "matchExpressions", default=[]),
                        code=code,
                        where=f"{where}.required[{term_index}].match_expressions",
                    )
                )
        else:
            raise _error(code, f"{where}.required must be an array or object", where=where)

    preferred: list[PreferredAffinity] = []
    preferred_raw = _first(
        item,
        "preferred",
        "preferredDuringSchedulingIgnoredDuringExecution",
    )
    for preferred_index, preferred_raw_item in enumerate(
        _list(preferred_raw, code=code, where=f"{where}.preferred")
    ):
        preferred_item = _mapping(
            preferred_raw_item,
            code=code,
            where=f"{where}.preferred[{preferred_index}]",
        )
        weight = _require_int(
            preferred_item.get("weight", 1),
            code=code,
            message="preferred affinity weight must be a positive integer",
            minimum=1,
        )
        if (
            "key" in preferred_item
            and "preference" not in preferred_item
            and "match_expressions" not in preferred_item
            and "matchExpressions" not in preferred_item
        ):
            expressions = (
                _parse_expression(
                    preferred_item,
                    code=code,
                    where=f"{where}.preferred[{preferred_index}]",
                ),
            )
        else:
            preference_raw = preferred_item.get("preference", preferred_item)
            preference = _mapping(
                preference_raw,
                code=code,
                where=f"{where}.preferred[{preferred_index}].preference",
            )
            expressions = _expressions(
                _first(preference, "match_expressions", "matchExpressions", default=[]),
                code=code,
                where=f"{where}.preferred[{preferred_index}].match_expressions",
            )
        if not expressions:
            raise _error(
                code,
                "preferred node affinity requires match expressions",
                where=f"{where}.preferred[{preferred_index}]",
            )
        preferred.append(PreferredAffinity(weight, expressions))

    return NodeAffinity(
        required_terms=tuple(required_terms),
        required_selector=tuple(required_selector),
        preferred=tuple(preferred),
    )


def _parse_anti_affinity(raw: Any, *, code: str, where: str) -> tuple[AntiAffinityRule, ...]:
    if raw is None:
        return ()
    values = raw
    if isinstance(raw, dict):
        values = _first(
            raw,
            "required",
            "requiredDuringSchedulingIgnoredDuringExecution",
            default=raw,
        )
    rules: list[AntiAffinityRule] = []
    for index, rule_raw in enumerate(_list(values, code=code, where=where)):
        rule = _mapping(rule_raw, code=code, where=f"{where}[{index}]")
        topology_key = _label_key(
            rule.get("topology_key", rule.get("topologyKey")),
            code=code,
            where=f"{where}[{index}].topology_key",
        )
        selector_raw = _first(rule, "selector", "label_selector", "labelSelector")
        if selector_raw is not None:
            labels, expressions = _selector_parts(
                selector_raw,
                code=code,
                where=f"{where}[{index}].selector",
            )
        else:
            labels_raw = _first(rule, "match_labels", "matchLabels", default={})
            labels = tuple(
                sorted(_labels(labels_raw, code=code, where=f"{where}[{index}].match_labels").items())
            )
            expressions = _expressions(
                _first(rule, "match_expressions", "matchExpressions", default=[]),
                code=code,
                where=f"{where}[{index}].match_expressions",
            )
        if not labels and not expressions:
            raise _error(code, f"{where}[{index}] requires a selector", where=where)
        rules.append(AntiAffinityRule(topology_key, labels, expressions))
    return tuple(rules)


def _parse_topology_spread(
    raw: Any,
    *,
    code: str,
    where: str,
) -> tuple[TopologySpreadConstraint, ...]:
    constraints: list[TopologySpreadConstraint] = []
    for index, constraint_raw in enumerate(
        _list(raw, code=code, where=where)
    ):
        item = _mapping(constraint_raw, code=code, where=f"{where}[{index}]")
        topology_key = _label_key(
            item.get("topology_key", item.get("topologyKey")),
            code=code,
            where=f"{where}[{index}].topology_key",
        )
        max_skew = _require_int(
            item.get("max_skew", item.get("maxSkew")),
            code=code,
            message="topology max_skew must be a positive integer",
            minimum=1,
        )
        min_domains = _require_int(
            item.get("min_domains", item.get("minDomains", 1)),
            code=code,
            message="topology min_domains must be a positive integer",
            minimum=1,
        )
        when = item.get("when_unsatisfiable", item.get("whenUnsatisfiable", "DoNotSchedule"))
        if when not in {"DoNotSchedule", "ScheduleAnyway"}:
            raise _error(
                code,
                "when_unsatisfiable must be DoNotSchedule or ScheduleAnyway",
                where=f"{where}[{index}].when_unsatisfiable",
            )
        selector_raw = _first(item, "selector", "label_selector", "labelSelector")
        if selector_raw is not None:
            labels, expressions = _selector_parts(
                selector_raw,
                code=code,
                where=f"{where}[{index}].selector",
            )
        else:
            labels_raw = _first(item, "match_labels", "matchLabels", default={})
            labels = tuple(
                sorted(_labels(labels_raw, code=code, where=f"{where}[{index}].match_labels").items())
            )
            expressions = _expressions(
                _first(item, "match_expressions", "matchExpressions", default=[]),
                code=code,
                where=f"{where}[{index}].match_expressions",
            )
        constraints.append(
            TopologySpreadConstraint(
                topology_key=topology_key,
                max_skew=max_skew,
                min_domains=min_domains,
                when_unsatisfiable=when,
                match_labels=labels,
                match_expressions=expressions,
            )
        )
    return tuple(constraints)


def _parse_tolerations(
    raw: Any,
    *,
    code: str,
    where: str,
) -> tuple[Toleration, ...]:
    result: list[Toleration] = []
    for index, toleration_raw in enumerate(_list(raw, code=code, where=where)):
        item = _mapping(toleration_raw, code=code, where=f"{where}[{index}]")
        key_raw = item.get("key", "")
        if key_raw:
            key = _label_key(key_raw, code=code, where=f"{where}[{index}].key")
        else:
            key = ""
        operator = item.get("operator", "Equal")
        if operator not in {"Equal", "Exists"}:
            raise _error(
                code,
                "toleration operator must be Equal or Exists",
                where=f"{where}[{index}].operator",
                value=operator,
            )
        value = _label_value(
            item.get("value", ""),
            code=code,
            where=f"{where}[{index}].value",
        )
        effect = item.get("effect")
        if effect is not None and effect not in VALID_EFFECTS:
            raise _error(
                code,
                "toleration effect is invalid",
                where=f"{where}[{index}].effect",
                value=effect,
            )
        if operator == "Exists" and value:
            raise _error(
                code,
                "Exists toleration must not define a value",
                where=f"{where}[{index}].value",
            )
        if operator == "Equal" and not key:
            raise _error(
                code,
                "Equal toleration requires a key",
                where=f"{where}[{index}].key",
            )
        result.append(Toleration(key, operator, value, effect))
    return tuple(
        sorted(
            result,
            key=lambda item: (item.key, item.operator, item.value, item.effect or ""),
        )
    )


def parse_pod(raw: Any, index: int) -> Pod:
    code = "E_BAD_POD"
    item = _mapping(raw, code=code, where=f"pods[{index}]")
    name = _name(item.get("name"), code=code, where=f"pods[{index}].name")
    resources = _resource_values(item, code=code, where=f"pods[{index}]", node_capacity=False)
    priority = _require_int(
        item.get("priority", 0),
        code=code,
        message="pod priority must be an integer",
    )
    labels = _labels(item.get("labels"), code=code, where=f"pods[{index}].labels")
    workload = item.get("workload")
    if workload is None:
        workload = labels.get("workload", labels.get("app", name))
    if not isinstance(workload, str) or not NAME_RE.fullmatch(workload):
        raise _error(
            code,
            "pod workload must be a valid name",
            where=f"pods[{index}].workload",
            value=workload,
        )
    affinity_raw = _first(item, "node_affinity", "nodeAffinity", default={})
    if isinstance(affinity_raw, dict) and "node_selector" in item:
        affinity_raw = dict(affinity_raw)
        affinity_raw.setdefault("node_selector", item["node_selector"])
    affinity = _parse_node_affinity(
        affinity_raw,
        code=code,
        where=f"pods[{index}].node_affinity",
    )
    anti_affinity = _parse_anti_affinity(
        _first(item, "pod_anti_affinity", "podAntiAffinity", "anti_affinity"),
        code=code,
        where=f"pods[{index}].pod_anti_affinity",
    )
    topology_spread = _parse_topology_spread(
        _first(item, "topology_spread", "topologySpreadConstraints"),
        code=code,
        where=f"pods[{index}].topology_spread",
    )
    tolerations = _parse_tolerations(
        item.get("tolerations"),
        code=code,
        where=f"pods[{index}].tolerations",
    )
    return Pod(
        name=name,
        resources=resources,
        priority=priority,
        workload=workload,
        labels=labels,
        node_affinity=affinity,
        anti_affinity=anti_affinity,
        topology_spread=topology_spread,
        tolerations=tolerations,
    )


def parse_config(raw: Any) -> ScheduleConfig:
    code = "E_BAD_CONFIG"
    item = _mapping(raw, code=code, where="schedule_config")

    filters_raw = _first(item, "filters", "filter_plugins", "enabled_filters")
    if filters_raw is None:
        filters = list(FILTER_ORDER)
    else:
        if not isinstance(filters_raw, list) or not filters_raw:
            raise _error(code, "filters must be a non-empty array", where="filters")
        resolved: set[str] = set()
        for index, name in enumerate(filters_raw):
            canonical = FILTER_ALIASES.get(name)
            if canonical is None:
                raise _error(
                    code,
                    "unknown filter plugin",
                    where=f"filters[{index}]",
                    value=name,
                    known=list(FILTER_ORDER),
                )
            if canonical in resolved:
                raise _error(code, "duplicate filter plugin", value=canonical)
            resolved.add(canonical)
        filters = [plugin for plugin in FILTER_ORDER if plugin in resolved]

    weights_raw = _first(item, "scores", "score_weights", "scoring_weights")
    if weights_raw is None:
        weights_raw = {plugin: 1 for plugin in SCORE_ORDER}
    weights_mapping = _mapping(weights_raw, code=code, where="scores")
    weights: dict[str, Decimal] = {plugin: Decimal(0) for plugin in SCORE_ORDER}
    seen_weights: set[str] = set()
    for name, value in weights_mapping.items():
        canonical = SCORE_ALIASES.get(name)
        if canonical is None:
            raise _error(
                code,
                "unknown scoring plugin",
                where=f"scores.{name}",
                value=name,
                known=list(SCORE_ORDER),
            )
        if canonical in seen_weights:
            raise _error(code, "duplicate scoring plugin", value=canonical)
        seen_weights.add(canonical)
        weights[canonical] = _parse_decimal(
            value,
            code=code,
            where=f"scores.{name}",
            minimum=Decimal(0),
        )
    if sum(weights.values(), Decimal(0)) <= 0:
        raise _error(code, "scoring weights must have a positive sum", where="scores")

    budget_ms = _require_int(
        _first(item, "total_time_budget_ms", "budget_ms", "round_budget_ms"),
        code=code,
        message="total_time_budget_ms must be a positive integer",
        minimum=1,
    )
    concurrency = _require_int(
        _first(
            item,
            "max_concurrent_per_workload",
            "workload_concurrency_limit",
            "max_concurrency_per_workload",
        ),
        code=code,
        message="max_concurrent_per_workload must be a positive integer",
        minimum=1,
    )
    retry_budget = _require_int(
        item.get("retry_budget", 0),
        code=code,
        message="retry_budget must be a non-negative integer",
        minimum=0,
    )
    clock_step = _require_int(
        _first(item, "logical_clock_step_ms", "clock_step_ms"),
        code=code,
        message="logical_clock_step_ms must be a positive integer",
        minimum=1,
    )
    max_parallel = _require_int(
        _first(item, "max_parallel_pods", "global_concurrency_limit", default=max(concurrency, 8)),
        code=code,
        message="max_parallel_pods must be a positive integer",
        minimum=1,
    )
    retry_delay = _require_int(
        _first(item, "retry_delay_ms", default=clock_step),
        code=code,
        message="retry_delay_ms must be a non-negative integer",
        minimum=0,
    )
    weight_decimals = _require_int(
        _first(item, "weight_decimals", default=12),
        code=code,
        message="weight_decimals must be between 0 and 15",
        minimum=0,
    )
    score_decimals = _require_int(
        _first(item, "score_decimals", default=6),
        code=code,
        message="score_decimals must be between 0 and 15",
        minimum=0,
    )
    if weight_decimals > 15 or score_decimals > 15:
        raise _error(code, "decimal precision must be between 0 and 15")
    rounding = item.get("weight_rounding", "ROUND_HALF_EVEN")
    if rounding not in {"ROUND_HALF_EVEN", "ROUND_HALF_UP"}:
        raise _error(
            code,
            "weight_rounding must be ROUND_HALF_EVEN or ROUND_HALF_UP",
            value=rounding,
        )
    tolerance = _parse_decimal(
        _first(item, "precision_tolerance", "score_tolerance", default="0.000001"),
        code=code,
        where="precision_tolerance",
        minimum=Decimal(0),
    )

    costs_raw = _mapping(item.get("plugin_costs_ms", {}), code=code, where="plugin_costs_ms")
    filter_costs_raw = _mapping(
        costs_raw.get("filters", costs_raw.get("filter", {})),
        code=code,
        where="plugin_costs_ms.filters",
    )
    score_costs_raw = _mapping(
        costs_raw.get("scores", costs_raw.get("score", {})),
        code=code,
        where="plugin_costs_ms.scores",
    )
    filter_costs: dict[str, int] = {plugin: clock_step for plugin in FILTER_ORDER}
    for name, value in filter_costs_raw.items():
        canonical = FILTER_ALIASES.get(name)
        if canonical is None:
            raise _error(code, "unknown filter plugin in plugin_costs_ms", value=name)
        filter_costs[canonical] = _require_int(
            value,
            code=code,
            message="filter plugin cost must be a non-negative integer",
            minimum=0,
        )
    score_costs: dict[str, int] = {plugin: clock_step for plugin in SCORE_ORDER}
    for name, value in score_costs_raw.items():
        canonical = SCORE_ALIASES.get(name)
        if canonical is None:
            raise _error(code, "unknown scoring plugin in plugin_costs_ms", value=name)
        score_costs[canonical] = _require_int(
            value,
            code=code,
            message="scoring plugin cost must be a non-negative integer",
            minimum=0,
        )
    bind_cost = _require_int(
        costs_raw.get("bind", clock_step),
        code=code,
        message="bind cost must be a non-negative integer",
        minimum=0,
    )

    return ScheduleConfig(
        filters=tuple(filters),
        score_weights=weights,
        budget_ms=budget_ms,
        max_concurrent_per_workload=concurrency,
        max_parallel_pods=max_parallel,
        retry_budget=retry_budget,
        clock_step_ms=clock_step,
        retry_delay_ms=retry_delay,
        filter_costs=filter_costs,
        score_costs=score_costs,
        bind_cost_ms=bind_cost,
        precision_tolerance=tolerance,
        weight_decimals=weight_decimals,
        score_decimals=score_decimals,
        weight_rounding=rounding,
    )


def parse_injections(raw: Any, pod_names: set[str]) -> tuple[Injection, ...]:
    code = "E_BAD_INJECTION"
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw = raw.get("injections", [])
    if not isinstance(raw, list):
        raise _error(code, "injections must be an array", where="injections")
    result: list[Injection] = []
    valid_phases = {"filter", "score", "bind", "after_filter"}
    for index, injection_raw in enumerate(raw):
        item = _mapping(injection_raw, code=code, where=f"injections[{index}]")
        kind = item.get("type", "latency")
        if kind not in {"latency", "terminate", "abandon"}:
            raise _error(code, "unknown injection type", value=kind)
        pod = item.get("pod")
        if pod not in pod_names:
            raise _error(code, "injection references unknown pod", value=pod)
        phase = item.get("phase", "filter")
        if phase not in valid_phases:
            raise _error(code, "invalid injection phase", value=phase)
        plugin = item.get("plugin")
        if plugin is not None:
            canonical = FILTER_ALIASES.get(plugin) or SCORE_ALIASES.get(plugin)
            if canonical is None:
                raise _error(code, "injection references unknown plugin", value=plugin)
            plugin = canonical
        if kind == "latency":
            ms = _require_int(
                item.get("ms"),
                code=code,
                message="latency injection requires non-negative integer ms",
                minimum=0,
            )
            result.append(Injection(kind, pod, phase, plugin, ms=ms))
        elif kind == "terminate":
            exit_code = _require_int(
                item.get("exit_code", 137),
                code=code,
                message="terminate exit_code must be a positive integer",
                minimum=1,
            )
            result.append(Injection(kind, pod, phase, plugin, exit_code=exit_code))
        else:
            reason = item.get("reason", "injected active abandon")
            if not isinstance(reason, str) or not reason:
                raise _error(code, "abandon reason must be a non-empty string")
            result.append(Injection(kind, pod, phase, plugin, reason=reason))
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.pod,
                item.phase,
                item.plugin or "",
                item.kind,
                item.ms,
            ),
        )
    )


def parse_dataset(
    nodes_document: Any,
    pods_document: Any,
    config_document: Any,
    injections_document: Any = None,
) -> Dataset:
    nodes_raw = _unwrap(nodes_document, "nodes", code="E_BAD_NODE")
    pods_raw = _unwrap(pods_document, "pods", code="E_BAD_POD")
    nodes: list[Node] = []
    node_names: set[str] = set()
    for index, raw in enumerate(nodes_raw):
        node = parse_node(raw, index)
        if node.name in node_names:
            raise bad_node("duplicate node name", name=node.name)
        node_names.add(node.name)
        nodes.append(node)
    pods: list[Pod] = []
    pod_names: set[str] = set()
    for index, raw in enumerate(pods_raw):
        pod = parse_pod(raw, index)
        if pod.name in pod_names:
            raise bad_pod("duplicate pod name", name=pod.name)
        pod_names.add(pod.name)
        pods.append(pod)
    if not nodes:
        raise bad_node("node list must not be empty")
    if not pods:
        raise bad_pod("pod list must not be empty")
    config = parse_config(config_document)
    injections = parse_injections(injections_document, pod_names)
    return Dataset(
        nodes=tuple(sorted(nodes, key=lambda item: item.name)),
        pods=tuple(sorted(pods, key=lambda item: item.name)),
        config=config,
        injections=injections,
    )


def labels_match(
    labels: dict[str, str],
    match_labels: Iterable[tuple[str, str]] = (),
    expressions: Iterable[MatchExpression] = (),
) -> bool:
    for key, value in match_labels:
        if labels.get(key) != value:
            return False
    for expression in expressions:
        actual = labels.get(expression.key)
        if expression.operator == "In" and actual not in expression.values:
            return False
        if expression.operator == "NotIn" and actual in expression.values:
            return False
        if expression.operator == "Exists" and actual is None:
            return False
        if expression.operator == "DoesNotExist" and actual is not None:
            return False
        if expression.operator in {"Gt", "Lt"}:
            try:
                actual_number = int(actual) if actual is not None else None
                target = int(expression.values[0])
            except (TypeError, ValueError):
                return False
            if actual_number is None:
                return False
            if expression.operator == "Gt" and actual_number <= target:
                return False
            if expression.operator == "Lt" and actual_number >= target:
                return False
    return True
