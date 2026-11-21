from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .errors import fail
from .fixed import (
    as_decimal,
    as_int,
    core_hours_to_units,
    to_fixed,
)


def _read_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail("E_FILE_NOT_FOUND", "input file does not exist", {"path": str(path)})
    except json.JSONDecodeError as exc:
        fail(
            "E_JSON_PARSE",
            "input file is not valid JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        )
    except OSError as exc:
        fail(
            "E_FILE_IO",
            "input file could not be read",
            {"path": str(path), "error": str(exc)},
        )


def load_json(path: str | Path) -> Any:
    return _read_json(path)


def _object_list(
    payload: Any,
    *,
    top_key: str,
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        fail(
            "E_SCHEMA",
            f"{path} must contain a JSON object",
            {"path": path},
        )
    value = payload.get(top_key)
    if not isinstance(value, list):
        fail(
            "E_SCHEMA",
            f"{path} must contain a '{top_key}' array",
            {"path": path, "key": top_key},
        )
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            fail(
                "E_SCHEMA",
                f"{top_key}[{index}] must be an object",
                {"path": path, "index": index},
            )
        result.append(item)
    return result


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(
            "E_SCHEMA",
            f"{context}.{key} must be a non-empty string",
            {"object": context, "field": key},
        )
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        fail(
            "E_SCHEMA",
            f"{key} must be a string",
            {"field": key},
        )
    return value.strip()


@dataclass(frozen=True)
class Workload:
    id: str
    name: str
    min_replicas: int
    max_replicas: int
    current_replicas: int
    cpu_millicores_per_replica: int
    memory_mib_per_replica: int
    topology_domain: str
    version: int = 1

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "Workload":
        context = f"workloads[{index}]"
        workload_id = _required_string(data, "id", context)
        minimum = as_int(data.get("minReplicas"), f"{context}.minReplicas", minimum=0)
        maximum = as_int(data.get("maxReplicas"), f"{context}.maxReplicas", minimum=1)
        current = as_int(
            data.get("currentReplicas"),
            f"{context}.currentReplicas",
            minimum=0,
        )
        cpu = as_int(
            data.get("cpuMillicoresPerReplica"),
            f"{context}.cpuMillicoresPerReplica",
            minimum=1,
        )
        memory = as_int(
            data.get("memoryMiBPerReplica"),
            f"{context}.memoryMiBPerReplica",
            minimum=1,
        )
        if minimum > maximum:
            fail(
                "E_REPLICA_RANGE",
                "minReplicas must not exceed maxReplicas",
                {
                    "object": context,
                    "id": workload_id,
                    "minReplicas": minimum,
                    "maxReplicas": maximum,
                },
            )
        if current < minimum or current > maximum:
            fail(
                "E_REPLICA_RANGE",
                "currentReplicas must be within minReplicas and maxReplicas",
                {
                    "object": context,
                    "id": workload_id,
                    "currentReplicas": current,
                    "minReplicas": minimum,
                    "maxReplicas": maximum,
                },
            )
        topology = _optional_string(data, "topologyDomain", "default")
        return cls(
            id=workload_id,
            name=_required_string(data, "name", context),
            min_replicas=minimum,
            max_replicas=maximum,
            current_replicas=current,
            cpu_millicores_per_replica=cpu,
            memory_mib_per_replica=memory,
            topology_domain=topology or "default",
        )

    def to_json(self, *, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "minReplicas": self.min_replicas,
            "maxReplicas": self.max_replicas,
            "currentReplicas": self.current_replicas,
            "cpuMillicoresPerReplica": self.cpu_millicores_per_replica,
            "memoryMiBPerReplica": self.memory_mib_per_replica,
            "topologyDomain": self.topology_domain,
        }
        if include_version:
            result["version"] = self.version
        return result


@dataclass(frozen=True)
class MetricSource:
    id: str
    name: str
    aggregation: str
    percentile: Decimal | None
    window_ms: int
    target_value: Decimal
    alert_threshold: Decimal
    version: int = 1

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "MetricSource":
        context = f"metricSources[{index}]"
        metric_id = _required_string(data, "id", context)
        aggregation = _required_string(data, "aggregation", context).lower()
        if aggregation not in {"avg", "percentile"}:
            fail(
                "E_INVALID_AGGREGATION",
                "aggregation must be 'avg' or 'percentile'",
                {"object": context, "id": metric_id, "aggregation": aggregation},
            )
        percentile: Decimal | None = None
        if aggregation == "percentile":
            percentile = as_decimal(
                data.get("percentile"),
                f"{context}.percentile",
            )
            if percentile <= 0 or percentile > 1:
                fail(
                    "E_INVALID_PERCENTILE",
                    "percentile must be greater than 0 and at most 1",
                    {
                        "object": context,
                        "id": metric_id,
                        "percentile": str(percentile),
                    },
                )
        elif data.get("percentile") not in (None, ""):
            fail(
                "E_INVALID_PERCENTILE",
                "percentile is only valid for percentile aggregation",
                {"object": context, "id": metric_id},
            )
        target = as_decimal(data.get("targetValue"), f"{context}.targetValue")
        if target <= 0:
            fail(
                "E_INVALID_NUMBER",
                "targetValue must be greater than zero",
                {"object": context, "id": metric_id, "targetValue": str(target)},
            )
        alert = as_decimal(data.get("alertThreshold"), f"{context}.alertThreshold")
        return cls(
            id=metric_id,
            name=_required_string(data, "name", context),
            aggregation=aggregation,
            percentile=percentile,
            window_ms=as_int(
                data.get("windowMs"),
                f"{context}.windowMs",
                minimum=1,
            ),
            target_value=target,
            alert_threshold=alert,
        )

    def to_json(self, *, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "aggregation": self.aggregation,
            "windowMs": self.window_ms,
            "targetValue": str(self.target_value),
            "alertThreshold": str(self.alert_threshold),
        }
        if self.percentile is not None:
            result["percentile"] = str(self.percentile)
        if include_version:
            result["version"] = self.version
        return result


@dataclass(frozen=True)
class ScalingRule:
    id: str
    name: str
    workload_id: str
    metric_source_id: str
    direction: str
    step: int
    percent: Decimal
    priority: int
    cooldown_ms: int
    version: int = 1

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "ScalingRule":
        context = f"scalingRules[{index}]"
        direction = _required_string(data, "direction", context).lower()
        if direction not in {"scale_out", "scale_in"}:
            fail(
                "E_INVALID_DIRECTION",
                "direction must be 'scale_out' or 'scale_in'",
                {"object": context, "direction": direction},
            )
        percent = as_decimal(data.get("percent"), f"{context}.percent")
        if percent <= 0 or percent > 100:
            fail(
                "E_INVALID_PERCENT",
                "percent must be greater than 0 and at most 100",
                {"object": context, "percent": str(percent)},
            )
        return cls(
            id=_required_string(data, "id", context),
            name=_required_string(data, "name", context),
            workload_id=_required_string(data, "workloadId", context),
            metric_source_id=_required_string(data, "metricSourceId", context),
            direction=direction,
            step=as_int(data.get("step"), f"{context}.step", minimum=1),
            percent=percent,
            priority=as_int(data.get("priority"), f"{context}.priority", minimum=0),
            cooldown_ms=as_int(
                data.get("cooldownMs"),
                f"{context}.cooldownMs",
                minimum=1,
            ),
        )

    def to_json(self, *, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "workloadId": self.workload_id,
            "metricSourceId": self.metric_source_id,
            "direction": self.direction,
            "step": self.step,
            "percent": str(self.percent),
            "priority": self.priority,
            "cooldownMs": self.cooldown_ms,
        }
        if include_version:
            result["version"] = self.version
        return result


@dataclass(frozen=True)
class FreezeWindow:
    id: str
    start_ms: int
    end_ms: int

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "FreezeWindow":
        context = f"resourceBudget.freezeWindows[{index}]"
        start = as_int(data.get("startMs"), f"{context}.startMs", minimum=0)
        end = as_int(data.get("endMs"), f"{context}.endMs", minimum=1)
        if start >= end:
            fail(
                "E_INVALID_FREEZE_WINDOW",
                "freeze window must have startMs < endMs",
                {"object": context, "startMs": start, "endMs": end},
            )
        return cls(
            id=_required_string(data, "id", context),
            start_ms=start,
            end_ms=end,
        )

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "startMs": self.start_ms, "endMs": self.end_ms}


@dataclass(frozen=True)
class ResourceBudget:
    id: str
    name: str
    pool_core_hours: Decimal
    pool_core_units: int
    memory_limit_mib: int
    round_time_budget_ms: int
    max_concurrent_actions_per_workload: int
    action_base_duration_ms: int
    action_duration_per_replica_ms: int
    clock_rollback_tolerance_ms: int
    failure_backoff_base_ms: int
    failure_backoff_max_ms: int
    failure_backoff_jitter_ratio: Decimal
    freeze_windows: tuple[FreezeWindow, ...] = field(default_factory=tuple)
    version: int = 1

    @classmethod
    def parse(cls, data: Any) -> "ResourceBudget":
        if not isinstance(data, dict):
            fail("E_SCHEMA", "resourceBudget must be an object")
        pool_hours = as_decimal(data.get("poolCoreHours"), "resourceBudget.poolCoreHours")
        pool_units = core_hours_to_units(pool_hours, "resourceBudget.poolCoreHours")
        if pool_units <= 0:
            fail(
                "E_INVALID_BUDGET",
                "poolCoreHours must be greater than zero",
                {"poolCoreHours": str(pool_hours)},
            )
        base = as_int(
            data.get("failureBackoffBaseMs"),
            "resourceBudget.failureBackoffBaseMs",
            minimum=1,
        )
        maximum = as_int(
            data.get("failureBackoffMaxMs"),
            "resourceBudget.failureBackoffMaxMs",
            minimum=1,
        )
        if maximum < base:
            fail(
                "E_INVALID_BACKOFF",
                "failureBackoffMaxMs must be at least failureBackoffBaseMs",
                {"baseMs": base, "maxMs": maximum},
            )
        jitter = as_decimal(
            data.get("failureBackoffJitterRatio", "0"),
            "resourceBudget.failureBackoffJitterRatio",
        )
        if jitter > 1:
            fail(
                "E_INVALID_BACKOFF",
                "failureBackoffJitterRatio must be between 0 and 1",
                {"jitterRatio": str(jitter)},
            )
        raw_windows = data.get("freezeWindows", [])
        if not isinstance(raw_windows, list):
            fail("E_SCHEMA", "resourceBudget.freezeWindows must be an array")
        windows: list[FreezeWindow] = []
        seen_window_ids: set[str] = set()
        for index, item in enumerate(raw_windows):
            if not isinstance(item, dict):
                fail(
                    "E_SCHEMA",
                    f"resourceBudget.freezeWindows[{index}] must be an object",
                )
            window = FreezeWindow.parse(item, index)
            if window.id in seen_window_ids:
                fail(
                    "E_DUPLICATE_ID",
                    "freeze window ids must be unique",
                    {"id": window.id},
                )
            seen_window_ids.add(window.id)
            windows.append(window)
        windows.sort(key=lambda item: (item.start_ms, item.end_ms, item.id))
        return cls(
            id=_required_string(data, "id", "resourceBudget"),
            name=_required_string(data, "name", "resourceBudget"),
            pool_core_hours=pool_hours,
            pool_core_units=pool_units,
            memory_limit_mib=as_int(
                data.get("memoryLimitMiB"),
                "resourceBudget.memoryLimitMiB",
                minimum=1,
            ),
            round_time_budget_ms=as_int(
                data.get("roundTimeBudgetMs"),
                "resourceBudget.roundTimeBudgetMs",
                minimum=1,
            ),
            max_concurrent_actions_per_workload=as_int(
                data.get("maxConcurrentActionsPerWorkload"),
                "resourceBudget.maxConcurrentActionsPerWorkload",
                minimum=1,
            ),
            action_base_duration_ms=as_int(
                data.get("actionBaseDurationMs"),
                "resourceBudget.actionBaseDurationMs",
                minimum=1,
            ),
            action_duration_per_replica_ms=as_int(
                data.get("actionDurationPerReplicaMs"),
                "resourceBudget.actionDurationPerReplicaMs",
                minimum=0,
            ),
            clock_rollback_tolerance_ms=as_int(
                data.get("clockRollbackToleranceMs"),
                "resourceBudget.clockRollbackToleranceMs",
                minimum=0,
            ),
            failure_backoff_base_ms=base,
            failure_backoff_max_ms=maximum,
            failure_backoff_jitter_ratio=jitter,
            freeze_windows=tuple(windows),
        )

    def to_json(self, *, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "poolCoreHours": str(self.pool_core_hours),
            "memoryLimitMiB": self.memory_limit_mib,
            "roundTimeBudgetMs": self.round_time_budget_ms,
            "maxConcurrentActionsPerWorkload": self.max_concurrent_actions_per_workload,
            "actionBaseDurationMs": self.action_base_duration_ms,
            "actionDurationPerReplicaMs": self.action_duration_per_replica_ms,
            "clockRollbackToleranceMs": self.clock_rollback_tolerance_ms,
            "failureBackoffBaseMs": self.failure_backoff_base_ms,
            "failureBackoffMaxMs": self.failure_backoff_max_ms,
            "failureBackoffJitterRatio": str(self.failure_backoff_jitter_ratio),
            "freezeWindows": [window.to_json() for window in self.freeze_windows],
        }
        if include_version:
            result["version"] = self.version
        return result

    def is_frozen(self, logical_now_ms: int) -> FreezeWindow | None:
        for window in self.freeze_windows:
            if window.start_ms <= logical_now_ms < window.end_ms:
                return window
        return None


@dataclass(frozen=True)
class MetricSample:
    metric_source_id: str
    timestamp_ms: int
    value: Decimal
    value_fixed: int

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "MetricSample":
        context = f"samples[{index}]"
        value = as_decimal(data.get("value"), f"{context}.value")
        return cls(
            metric_source_id=_required_string(data, "metricSourceId", context),
            timestamp_ms=as_int(
                data.get("timestampMs"),
                f"{context}.timestampMs",
                minimum=0,
            ),
            value=value,
            value_fixed=to_fixed(value, f"{context}.value", non_negative=True),
        )


@dataclass(frozen=True)
class ActionOutcomeRule:
    rule_id: str
    logical_now_ms: int
    outcome: str
    reason: str

    @classmethod
    def parse(cls, data: dict[str, Any], index: int = 0) -> "ActionOutcomeRule":
        context = f"rules[{index}]"
        outcome = _required_string(data, "outcome", context).lower()
        if outcome not in {"success", "failure"}:
            fail(
                "E_INVALID_OUTCOME",
                "action outcome must be 'success' or 'failure'",
                {"object": context, "outcome": outcome},
            )
        return cls(
            rule_id=_required_string(data, "ruleId", context),
            logical_now_ms=as_int(
                data.get("logicalNowMs"),
                f"{context}.logicalNowMs",
                minimum=0,
            ),
            outcome=outcome,
            reason=_optional_string(data, "reason", "simulated failure"),
        )


@dataclass(frozen=True)
class PolicyBundle:
    workloads: dict[str, Workload]
    metrics: dict[str, MetricSource]
    rules: dict[str, ScalingRule]
    budget: ResourceBudget

    def validate_cross_references(self) -> None:
        workload_names: dict[str, str] = {}
        for workload in self.workloads.values():
            other = workload_names.get(workload.name)
            if other is not None:
                fail(
                    "E_DUPLICATE_NAME",
                    "workload names must be unique",
                    {"name": workload.name, "ids": sorted([other, workload.id])},
                )
            workload_names[workload.name] = workload.id

        metric_names: dict[str, str] = {}
        for metric in self.metrics.values():
            other = metric_names.get(metric.name)
            if other is not None:
                fail(
                    "E_DUPLICATE_NAME",
                    "metric source names must be unique",
                    {"name": metric.name, "ids": sorted([other, metric.id])},
                )
            metric_names[metric.name] = metric.id

        rule_names: dict[str, str] = {}
        priorities: dict[int, str] = {}
        for rule in self.rules.values():
            other = rule_names.get(rule.name)
            if other is not None:
                fail(
                    "E_DUPLICATE_NAME",
                    "scaling rule names must be unique",
                    {"name": rule.name, "ids": sorted([other, rule.id])},
                )
            rule_names[rule.name] = rule.id
            if rule.priority in priorities:
                fail(
                    "E_DUPLICATE_PRIORITY",
                    "scaling rule priorities must be unique",
                    {
                        "priority": rule.priority,
                        "ruleIds": sorted([priorities[rule.priority], rule.id]),
                    },
                )
            priorities[rule.priority] = rule.id
            if rule.workload_id not in self.workloads:
                fail(
                    "E_MISSING_WORKLOAD_REF",
                    "scaling rule references a missing workload",
                    {"ruleId": rule.id, "workloadId": rule.workload_id},
                )
            if rule.metric_source_id not in self.metrics:
                fail(
                    "E_MISSING_METRIC_REF",
                    "scaling rule references a missing metric source",
                    {"ruleId": rule.id, "metricSourceId": rule.metric_source_id},
                )

    def to_json(self) -> dict[str, Any]:
        return {
            "workloads": [
                self.workloads[key].to_json()
                for key in sorted(self.workloads)
            ],
            "metricSources": [
                self.metrics[key].to_json()
                for key in sorted(self.metrics)
            ],
            "scalingRules": [
                self.rules[key].to_json()
                for key in sorted(self.rules)
            ],
            "resourceBudget": self.budget.to_json(),
        }


def parse_workloads(payload: Any, path: str = "workloads.json") -> dict[str, Workload]:
    result: dict[str, Workload] = {}
    for index, item in enumerate(_object_list(payload, top_key="workloads", path=path)):
        workload = Workload.parse(item, index)
        if workload.id in result:
            fail(
                "E_DUPLICATE_ID",
                "workload ids must be unique",
                {"id": workload.id},
            )
        result[workload.id] = workload
    return result


def parse_metrics(
    payload: Any,
    path: str = "metric_sources.json",
) -> dict[str, MetricSource]:
    result: dict[str, MetricSource] = {}
    for index, item in enumerate(
        _object_list(payload, top_key="metricSources", path=path)
    ):
        metric = MetricSource.parse(item, index)
        if metric.id in result:
            fail(
                "E_DUPLICATE_ID",
                "metric source ids must be unique",
                {"id": metric.id},
            )
        result[metric.id] = metric
    return result


def parse_rules(
    payload: Any,
    path: str = "scaling_rules.json",
) -> dict[str, ScalingRule]:
    result: dict[str, ScalingRule] = {}
    for index, item in enumerate(
        _object_list(payload, top_key="scalingRules", path=path)
    ):
        rule = ScalingRule.parse(item, index)
        if rule.id in result:
            fail(
                "E_DUPLICATE_ID",
                "scaling rule ids must be unique",
                {"id": rule.id},
            )
        result[rule.id] = rule
    return result


def parse_budget(payload: Any, path: str = "resource_budget.json") -> ResourceBudget:
    if not isinstance(payload, dict):
        fail("E_SCHEMA", f"{path} must contain a JSON object")
    return ResourceBudget.parse(payload.get("resourceBudget"))


def parse_samples(
    payload: Any,
    metrics: dict[str, MetricSource],
    path: str = "metric_stream.json",
) -> list[MetricSample]:
    samples: list[MetricSample] = []
    seen: dict[tuple[str, int], MetricSample] = {}
    for index, item in enumerate(
        _object_list(payload, top_key="samples", path=path)
    ):
        sample = MetricSample.parse(item, index)
        if sample.metric_source_id not in metrics:
            fail(
                "E_MISSING_METRIC_REF",
                "metric sample references a missing metric source",
                {"index": index, "metricSourceId": sample.metric_source_id},
            )
        key = (sample.metric_source_id, sample.timestamp_ms)
        existing = seen.get(key)
        if existing is not None and existing.value_fixed != sample.value_fixed:
            fail(
                "E_DUPLICATE_SAMPLE",
                "duplicate metric sample has conflicting values",
                {
                    "metricSourceId": sample.metric_source_id,
                    "timestampMs": sample.timestamp_ms,
                    "values": [str(existing.value), str(sample.value)],
                },
            )
        if existing is None:
            seen[key] = sample
            samples.append(sample)
    samples.sort(key=lambda item: (item.metric_source_id, item.timestamp_ms))
    return samples


def parse_outcomes(
    payload: Any,
    rules: dict[str, ScalingRule],
    path: str = "action_outcomes.json",
) -> list[ActionOutcomeRule]:
    if not isinstance(payload, dict):
        fail("E_SCHEMA", f"{path} must contain a JSON object")
    raw_rules = payload.get("rules", [])
    if not isinstance(raw_rules, list):
        fail("E_SCHEMA", f"{path}.rules must be an array")
    outcomes: list[ActionOutcomeRule] = []
    seen: set[tuple[str, int]] = set()
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            fail("E_SCHEMA", f"{path}.rules[{index}] must be an object")
        outcome = ActionOutcomeRule.parse(item, index)
        if outcome.rule_id not in rules:
            fail(
                "E_MISSING_RULE_REF",
                "action outcome references a missing scaling rule",
                {"ruleId": outcome.rule_id},
            )
        key = (outcome.rule_id, outcome.logical_now_ms)
        if key in seen:
            fail(
                "E_DUPLICATE_OUTCOME",
                "duplicate action outcome rule",
                {"ruleId": outcome.rule_id, "logicalNowMs": outcome.logical_now_ms},
            )
        seen.add(key)
        outcomes.append(outcome)
    outcomes.sort(key=lambda item: (item.logical_now_ms, item.rule_id))
    return outcomes


def parse_bundle(
    workloads_payload: Any,
    metrics_payload: Any,
    rules_payload: Any,
    budget_payload: Any,
) -> PolicyBundle:
    bundle = PolicyBundle(
        workloads=parse_workloads(workloads_payload),
        metrics=parse_metrics(metrics_payload),
        rules=parse_rules(rules_payload),
        budget=parse_budget(budget_payload),
    )
    duplicate_ids = (
        set(bundle.workloads) & set(bundle.metrics)
        | set(bundle.workloads) & set(bundle.rules)
        | set(bundle.metrics) & set(bundle.rules)
        | {bundle.budget.id} & (
            set(bundle.workloads) | set(bundle.metrics) | set(bundle.rules)
        )
    )
    if duplicate_ids:
        fail(
            "E_DUPLICATE_ID",
            "object ids must be globally unique",
            {"ids": sorted(duplicate_ids)},
        )
    bundle.validate_cross_references()
    return bundle


def with_version(obj: Any, version: int) -> Any:
    return replace(obj, version=version)


def sorted_objects(objects: dict[str, Any]) -> Iterable[Any]:
    for key in sorted(objects):
        yield objects[key]

