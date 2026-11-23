"""Declarative policy objects and validation.

Validation is pure: constructing a ``PolicyBundle`` never touches SQLite.  The
CLI relies on this to guarantee that rejected input does not create or modify
persistent state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import (
    DEPENDENCY_CYCLE,
    DUPLICATE_NAME,
    INVALID_AGGREGATION,
    INVALID_BUDGET,
    INVALID_ERROR_BUDGET,
    INVALID_PERCENTILE,
    INVALID_POSITIVE_INTEGER,
    INVALID_REPLICA_RANGE,
    INVALID_RULE,
    INVALID_SCALE,
    INVALID_THRESHOLD,
    INVALID_WINDOW,
    MISSING_REFERENCE,
    SCHEMA_ERROR,
    ControlError,
)
from .util import dec, decimal_text, integer, jsonable, load_json_file


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlError(
            SCHEMA_ERROR,
            f"{field} must be an object",
            {"field": field, "actual_type": type(value).__name__},
        )
    return value


def _string(value: Any, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ControlError(
            SCHEMA_ERROR,
            f"{field} must be a non-empty string",
            {"field": field},
        )
    return value


def _pick(data: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _required(data: Mapping[str, Any], names: Iterable[str], field: str) -> Any:
    sentinel = object()
    value = _pick(data, names, sentinel)
    if value is sentinel:
        raise ControlError(
            SCHEMA_ERROR, f"missing required field {field}", {"field": field}
        )
    return value


@dataclass(frozen=True)
class Workload:
    name: str
    current_replicas: int
    min_replicas: int
    max_replicas: int
    cpu_millicores: int
    memory_mib: int
    topology_domain: str = "default"

    @classmethod
    def from_dict(cls, raw: Any) -> "Workload":
        data = _mapping(raw, "workload")
        name = _string(data.get("name"), "workload.name")
        current = integer(data.get("current_replicas"), "workload.current_replicas", nonnegative=True)
        minimum = integer(data.get("min_replicas"), "workload.min_replicas", nonnegative=True)
        maximum = integer(data.get("max_replicas"), "workload.max_replicas", positive=True)
        if minimum > maximum:
            raise ControlError(
                INVALID_REPLICA_RANGE,
                f"workload {name!r}: min_replicas must not exceed max_replicas",
                {"workload": name, "min_replicas": minimum, "max_replicas": maximum},
            )
        cpu = integer(data.get("cpu_millicores"), "workload.cpu_millicores", positive=True)
        memory = integer(data.get("memory_mib"), "workload.memory_mib", positive=True)
        topology = _string(
            data.get("topology_domain", data.get("topology", "default")),
            "workload.topology_domain",
        )
        return cls(
            name=name,
            current_replicas=current,
            min_replicas=minimum,
            max_replicas=maximum,
            cpu_millicores=cpu,
            memory_mib=memory,
            topology_domain=topology,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current_replicas": self.current_replicas,
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "cpu_millicores": self.cpu_millicores,
            "memory_mib": self.memory_mib,
            "topology_domain": self.topology_domain,
        }


@dataclass(frozen=True)
class MetricSource:
    name: str
    aggregation: str
    window_ms: int
    target_value: Decimal
    alert_threshold: Decimal
    scale: int
    error_budget: Decimal
    percentile: Decimal | None = None
    lower_threshold: Decimal | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "MetricSource":
        data = _mapping(raw, "metric")
        name = _string(data.get("name"), "metric.name")
        aggregation = _string(
            data.get("aggregation", data.get("aggregate", "average")),
            "metric.aggregation",
        ).lower()
        if aggregation not in {"average", "percentile"}:
            raise ControlError(
                INVALID_AGGREGATION,
                f"metric {name!r}: aggregation must be average or percentile",
                {"metric": name, "aggregation": aggregation},
            )
        percentile_raw = data.get("percentile", data.get("quantile"))
        percentile: Decimal | None
        if aggregation == "percentile":
            if percentile_raw is None:
                raise ControlError(
                    INVALID_PERCENTILE,
                    f"metric {name!r}: percentile is required for percentile aggregation",
                    {"metric": name},
                )
            percentile = dec(percentile_raw, "metric.percentile")
            if percentile < 0 or percentile > 100:
                raise ControlError(
                    INVALID_PERCENTILE,
                    f"metric {name!r}: percentile must be in [0, 100]",
                    {"metric": name, "percentile": decimal_text(percentile)},
                )
        else:
            percentile = None
            if percentile_raw is not None:
                # An unused parameter is still checked rather than silently ignored.
                unused = dec(percentile_raw, "metric.percentile")
                if unused < 0 or unused > 100:
                    raise ControlError(
                        INVALID_PERCENTILE,
                        f"metric {name!r}: percentile must be in [0, 100]",
                        {"metric": name, "percentile": decimal_text(unused)},
                    )
        try:
            window = integer(data.get("window_ms"), "metric.window_ms", positive=True)
        except ControlError as exc:
            raise ControlError(
                INVALID_WINDOW,
                f"metric {name!r}: window_ms must be a positive integer",
                {"metric": name, "reason": exc.code},
            ) from exc
        target = dec(_required(data, ("target_value", "target"), "metric.target_value"), "metric.target_value")
        threshold = dec(
            _required(data, ("alert_threshold", "threshold"), "metric.alert_threshold"),
            "metric.alert_threshold",
        )
        lower_raw = _pick(data, ("lower_threshold", "down_threshold"), None)
        lower = None if lower_raw is None else dec(lower_raw, "metric.lower_threshold")
        if target <= 0:
            raise ControlError(
                INVALID_THRESHOLD,
                f"metric {name!r}: target_value must be positive",
                {"metric": name, "target_value": decimal_text(target)},
            )
        if threshold <= 0:
            raise ControlError(
                INVALID_THRESHOLD,
                f"metric {name!r}: alert_threshold must be positive",
                {"metric": name, "alert_threshold": decimal_text(threshold)},
            )
        if lower is not None and lower < 0:
            raise ControlError(
                INVALID_THRESHOLD,
                f"metric {name!r}: lower_threshold must be non-negative",
                {"metric": name, "lower_threshold": decimal_text(lower)},
            )
        if lower is not None and lower >= threshold:
            raise ControlError(
                INVALID_THRESHOLD,
                f"metric {name!r}: lower_threshold must be below alert_threshold",
                {
                    "metric": name,
                    "lower_threshold": decimal_text(lower),
                    "alert_threshold": decimal_text(threshold),
                },
            )
        scale_raw = data.get("scale", data.get("fixed_scale"))
        if scale_raw is None:
            raise ControlError(
                INVALID_SCALE,
                f"metric {name!r}: scale is required",
                {"metric": name},
            )
        try:
            scale = integer(scale_raw, "metric.scale", positive=True)
        except ControlError as exc:
            raise ControlError(
                INVALID_SCALE,
                f"metric {name!r}: scale must be a positive integer",
                {"metric": name, "reason": exc.code},
            ) from exc
        if scale <= 0:
            raise ControlError(
                INVALID_SCALE,
                f"metric {name!r}: scale must be positive",
                {"metric": name, "scale": scale},
            )
        error_budget = dec(
            data.get("error_budget", data.get("error_upper_bound", data.get("max_error", 0))),
            "metric.error_budget",
        )
        if error_budget <= 0:
            raise ControlError(
                INVALID_ERROR_BUDGET,
                f"metric {name!r}: error_budget must be positive",
                {"metric": name, "error_budget": decimal_text(error_budget)},
            )
        return cls(
            name=name,
            aggregation=aggregation,
            window_ms=window,
            target_value=target,
            alert_threshold=threshold,
            scale=scale,
            error_budget=error_budget,
            percentile=percentile,
            lower_threshold=lower,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "aggregation": self.aggregation,
            "window_ms": self.window_ms,
            "target_value": decimal_text(self.target_value),
            "alert_threshold": decimal_text(self.alert_threshold),
            "scale": self.scale,
            "error_budget": decimal_text(self.error_budget),
        }
        if self.percentile is not None:
            result["percentile"] = decimal_text(self.percentile)
        if self.lower_threshold is not None:
            result["lower_threshold"] = decimal_text(self.lower_threshold)
        return result


@dataclass(frozen=True)
class ScaleRule:
    name: str
    workload: str
    metric: str
    step: int
    percent: Decimal
    cooldown_ms: int
    freeze_ms: int
    priority: int
    direction: str = "auto"
    duration_ms: int | None = None
    version: int = 0

    @classmethod
    def from_dict(cls, raw: Any) -> "ScaleRule":
        data = _mapping(raw, "rule")
        name = _string(data.get("name"), "rule.name")
        workload = _string(data.get("workload"), "rule.workload")
        metric = _string(data.get("metric", data.get("metric_source")), "rule.metric")
        step = integer(data.get("step", 0), "rule.step", nonnegative=True)
        percent = dec(data.get("percent", data.get("percentage", 0)), "rule.percent")
        if percent < 0:
            raise ControlError(
                INVALID_RULE,
                f"rule {name!r}: percent must be non-negative",
                {"rule": name, "percent": decimal_text(percent)},
            )
        if step == 0 and percent == 0:
            raise ControlError(
                INVALID_RULE,
                f"rule {name!r}: step and percent cannot both be zero",
                {"rule": name},
            )
        try:
            cooldown = integer(
                data.get("cooldown_ms", 0), "rule.cooldown_ms", nonnegative=True
            )
            freeze = integer(
                data.get("freeze_ms", 0), "rule.freeze_ms", nonnegative=True
            )
        except ControlError as exc:
            raise ControlError(
                INVALID_WINDOW,
                f"rule {name!r}: cooldown_ms and freeze_ms must be non-negative integers",
                {"rule": name, "reason": exc.code},
            ) from exc
        priority = integer(data.get("priority", 0), "rule.priority")
        direction = str(data.get("direction", "auto")).lower()
        if direction not in {"up", "down", "auto"}:
            raise ControlError(
                INVALID_RULE,
                f"rule {name!r}: direction must be up, down or auto",
                {"rule": name, "direction": direction},
            )
        duration_raw = data.get("duration_ms")
        duration = (
            None
            if duration_raw is None
            else integer(duration_raw, "rule.duration_ms", positive=True)
        )
        version = integer(data.get("version", 1), "rule.version", nonnegative=True)
        return cls(
            name=name,
            workload=workload,
            metric=metric,
            step=step,
            percent=percent,
            cooldown_ms=cooldown,
            freeze_ms=freeze,
            priority=priority,
            direction=direction,
            duration_ms=duration,
            version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "workload": self.workload,
            "metric": self.metric,
            "step": self.step,
            "percent": decimal_text(self.percent),
            "cooldown_ms": self.cooldown_ms,
            "freeze_ms": self.freeze_ms,
            "priority": self.priority,
            "direction": self.direction,
            "version": self.version,
        }
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result


@dataclass(frozen=True)
class ResourceBudget:
    pool_cpu_core_ms: Decimal
    pool_memory_mib: Decimal
    round_time_budget_ms: int
    per_workload_concurrency: int
    float_tolerance: Decimal
    error_upper_bound: Decimal
    clock_rollback_tolerance_ms: int = 0
    backoff_base_ms: int = 1000
    action_duration_ms: int = 1000

    @classmethod
    def from_dict(cls, raw: Any) -> "ResourceBudget":
        data = _mapping(raw, "budget")
        cpu = dec(
            _required(
                data,
                ("pool_cpu_core_ms", "total_cpu_core_ms", "cpu_core_ms"),
                "budget.pool_cpu_core_ms",
            ),
            "budget.pool_cpu_core_ms",
        )
        memory = dec(
            _required(
                data,
                ("pool_memory_mib", "total_memory_mib", "memory_mib"),
                "budget.pool_memory_mib",
            ),
            "budget.pool_memory_mib",
        )
        logical_time = integer(
            _required(
                data,
                ("round_time_budget_ms", "logical_time_budget_ms"),
                "budget.round_time_budget_ms",
            ),
            "budget.round_time_budget_ms",
            positive=True,
        )
        concurrency = integer(
            data.get(
                "per_workload_concurrency",
                data.get("max_actions_per_workload", 1),
            ),
            "budget.per_workload_concurrency",
            positive=True,
        )
        tolerance = dec(
            _required(
                data,
                ("float_tolerance", "tolerance"),
                "budget.float_tolerance",
            ),
            "budget.float_tolerance",
        )
        error_bound = dec(
            data.get("error_upper_bound", data.get("error_budget", 0)),
            "budget.error_upper_bound",
        )
        rollback = integer(
            data.get("clock_rollback_tolerance_ms", 0),
            "budget.clock_rollback_tolerance_ms",
            nonnegative=True,
        )
        backoff = integer(
            data.get("backoff_base_ms", 1000),
            "budget.backoff_base_ms",
            nonnegative=True,
        )
        duration = integer(
            data.get("action_duration_ms", 1000),
            "budget.action_duration_ms",
            positive=True,
        )
        if cpu <= 0 or memory <= 0:
            raise ControlError(
                INVALID_BUDGET,
                "pool CPU and memory limits must be positive",
                {"pool_cpu_core_ms": decimal_text(cpu), "pool_memory_mib": decimal_text(memory)},
            )
        if tolerance <= 0:
            raise ControlError(
                INVALID_BUDGET,
                "float_tolerance must be positive",
                {"float_tolerance": decimal_text(tolerance)},
            )
        if error_bound <= 0:
            raise ControlError(
                INVALID_ERROR_BUDGET,
                "error_upper_bound must be positive",
                {"error_upper_bound": decimal_text(error_bound)},
            )
        return cls(
            pool_cpu_core_ms=cpu,
            pool_memory_mib=memory,
            round_time_budget_ms=logical_time,
            per_workload_concurrency=concurrency,
            float_tolerance=tolerance,
            error_upper_bound=error_bound,
            clock_rollback_tolerance_ms=rollback,
            backoff_base_ms=backoff,
            action_duration_ms=duration,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_cpu_core_ms": decimal_text(self.pool_cpu_core_ms),
            "pool_memory_mib": decimal_text(self.pool_memory_mib),
            "round_time_budget_ms": self.round_time_budget_ms,
            "per_workload_concurrency": self.per_workload_concurrency,
            "float_tolerance": decimal_text(self.float_tolerance),
            "error_upper_bound": decimal_text(self.error_upper_bound),
            "clock_rollback_tolerance_ms": self.clock_rollback_tolerance_ms,
            "backoff_base_ms": self.backoff_base_ms,
            "action_duration_ms": self.action_duration_ms,
        }


@dataclass
class PolicyBundle:
    workloads: dict[str, Workload] = field(default_factory=dict)
    metrics: dict[str, MetricSource] = field(default_factory=dict)
    rules: dict[str, ScaleRule] = field(default_factory=dict)
    budget: ResourceBudget | None = None

    @classmethod
    def empty(cls) -> "PolicyBundle":
        return cls()

    @classmethod
    def from_payload(cls, payload: Any) -> "PolicyBundle":
        data = _mapping(payload, "policy")
        workloads_raw = data.get("workloads", data.get("workload", []))
        metrics_raw = data.get("metrics", data.get("metric_sources", data.get("metric", [])))
        rules_raw = data.get("rules", data.get("scale_rules", []))
        if isinstance(workloads_raw, Mapping):
            workloads_raw = list(workloads_raw.values())
        if isinstance(metrics_raw, Mapping):
            metrics_raw = list(metrics_raw.values())
        if isinstance(rules_raw, Mapping):
            rules_raw = list(rules_raw.values())
        if not isinstance(workloads_raw, list):
            raise ControlError(SCHEMA_ERROR, "workloads must be an array")
        if not isinstance(metrics_raw, list):
            raise ControlError(SCHEMA_ERROR, "metrics must be an array")
        if not isinstance(rules_raw, list):
            raise ControlError(SCHEMA_ERROR, "rules must be an array")

        workloads: dict[str, Workload] = {}
        for raw in workloads_raw:
            item = Workload.from_dict(raw)
            if item.name in workloads:
                raise ControlError(
                    DUPLICATE_NAME,
                    f"duplicate workload name {item.name!r}",
                    {"kind": "workload", "name": item.name},
                )
            workloads[item.name] = item
        metrics: dict[str, MetricSource] = {}
        for raw in metrics_raw:
            item = MetricSource.from_dict(raw)
            if item.name in metrics:
                raise ControlError(
                    DUPLICATE_NAME,
                    f"duplicate metric name {item.name!r}",
                    {"kind": "metric", "name": item.name},
                )
            metrics[item.name] = item
        rules: dict[str, ScaleRule] = {}
        for raw in rules_raw:
            item = ScaleRule.from_dict(raw)
            if item.name in rules:
                raise ControlError(
                    DUPLICATE_NAME,
                    f"duplicate rule name {item.name!r}",
                    {"kind": "rule", "name": item.name},
                )
            rules[item.name] = item
        budget_raw = data.get("budget")
        budget = None if budget_raw is None else ResourceBudget.from_dict(budget_raw)
        bundle = cls(workloads=workloads, metrics=metrics, rules=rules, budget=budget)
        bundle.validate()
        return bundle

    def validate(self) -> None:
        if self.budget is None:
            raise ControlError(SCHEMA_ERROR, "missing resource budget")
        for rule in self.rules.values():
            if rule.workload not in self.workloads:
                raise ControlError(
                    MISSING_REFERENCE,
                    f"rule {rule.name!r} references unknown workload {rule.workload!r}",
                    {
                        "rule": rule.name,
                        "workload": rule.workload,
                        "reference_kind": "workload",
                    },
                )
            if rule.metric not in self.metrics:
                raise ControlError(
                    MISSING_REFERENCE,
                    f"rule {rule.name!r} references unknown metric {rule.metric!r}",
                    {
                        "rule": rule.name,
                        "metric": rule.metric,
                        "reference_kind": "metric",
                    },
                )
        self.detect_cycles()

    def detect_cycles(self) -> None:
        """Validate the object dependency DAG.

        Rules reference workloads and metrics.  An optional ``depends_on``
        array on workloads/metrics is also honoured, which makes cycle checking
        useful for future policy extensions without changing the schema.
        """

        nodes: dict[str, set[str]] = {}
        for name in self.workloads:
            nodes[f"workload:{name}"] = set()
        for name in self.metrics:
            nodes[f"metric:{name}"] = set()
        for name in self.rules:
            nodes[f"rule:{name}"] = set()
        for workload in self.workloads.values():
            for dependency in getattr(workload, "depends_on", ()) or ():
                nodes[f"workload:{workload.name}"].add(str(dependency))
        for rule in self.rules.values():
            nodes[f"rule:{rule.name}"].add(f"workload:{rule.workload}")
            nodes[f"rule:{rule.name}"].add(f"metric:{rule.metric}")

        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(node: str) -> None:
            marker = state.get(node, 0)
            if marker == 1:
                cycle_start = stack.index(node) if node in stack else 0
                cycle = stack[cycle_start:] + [node]
                raise ControlError(
                    DEPENDENCY_CYCLE,
                    "policy dependency cycle detected",
                    {"cycle": cycle},
                )
            if marker == 2:
                return
            state[node] = 1
            stack.append(node)
            for target in sorted(nodes.get(node, ())):
                if target not in nodes:
                    raise ControlError(
                        MISSING_REFERENCE,
                        f"dependency {target!r} does not exist",
                        {"source": node, "target": target},
                    )
                visit(target)
            stack.pop()
            state[node] = 2

        for node in sorted(nodes):
            visit(node)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workloads": [
                self.workloads[name].to_dict() for name in sorted(self.workloads)
            ],
            "metrics": [
                self.metrics[name].to_dict() for name in sorted(self.metrics)
            ],
            "rules": [self.rules[name].to_dict() for name in sorted(self.rules)],
            "budget": None if self.budget is None else self.budget.to_dict(),
        }

    def copy(self) -> "PolicyBundle":
        return PolicyBundle.from_payload(jsonable(self.to_dict()))

    def rules_for_workload(self, workload: str) -> list[ScaleRule]:
        return sorted(
            (rule for rule in self.rules.values() if rule.workload == workload),
            key=lambda rule: (-rule.priority, rule.name),
        )

    def metrics_for_workload(self, workload: str) -> set[str]:
        return {
            rule.metric for rule in self.rules.values() if rule.workload == workload
        }


OBJECT_FILES = {
    "workloads": "workloads.json",
    "metrics": "metrics.json",
    "rules": "rules.json",
    "budget": "budget.json",
}


def load_policy_from_directory(directory: str | Path) -> PolicyBundle:
    base = Path(directory)
    if not base.exists():
        raise ControlError(
            SCHEMA_ERROR, f"sample directory does not exist: {base}", {"path": str(base)}
        )
    payload: dict[str, Any] = {}
    for key, filename in OBJECT_FILES.items():
        path = base / filename
        if path.exists():
            loaded = load_json_file(path)
            if isinstance(loaded, Mapping) and key in loaded:
                payload[key] = loaded[key]
            elif isinstance(loaded, list):
                payload[key] = loaded
            elif isinstance(loaded, Mapping):
                # Permit a single object file such as budget.json {"budget": {...}}
                # or {"pool_cpu_core_ms": ...}.
                payload[key] = loaded.get(key, loaded)
            else:
                raise ControlError(
                    SCHEMA_ERROR,
                    f"{path} must contain an object or array",
                    {"path": str(path)},
                )
    if not payload:
        combined = base / "policy.json"
        if combined.exists():
            return PolicyBundle.from_payload(load_json_file(combined))
        raise ControlError(
            SCHEMA_ERROR,
            f"no policy files found in {base}",
            {"path": str(base)},
        )
    return PolicyBundle.from_payload(payload)


def load_combined_policy(path: str | Path) -> PolicyBundle:
    return PolicyBundle.from_payload(load_json_file(path))
