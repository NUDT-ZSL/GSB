"""Incremental policy patches and dependency impact analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .errors import INVALID_PATCH, ControlError
from .models import (
    MetricSource,
    PolicyBundle,
    ResourceBudget,
    ScaleRule,
    Workload,
)
from .util import jsonable


KINDS = {"workload", "metric", "rule", "budget"}
OPS = {"add", "update", "delete", "replace"}


@dataclass(frozen=True)
class PatchOperation:
    op: str
    kind: str
    name: str
    value: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "PatchOperation":
        if not isinstance(raw, Mapping):
            raise ControlError(
                INVALID_PATCH,
                f"patch operation {index} must be an object",
                {"index": index},
            )
        op = str(raw.get("op", "update")).lower()
        kind = str(raw.get("kind", "")).lower()
        name_raw = raw.get("name", "global" if kind == "budget" else None)
        if op not in OPS:
            raise ControlError(
                INVALID_PATCH,
                f"unsupported patch operation {op!r}",
                {"index": index, "op": op},
            )
        if kind not in KINDS:
            raise ControlError(
                INVALID_PATCH,
                f"unsupported object kind {kind!r}",
                {"index": index, "kind": kind},
            )
        if not isinstance(name_raw, str) or not name_raw:
            raise ControlError(
                INVALID_PATCH,
                "patch operation requires a non-empty name",
                {"index": index},
            )
        value = raw.get("value")
        if op != "delete" and not isinstance(value, Mapping):
            raise ControlError(
                INVALID_PATCH,
                f"patch {op} {kind} requires an object value",
                {"index": index, "op": op, "kind": kind},
            )
        return cls(op=op, kind=kind, name=name_raw, value=value)


@dataclass
class PolicyPatch:
    base_version: int | None
    operations: list[PatchOperation]

    @classmethod
    def from_payload(cls, payload: Any) -> "PolicyPatch":
        if not isinstance(payload, Mapping):
            raise ControlError(INVALID_PATCH, "patch must be a JSON object")
        raw_ops = payload.get("operations", payload.get("ops"))
        if not isinstance(raw_ops, list) or not raw_ops:
            raise ControlError(
                INVALID_PATCH,
                "patch.operations must be a non-empty array",
            )
        base = payload.get("base_version", payload.get("expected_version"))
        if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
            raise ControlError(INVALID_PATCH, "patch.base_version must be an integer")
        return cls(
            base_version=base,
            operations=[
                PatchOperation.from_dict(raw, index)
                for index, raw in enumerate(raw_ops)
            ],
        )


@dataclass
class PatchResult:
    bundle: PolicyBundle
    impact: dict[str, Any]
    changed_objects: list[dict[str, str]] = field(default_factory=list)


def _workload_dict(item: Workload) -> dict[str, Any]:
    return item.to_dict()


def _metric_dict(item: MetricSource) -> dict[str, Any]:
    return item.to_dict()


def _rule_dict(item: ScaleRule) -> dict[str, Any]:
    return item.to_dict()


def _merge(existing: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(change)
    return merged


def apply_patch_to_bundle(bundle: PolicyBundle, patch: PolicyPatch) -> PatchResult:
    """Apply and validate a patch in memory.

    The caller only persists the result after this function returns.  Therefore
    a failed reference, name or cycle check cannot leave a partial policy.
    """

    workloads = {name: item.to_dict() for name, item in bundle.workloads.items()}
    metrics = {name: item.to_dict() for name, item in bundle.metrics.items()}
    rules = {name: item.to_dict() for name, item in bundle.rules.items()}
    budget = None if bundle.budget is None else bundle.budget.to_dict()

    changed: list[dict[str, str]] = []
    for operation in patch.operations:
        if operation.kind == "budget":
            if operation.op == "delete":
                raise ControlError(
                    INVALID_PATCH,
                    "the resource budget cannot be deleted",
                    {"name": operation.name},
                )
            if operation.op == "add" and budget is not None:
                raise ControlError(
                    INVALID_PATCH,
                    "budget already exists; use update or replace",
                    {"name": operation.name},
                )
            if operation.op in {"update", "replace"} and budget is None:
                raise ControlError(
                    INVALID_PATCH,
                    "budget does not exist; use add",
                    {"name": operation.name},
                )
            assert operation.value is not None
            if operation.op == "update" and budget is not None:
                budget = _merge(budget, operation.value)
            else:
                budget = dict(operation.value)
            changed.append({"kind": "budget", "name": operation.name, "op": operation.op})
            continue

        target = {
            "workload": workloads,
            "metric": metrics,
            "rule": rules,
        }[operation.kind]
        exists = operation.name in target
        if operation.op == "add" and exists:
            raise ControlError(
                INVALID_PATCH,
                f"{operation.kind} {operation.name!r} already exists",
                {"kind": operation.kind, "name": operation.name},
            )
        if operation.op in {"update", "replace"} and not exists:
            raise ControlError(
                INVALID_PATCH,
                f"{operation.kind} {operation.name!r} does not exist",
                {"kind": operation.kind, "name": operation.name},
            )
        if operation.op == "delete":
            if not exists:
                raise ControlError(
                    INVALID_PATCH,
                    f"{operation.kind} {operation.name!r} does not exist",
                    {"kind": operation.kind, "name": operation.name},
                )
            del target[operation.name]
            changed.append(
                {"kind": operation.kind, "name": operation.name, "op": operation.op}
            )
            continue

        assert operation.value is not None
        value = dict(operation.value)
        if operation.kind == "rule" and exists and operation.op == "update":
            existing_rule = rules[operation.name]
            value = _merge(existing_rule, value)
            next_version = max(
                int(existing_rule.get("version", 0)) + 1,
                int(value.get("version", 0)),
            )
            value["version"] = next_version
        elif operation.kind == "rule" and operation.op == "add":
            value.setdefault("version", 1)
        if "name" in value and value["name"] != operation.name:
            raise ControlError(
                INVALID_PATCH,
                "renaming objects through patch is not supported",
                {
                    "kind": operation.kind,
                    "name": operation.name,
                    "new_name": value["name"],
                },
            )
        value["name"] = operation.name
        if operation.op == "update" and exists:
            value = _merge(target[operation.name], value)
            value["name"] = operation.name
        target[operation.name] = value
        changed.append(
            {"kind": operation.kind, "name": operation.name, "op": operation.op}
        )

    payload = {
        "workloads": [workloads[name] for name in sorted(workloads)],
        "metrics": [metrics[name] for name in sorted(metrics)],
        "rules": [rules[name] for name in sorted(rules)],
        "budget": budget,
    }
    new_bundle = PolicyBundle.from_payload(jsonable(payload))
    impact = analyse_impact(bundle, new_bundle, changed)
    return PatchResult(bundle=new_bundle, impact=impact, changed_objects=changed)


def analyse_impact(
    old: PolicyBundle,
    new: PolicyBundle,
    changed: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    changed_list = list(changed)
    changed_keys = {(item["kind"], item["name"]) for item in changed_list}
    reasons: dict[str, set[str]] = {name: set() for name in new.workloads}

    # Compare effective object content, not merely the patch's intent.  This
    # makes a no-op update safe and keeps the impact report precise.
    old_workloads = {name: item.to_dict() for name, item in old.workloads.items()}
    new_workloads = {name: item.to_dict() for name, item in new.workloads.items()}
    old_metrics = {name: item.to_dict() for name, item in old.metrics.items()}
    new_metrics = {name: item.to_dict() for name, item in new.metrics.items()}
    old_rules = {name: item.to_dict() for name, item in old.rules.items()}
    new_rules = {name: item.to_dict() for name, item in new.rules.items()}
    old_budget = None if old.budget is None else old.budget.to_dict()
    new_budget = None if new.budget is None else new.budget.to_dict()

    changed_workloads = {
        name
        for name in set(old_workloads) | set(new_workloads)
        if old_workloads.get(name) != new_workloads.get(name)
    }
    changed_metrics = {
        name
        for name in set(old_metrics) | set(new_metrics)
        if old_metrics.get(name) != new_metrics.get(name)
    }
    changed_rules = {
        name
        for name in set(old_rules) | set(new_rules)
        if old_rules.get(name) != new_rules.get(name)
    }
    budget_changed = old_budget != new_budget

    for name in changed_workloads:
        if name in reasons:
            reasons[name].add(f"workload:{name} changed")
    for name in changed_metrics:
        old_refs = {
            rule.workload for rule in old.rules.values() if rule.metric == name
        }
        new_refs = {
            rule.workload for rule in new.rules.values() if rule.metric == name
        }
        for workload in old_refs | new_refs:
            if workload in reasons:
                reasons[workload].add(f"metric:{name} changed")
    for name in changed_rules:
        old_target = (
            old_rules.get(name, {}).get("workload") if name in old_rules else None
        )
        new_target = (
            new_rules.get(name, {}).get("workload") if name in new_rules else None
        )
        for workload in {old_target, new_target}:
            if workload in reasons:
                reasons[workload].add(f"rule:{name} changed")
    if budget_changed:
        for workload in reasons:
            reasons[workload].add("budget:global changed")

    affected: dict[str, Any] = {}
    reused: list[str] = []
    for workload_name in sorted(new.workloads):
        reason_set = reasons.get(workload_name, set())
        if not reason_set:
            reused.append(workload_name)
            continue
        workload_changed = f"workload:{workload_name} changed" in reason_set
        rule_changed = any(item.startswith("rule:") for item in reason_set)
        metric_changed = any(item.startswith("metric:") for item in reason_set)
        only_budget = reason_set == {"budget:global changed"}
        affected[workload_name] = {
            "reasons": sorted(reason_set),
            "recalculate": not only_budget,
            "recalculate_budget": True,
            "invalidate_target_cache": not only_budget,
            "invalidate_cooldown_timer": workload_changed or rule_changed,
            "invalidate_freeze_timer": workload_changed or rule_changed,
            "invalidate_budget_reservation": True,
            "invalidate_precision_ledger": workload_changed or rule_changed or metric_changed,
            "invalidate_precision_flags": workload_changed or rule_changed or metric_changed,
            "invalidate_metric_residual_ledger": metric_changed,
            "reuse_committed_samples": True,
            "reuse_sample_watermark": not metric_changed,
            "reason": "budget_only" if only_budget else "policy_dependency",
        }
    deleted = sorted(set(old.workloads) - set(new.workloads))
    all_known = sorted(set(old.workloads) | set(new.workloads))
    return {
        "changed_objects": changed_list,
        "changed_workloads": sorted(changed_workloads),
        "changed_metrics": sorted(changed_metrics),
        "changed_rules": sorted(changed_rules),
        "budget_changed": budget_changed,
        "affected_workloads": affected,
        "reused_workloads": reused,
        "deleted_workloads": deleted,
        "workload_closure": all_known,
        "reuse_guarantee": (
            "unaffected workload rows, target cache, timers and precision flags "
            "are not updated by the patch transaction"
        ),
    }
