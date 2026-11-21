from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from .errors import fail
from .fixed import as_int
from .model import (
    MetricSource,
    PolicyBundle,
    ResourceBudget,
    ScalingRule,
    Workload,
)

COOLDOWN_AFFECTING_RULE_FIELDS = {
    "workloadId",
    "metricSourceId",
    "direction",
    "cooldownMs",
}
BUDGET_AFFECTING_WORKLOAD_FIELDS = {
    "cpuMillicoresPerReplica",
    "memoryMiBPerReplica",
    "minReplicas",
    "maxReplicas",
    "currentReplicas",
}
BUDGET_AFFECTING_RULE_FIELDS = {
    "workloadId",
    "metricSourceId",
    "direction",
    "step",
    "percent",
    "priority",
}


@dataclass(frozen=True)
class PatchOperation:
    operation_id: str
    op: str
    object_type: str
    object_id: str | None
    changes: dict[str, Any]
    object_payload: dict[str, Any] | None
    expected_version: int | None


@dataclass(frozen=True)
class PatchDocument:
    patch_id: str
    base_config_version: int | None
    operations: tuple[PatchOperation, ...]


@dataclass
class PatchApplication:
    bundle: PolicyBundle
    changed_nodes: list[tuple[str, str]]
    rule_field_changes: dict[str, set[str]]
    added_workloads: list[str]
    deleted_workloads: list[str]
    budget_changed: bool
    workload_current_changes: dict[str, int]
    added_rules: list[str]
    deleted_rules: list[str]


def parse_patch_document(payload: Any) -> PatchDocument:
    if not isinstance(payload, dict):
        fail("E_INVALID_PATCH", "patch document must be a JSON object")
    patch_id = payload.get("patchId") or payload.get("id")
    if not isinstance(patch_id, str) or not patch_id.strip():
        fail("E_INVALID_PATCH", "patch document requires a non-empty patchId")
    raw_operations = payload.get("patches")
    if not isinstance(raw_operations, list) or not raw_operations:
        fail("E_INVALID_PATCH", "patch document requires a non-empty patches array")
    base_version = payload.get("baseConfigVersion")
    if base_version is not None:
        base_version = as_int(
            base_version,
            "baseConfigVersion",
            minimum=1,
        )
    operations: list[PatchOperation] = []
    seen_operations: set[str] = set()
    for index, raw in enumerate(raw_operations):
        context = f"patches[{index}]"
        if not isinstance(raw, dict):
            fail("E_INVALID_PATCH", f"{context} must be an object")
        operation_id = raw.get("operationId", raw.get("patchId"))
        if not isinstance(operation_id, str) or not operation_id.strip():
            fail(
                "E_INVALID_PATCH",
                f"{context} requires a non-empty operationId",
            )
        if operation_id in seen_operations:
            fail(
                "E_INVALID_PATCH",
                "patch operation ids must be unique",
                {"operationId": operation_id},
            )
        seen_operations.add(operation_id)
        op = raw.get("op")
        if op not in {"add", "modify", "delete"}:
            fail(
                "E_INVALID_PATCH",
                f"{context}.op must be add, modify, or delete",
                {"operationId": operation_id, "op": op},
            )
        object_type = raw.get("objectType")
        if object_type not in {
            "workload",
            "metric_source",
            "rule",
            "resource_budget",
        }:
            fail(
                "E_INVALID_PATCH",
                f"{context}.objectType is not supported",
                {"operationId": operation_id, "objectType": object_type},
            )
        changes = raw.get("changes", {})
        if not isinstance(changes, dict):
            fail(
                "E_INVALID_PATCH",
                f"{context}.changes must be an object",
                {"operationId": operation_id},
            )
        object_payload = raw.get("object")
        if object_payload is not None and not isinstance(object_payload, dict):
            fail(
                "E_INVALID_PATCH",
                f"{context}.object must be an object",
                {"operationId": operation_id},
            )
        object_id = raw.get("objectId")
        if object_id is None and isinstance(object_payload, dict):
            object_id = object_payload.get("id")
        if object_id is not None and not isinstance(object_id, str):
            fail(
                "E_INVALID_PATCH",
                f"{context}.objectId must be a string",
                {"operationId": operation_id},
            )
        if op in {"modify", "delete"} and not object_id:
            fail(
                "E_INVALID_PATCH",
                f"{context} requires objectId",
                {"operationId": operation_id},
            )
        if op == "add" and object_payload is None:
            fail(
                "E_INVALID_PATCH",
                f"{context} add operation requires object",
                {"operationId": operation_id},
            )
        if op == "modify" and not changes and object_type != "resource_budget":
            fail(
                "E_INVALID_PATCH",
                f"{context} modify operation requires changes",
                {"operationId": operation_id},
            )
        expected_version = raw.get("expectedVersion")
        if expected_version is not None:
            expected_version = as_int(
                expected_version,
                f"{context}.expectedVersion",
                minimum=1,
            )
        operations.append(
            PatchOperation(
                operation_id=operation_id,
                op=op,
                object_type=object_type,
                object_id=object_id,
                changes=dict(changes),
                object_payload=None if object_payload is None else dict(object_payload),
                expected_version=expected_version,
            )
        )
    return PatchDocument(
        patch_id=patch_id.strip(),
        base_config_version=base_version,
        operations=tuple(operations),
    )


def _with_version(obj: Any, version: int) -> Any:
    return replace(obj, version=version)


def _check_expected(obj: Any, operation: PatchOperation) -> None:
    if (
        operation.expected_version is not None
        and int(getattr(obj, "version")) != operation.expected_version
    ):
        fail(
            "E_VERSION_CONFLICT",
            "patch expectedVersion does not match the current object version",
            {
                "operationId": operation.operation_id,
                "objectType": operation.object_type,
                "objectId": operation.object_id,
                "expectedVersion": operation.expected_version,
                "actualVersion": getattr(obj, "version"),
            },
        )


def _object_from_dict(
    object_type: str,
    data: dict[str, Any],
    *,
    version: int | None = None,
) -> Any:
    if object_type == "workload":
        parsed = Workload.parse(data)
    elif object_type == "metric_source":
        parsed = MetricSource.parse(data)
    elif object_type == "rule":
        parsed = ScalingRule.parse(data)
    elif object_type == "resource_budget":
        parsed = ResourceBudget.parse(data)
    else:
        fail(
            "E_INVALID_PATCH",
            "unsupported patch object type",
            {"objectType": object_type},
        )
    if version is not None:
        parsed = _with_version(parsed, version)
    return parsed


def apply_patch_in_memory(
    bundle: PolicyBundle,
    document: PatchDocument,
) -> PatchApplication:
    workloads = dict(bundle.workloads)
    metrics = dict(bundle.metrics)
    rules = dict(bundle.rules)
    budget = bundle.budget
    changed_nodes: list[tuple[str, str]] = []
    rule_field_changes: dict[str, set[str]] = {}
    added_workloads: list[str] = []
    deleted_workloads: list[str] = []
    workload_current_changes: dict[str, int] = {}
    added_rules: list[str] = []
    deleted_rules: list[str] = []
    budget_changed = False

    def object_map(object_type: str) -> dict[str, Any]:
        if object_type == "workload":
            return workloads
        if object_type == "metric_source":
            return metrics
        if object_type == "rule":
            return rules
        fail("E_INVALID_PATCH", "resource_budget does not support this operation")

    for operation in document.operations:
        object_type = operation.object_type
        object_id = operation.object_id
        if object_type == "resource_budget":
            if operation.op != "modify":
                fail(
                    "E_INVALID_PATCH",
                    "resource_budget supports modify only",
                    {"operationId": operation.operation_id},
                )
            if object_id is not None and object_id != budget.id:
                fail(
                    "E_INVALID_PATCH",
                    "resource_budget objectId does not match the active budget",
                    {
                        "operationId": operation.operation_id,
                        "objectId": object_id,
                        "budgetId": budget.id,
                    },
                )
            if "id" in operation.changes and operation.changes["id"] != budget.id:
                fail(
                    "E_INVALID_PATCH",
                    "modify operations may not change the budget id",
                    {"operationId": operation.operation_id},
                )
            _check_expected(budget, operation)
            data = budget.to_json(include_version=False)
            data.update(operation.changes)
            budget = _object_from_dict(
                "resource_budget",
                data,
                version=budget.version + 1,
            )
            changed_nodes.append(("resource_budget", budget.id))
            budget_changed = True
            continue

        objects = object_map(object_type)
        assert object_id is not None
        if operation.op == "add":
            if object_id in objects:
                fail(
                    "E_PATCH_DUPLICATE",
                    "patch add targets an existing object",
                    {
                        "operationId": operation.operation_id,
                        "objectType": object_type,
                        "objectId": object_id,
                    },
                )
            assert operation.object_payload is not None
            data = dict(operation.object_payload)
            if "id" not in data:
                data["id"] = object_id
            if data.get("id") != object_id:
                fail(
                    "E_INVALID_PATCH",
                    "patch add object id does not match objectId",
                    {"operationId": operation.operation_id},
                )
            parsed = _object_from_dict(object_type, data, version=1)
            objects[object_id] = parsed
            changed_nodes.append((object_type, object_id))
            if object_type == "workload":
                added_workloads.append(object_id)
                workload_current_changes[object_id] = parsed.current_replicas
            elif object_type == "rule":
                added_rules.append(object_id)
                rule_field_changes[object_id] = {"*"}
            continue

        if object_id not in objects:
            fail(
                "E_PATCH_OBJECT_NOT_FOUND",
                "patch references a missing object",
                {
                    "operationId": operation.operation_id,
                    "objectType": object_type,
                    "objectId": object_id,
                },
            )
        current = objects[object_id]
        _check_expected(current, operation)
        if operation.op == "delete":
            del objects[object_id]
            changed_nodes.append((object_type, object_id))
            if object_type == "workload":
                deleted_workloads.append(object_id)
            elif object_type == "rule":
                deleted_rules.append(object_id)
                rule_field_changes[object_id] = {"*"}
            continue

        if "id" in operation.changes and operation.changes["id"] != object_id:
            fail(
                "E_INVALID_PATCH",
                "modify operations may not change object ids",
                {
                    "operationId": operation.operation_id,
                    "objectId": object_id,
                },
            )
        data = current.to_json(include_version=False)
        data.update(operation.changes)
        updated = _object_from_dict(
            object_type,
            data,
            version=current.version + 1,
        )
        objects[object_id] = updated
        changed_nodes.append((object_type, object_id))
        if object_type == "workload" and "currentReplicas" in operation.changes:
            workload_current_changes[object_id] = updated.current_replicas
        if object_type == "rule":
            rule_field_changes.setdefault(object_id, set()).update(
                operation.changes.keys()
            )

    new_bundle = PolicyBundle(
        workloads=workloads,
        metrics=metrics,
        rules=rules,
        budget=budget,
    )
    new_bundle.validate_cross_references()
    duplicate_ids = (
        set(workloads) & set(metrics)
        | set(workloads) & set(rules)
        | set(metrics) & set(rules)
        | {new_bundle.budget.id} & (
            set(workloads) | set(metrics) | set(rules)
        )
    )
    if duplicate_ids:
        fail(
            "E_DUPLICATE_ID",
            "object ids must be globally unique",
            {"ids": sorted(duplicate_ids)},
        )
    return PatchApplication(
        bundle=new_bundle,
        changed_nodes=changed_nodes,
        rule_field_changes=rule_field_changes,
        added_workloads=added_workloads,
        deleted_workloads=deleted_workloads,
        budget_changed=budget_changed,
        workload_current_changes=workload_current_changes,
        added_rules=added_rules,
        deleted_rules=deleted_rules,
    )


def dependency_edges(bundle: PolicyBundle) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}

    def add(source: str, target: str) -> None:
        edges.setdefault(source, set()).add(target)
        edges.setdefault(target, set())

    for rule in bundle.rules.values():
        add(f"metric_source:{rule.metric_source_id}", f"rule:{rule.id}")
        add(f"rule:{rule.id}", f"workload:{rule.workload_id}")
    for workload_id in bundle.workloads:
        add(f"resource_budget:{bundle.budget.id}", f"workload:{workload_id}")
        edges.setdefault(f"workload:{workload_id}", set())
    return edges


def affected_workload_closures(
    old_bundle: PolicyBundle,
    new_bundle: PolicyBundle,
    changed_nodes: Iterable[tuple[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    edges = dependency_edges(old_bundle)
    for source, targets in dependency_edges(new_bundle).items():
        edges.setdefault(source, set()).update(targets)
    closures: dict[str, list[dict[str, Any]]] = {}
    for object_type, object_id in sorted(set(changed_nodes)):
        source = f"{object_type}:{object_id}"
        queue: list[list[str]] = [[source]]
        visited: set[str] = {source}
        while queue:
            path = queue.pop(0)
            current = path[-1]
            if current.startswith("workload:"):
                workload_id = current.split(":", 1)[1]
                chain = list(path)
                existing = closures.setdefault(workload_id, [])
                marker = (object_type, object_id, tuple(chain))
                if not any(
                    (entry["objectType"], entry["objectId"], tuple(entry["chain"]))
                    == marker
                    for entry in existing
                ):
                    existing.append(
                        {
                            "objectType": object_type,
                            "objectId": object_id,
                            "chain": chain,
                        }
                    )
                continue
            for target in sorted(edges.get(current, set())):
                if target in visited:
                    continue
                visited.add(target)
                queue.append(path + [target])
    for workload_id in closures:
        closures[workload_id].sort(
            key=lambda entry: (
                entry["objectType"],
                entry["objectId"],
                tuple(entry["chain"]),
            )
        )
    return closures


def build_invalidation_report(
    old_bundle: PolicyBundle,
    application: PatchApplication,
    *,
    before_config_version: int,
    after_config_version: int,
) -> dict[str, Any]:
    new_bundle = application.bundle
    closures = affected_workload_closures(
        old_bundle,
        new_bundle,
        application.changed_nodes,
    )
    existing_affected = sorted(
        workload_id
        for workload_id in closures
        if workload_id in new_bundle.workloads
    )
    unaffected = sorted(
        workload_id
        for workload_id in new_bundle.workloads
        if workload_id not in closures
    )
    cooldown_invalidated: list[dict[str, Any]] = []
    for rule_id in sorted(application.rule_field_changes):
        fields = application.rule_field_changes[rule_id]
        if "*" in fields or fields & COOLDOWN_AFFECTING_RULE_FIELDS:
            cooldown_invalidated.append(
                {
                    "ruleId": rule_id,
                    "reason": "rule_identity_or_cooldown_changed"
                    if rule_id in old_bundle.rules and rule_id in new_bundle.rules
                    else "rule_added_or_deleted",
                }
            )

    cost_affected_workloads: set[str] = set()
    if application.budget_changed:
        cost_affected_workloads.update(existing_affected)
    for workload_id, before in old_bundle.workloads.items():
        after = new_bundle.workloads.get(workload_id)
        if after is None:
            continue
        if before.to_json(include_version=False) != after.to_json(include_version=False):
            changed_fields = {
                key
                for key, value in after.to_json(include_version=False).items()
                if before.to_json(include_version=False).get(key) != value
            }
            if changed_fields & BUDGET_AFFECTING_WORKLOAD_FIELDS:
                cost_affected_workloads.add(workload_id)
    for rule_id, fields in application.rule_field_changes.items():
        old_rule = old_bundle.rules.get(rule_id)
        new_rule = new_bundle.rules.get(rule_id)
        if "*" in fields or fields & BUDGET_AFFECTING_RULE_FIELDS:
            for rule in (old_rule, new_rule):
                if rule is not None:
                    cost_affected_workloads.add(rule.workload_id)
    if application.budget_changed:
        cost_affected_workloads.update(new_bundle.workloads)

    return {
        "patchId": None,
        "beforeConfigVersion": before_config_version,
        "afterConfigVersion": after_config_version,
        "affectedWorkloads": [
            {
                "workloadId": workload_id,
                "triggers": closures.get(workload_id, []),
                "targetReplicaCacheInvalidated": True,
                "cooldownTimersInvalidated": any(
                    entry["ruleId"] in {
                        rule.id
                        for rule in new_bundle.rules.values()
                        if rule.workload_id == workload_id
                    }
                    or (
                        entry["ruleId"] in old_bundle.rules
                        and old_bundle.rules[entry["ruleId"]].workload_id
                        == workload_id
                    )
                    for entry in cooldown_invalidated
                ),
                "budgetPrechargeInvalidated": (
                    workload_id in cost_affected_workloads
                ),
            }
            for workload_id in existing_affected
        ],
        "unaffectedWorkloads": unaffected,
        "deletedWorkloads": sorted(application.deleted_workloads),
        "addedWorkloads": sorted(application.added_workloads),
        "cooldownInvalidations": cooldown_invalidated,
        "budgetPrechargeInvalidatedWorkloads": sorted(cost_affected_workloads),
    }

