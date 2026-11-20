from __future__ import annotations

import hashlib
import heapq
from collections import Counter, defaultdict
from typing import Any, Iterable

from .jsonutil import canonical_json
from .model import KIND_ORDER, EngineConfig, Resource
from .validation import selector_matches

ACTION_ORDER = {"create": 0, "update": 1, "delete": 2, "noop": 3}


def _operation_id(
    action: str,
    resource: Resource,
    target_body: dict[str, Any] | None,
    generation: int,
) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "action": action,
                "identity": resource.identity,
                "target": target_body,
                "generation": generation,
            }
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"op-{digest}"


def _resource_by_identity(
    resources: Iterable[Resource],
) -> dict[tuple[str, str, str], Resource]:
    return {
        (resource.kind, resource.namespace, resource.name): resource
        for resource in resources
    }


def _drift_groups(
    drifts: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for drift in drifts:
        identity = drift["identity"]
        groups[
            (identity["kind"], identity["namespace"], identity["name"])
        ].append(drift)
    return groups


def _reason_for_drifts(drifts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": drift["type"],
            "field_path": drift["field_path"],
            "expected": drift["expected"],
            "actual": drift["actual"],
            "basis": drift["basis"],
        }
        for drift in sorted(
            drifts,
            key=lambda item: (item["field_path"], item["type"]),
        )
    ]


def generate_plan(
    desired: dict[str, Resource],
    actual: dict[str, Resource],
    drifts: list[dict[str, Any]],
    config: EngineConfig,
    generations: dict[str, int] | None = None,
) -> dict[str, Any]:
    generations = generations or {}
    groups = _drift_groups(drifts)
    all_keys = sorted(
        set(desired) | set(actual),
        key=lambda key: (
            key.split("/", 2)[1],
            KIND_ORDER.get(key.split("/", 2)[0], 99),
            key.split("/", 2)[2],
            key,
        ),
    )
    desired_by_id = _resource_by_identity(desired.values())
    actual_by_id = _resource_by_identity(actual.values())
    operations: list[dict[str, Any]] = []
    op_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}

    for key in all_keys:
        kind, namespace, name = key.split("/", 2)
        identity_key = (kind, namespace, name)
        resource_drifts = groups.get(identity_key, [])
        expected = desired.get(key)
        observed = actual.get(key)
        if expected is None:
            action = "delete"
            source = observed
            target_body = observed.body if observed else None
        elif observed is None:
            action = "create"
            source = expected
            target_body = expected.body
        elif resource_drifts:
            action = "update"
            source = expected
            target_body = expected.body
        else:
            action = "noop"
            source = expected
            target_body = expected.body
        assert source is not None
        operation_id = _operation_id(
            action,
            source,
            target_body,
            int(generations.get(source.key, 0)),
        )
        operation = {
            "operation_id": operation_id,
            "action": action,
            "semantic_action": "maintain" if action == "noop" else action,
            "identity": source.identity,
            "resource_key": source.key,
            "exclusive_key": source.key,
            "depends_on": [],
            "conflicts_with": [],
            "payload": (
                {"object": target_body}
                if action != "delete"
                else {"expected_absence": True}
            ),
            "rationale": _reason_for_drifts(resource_drifts),
        }
        operations.append(operation)
        op_by_identity[identity_key] = operation

    op_by_id = {operation["operation_id"]: operation for operation in operations}

    def add_dependency(
        operation: dict[str, Any], prerequisite: dict[str, Any]
    ) -> None:
        if operation["operation_id"] == prerequisite["operation_id"]:
            return
        if prerequisite["operation_id"] not in operation["depends_on"]:
            operation["depends_on"].append(prerequisite["operation_id"])
        if operation["operation_id"] not in prerequisite["conflicts_with"]:
            prerequisite["conflicts_with"].append(
                operation["operation_id"]
            )
        if prerequisite["operation_id"] not in operation["conflicts_with"]:
            operation["conflicts_with"].append(
                prerequisite["operation_id"]
            )

    reference_checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    # Create/update order: ConfigMaps referenced by Deployments, then
    # Deployments selected by Services.
    for operation in operations:
        if operation["action"] not in ("create", "update"):
            continue
        identity = operation["identity"]
        desired_resource = desired_by_id[
            (identity["kind"], identity["namespace"], identity["name"])
        ]
        if identity["kind"] == "Deployment":
            for config_map_name in desired_resource.body.get(
                "configMapRefs", []
            ):
                referenced = op_by_identity.get(
                    ("ConfigMap", identity["namespace"], config_map_name)
                )
                if referenced is None:
                    violations.append(
                        {
                            "code": "E_PLAN_MISSING_REF",
                            "operation_id": operation["operation_id"],
                            "message": (
                                "desired Deployment references a ConfigMap "
                                "that has no planned or existing object: "
                                f"{identity['namespace']}/{config_map_name}"
                            ),
                        }
                    )
                elif referenced["action"] in ("create", "update"):
                    add_dependency(operation, referenced)
        elif identity["kind"] == "Service":
            selector = desired_resource.body["selector"]
            for deployment in sorted(
                desired.values(), key=lambda item: item.sort_key
            ):
                if (
                    deployment.kind == "Deployment"
                    and deployment.namespace == identity["namespace"]
                    and selector_matches(
                        selector, deployment.body.get("labels", {})
                    )
                ):
                    referenced = op_by_identity[
                        ("Deployment", deployment.namespace, deployment.name)
                    ]
                    if referenced["action"] in ("create", "update"):
                        add_dependency(operation, referenced)

    # Deletion order: remove live references first. Every reference must
    # either be scheduled for deletion or scheduled to update away from the
    # target. Otherwise the plan is rejected and never submitted.
    for operation in operations:
        if operation["action"] != "delete":
            continue
        identity = operation["identity"]
        if identity["kind"] == "ConfigMap":
            live_references: list[str] = []
            for deployment in sorted(
                actual.values(), key=lambda item: item.sort_key
            ):
                if (
                    deployment.kind != "Deployment"
                    or deployment.namespace != identity["namespace"]
                    or identity["name"]
                    not in deployment.body.get("configMapRefs", [])
                ):
                    continue
                reference_key = (
                    "Deployment",
                    deployment.namespace,
                    deployment.name,
                )
                reference_op = op_by_identity[reference_key]
                resolution = _deletion_reference_resolution(
                    reference_op=reference_op,
                    desired_by_id=desired_by_id,
                    reference_kind="Deployment",
                    target_name=identity["name"],
                )
                if resolution == "scheduled_delete":
                    add_dependency(operation, reference_op)
                elif resolution == "scheduled_update":
                    add_dependency(operation, reference_op)
                else:
                    violations.append(
                        {
                            "code": "E_UNSAFE_DELETE",
                            "operation_id": operation["operation_id"],
                            "message": (
                                "ConfigMap deletion still has a live "
                                f"Deployment reference: {deployment.key}"
                            ),
                        }
                    )
                live_references.append(
                    {
                        "resource": deployment.key,
                        "operation": reference_op["operation_id"],
                        "resolution": resolution,
                    }
                )
            reference_checks.append(
                {
                    "target": operation["resource_key"],
                    "references": live_references,
                    "safe": not any(
                        item["resolution"] == "unresolved"
                        for item in live_references
                    ),
                }
            )
        elif identity["kind"] == "Deployment":
            observed_deployment = actual_by_id[
                ("Deployment", identity["namespace"], identity["name"])
            ]
            live_references = []
            for service in sorted(
                actual.values(), key=lambda item: item.sort_key
            ):
                if (
                    service.kind != "Service"
                    or service.namespace != identity["namespace"]
                    or not selector_matches(
                        service.body["selector"],
                        observed_deployment.body.get("labels", {}),
                    )
                ):
                    continue
                reference_key = (
                    "Service",
                    service.namespace,
                    service.name,
                )
                reference_op = op_by_identity[reference_key]
                resolution = _deletion_reference_resolution(
                    reference_op=reference_op,
                    desired_by_id=desired_by_id,
                    reference_kind="Service",
                    target_name=identity["name"],
                    observed_deployment=observed_deployment,
                )
                if resolution in ("scheduled_delete", "scheduled_update"):
                    add_dependency(operation, reference_op)
                else:
                    violations.append(
                        {
                            "code": "E_UNSAFE_DELETE",
                            "operation_id": operation["operation_id"],
                            "message": (
                                "Deployment deletion still has a live Service "
                                f"selector reference: {service.key}"
                            ),
                        }
                    )
                live_references.append(
                    {
                        "resource": service.key,
                        "operation": reference_op["operation_id"],
                        "resolution": resolution,
                    }
                )
            reference_checks.append(
                {
                    "target": operation["resource_key"],
                    "references": live_references,
                    "safe": not any(
                        item["resolution"] == "unresolved"
                        for item in live_references
                    ),
                }
            )

    exclusive = Counter(
        operation["exclusive_key"] for operation in operations
    )
    for operation in operations:
        if exclusive[operation["exclusive_key"]] > 1:
            violations.append(
                {
                    "code": "E_PLAN_MUTEX",
                    "operation_id": operation["operation_id"],
                    "message": (
                        "multiple planned mutations share exclusive key: "
                        f"{operation['exclusive_key']}"
                    ),
                }
            )
        operation["depends_on"] = sorted(operation["depends_on"])
        operation["conflicts_with"] = sorted(operation["conflicts_with"])

    order, cycle_nodes = _topological_order(operations)
    if cycle_nodes:
        violations.append(
            {
                "code": "E_PLAN_CYCLE",
                "operation_ids": cycle_nodes,
                "message": "operation dependency graph contains a cycle",
            }
        )

    violations.sort(key=lambda item: canonical_json(item))
    return {
        "valid": not violations,
        "operations": operations,
        "topological_order": order,
        "constraints": {
            "dependency_edges": [
                {
                    "before": dependency,
                    "after": operation["operation_id"],
                }
                for operation in operations
                for dependency in operation["depends_on"]
            ],
            "mutual_exclusion_pairs": [
                {
                    "operation_ids": [left, right],
                }
                for left, right in sorted(
                    {
                        tuple(
                            sorted((operation_id, conflict_id))
                        )
                        for operation in operations
                        for operation_id in [operation["operation_id"]]
                        for conflict_id in operation["conflicts_with"]
                    }
                )
            ],
            "mutex_groups": [
                {
                    "exclusive_key": key,
                    "operation_ids": [
                        operation["operation_id"]
                        for operation in operations
                        if operation["exclusive_key"] == key
                    ],
                }
                for key in sorted(exclusive)
                if exclusive[key] > 1
            ],
        },
        "reference_checks": reference_checks,
        "violations": violations,
        "summary": {
            "operation_count": len(operations),
            "by_action": dict(
                sorted(
                    Counter(
                        operation["action"] for operation in operations
                    ).items()
                )
            ),
            "dependency_count": sum(
                len(operation["depends_on"]) for operation in operations
            ),
            "violation_count": len(violations),
        },
        "_op_by_id": op_by_id,
    }


def _deletion_reference_resolution(
    *,
    reference_op: dict[str, Any],
    desired_by_id: dict[tuple[str, str, str], Resource],
    reference_kind: str,
    target_name: str,
    observed_deployment: Resource | None = None,
) -> str:
    if reference_op["action"] == "delete":
        return "scheduled_delete"
    if reference_op["action"] != "update":
        return "unresolved"
    identity = reference_op["identity"]
    desired_reference = desired_by_id.get(
        (identity["kind"], identity["namespace"], identity["name"])
    )
    if desired_reference is None:
        return "unresolved"
    if reference_kind == "Deployment":
        if target_name not in desired_reference.body.get("configMapRefs", []):
            return "scheduled_update"
        return "unresolved"
    if reference_kind == "Service":
        assert observed_deployment is not None
        labels = observed_deployment.body.get("labels", {})
        if not selector_matches(
            desired_reference.body["selector"], labels
        ):
            return "scheduled_update"
        return "unresolved"
    return "unresolved"


def _topological_order(
    operations: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    by_id = {operation["operation_id"]: operation for operation in operations}
    indegree = {
        operation["operation_id"]: len(operation["depends_on"])
        for operation in operations
    }
    dependents: dict[str, list[str]] = defaultdict(list)
    for operation in operations:
        for dependency in operation["depends_on"]:
            if dependency in by_id:
                dependents[dependency].append(operation["operation_id"])
    heap: list[tuple[tuple[Any, ...], str]] = []
    for operation in operations:
        if indegree[operation["operation_id"]] == 0:
            identity = operation["identity"]
            heapq.heappush(
                heap,
                (
                    (
                        ACTION_ORDER[operation["action"]],
                        identity["namespace"],
                        KIND_ORDER[identity["kind"]],
                        identity["name"],
                        operation["operation_id"],
                    ),
                    operation["operation_id"],
                ),
            )
    order: list[str] = []
    while heap:
        _, operation_id = heapq.heappop(heap)
        order.append(operation_id)
        for dependent_id in sorted(dependents[operation_id]):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                operation = by_id[dependent_id]
                identity = operation["identity"]
                heapq.heappush(
                    heap,
                    (
                        (
                            ACTION_ORDER[operation["action"]],
                            identity["namespace"],
                            KIND_ORDER[identity["kind"]],
                            identity["name"],
                            operation["operation_id"],
                        ),
                        dependent_id,
                    ),
                )
    cycle_nodes = [
        operation_id
        for operation_id, degree in indegree.items()
        if degree > 0
    ]
    return order, sorted(cycle_nodes)


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in plan.items() if not key.startswith("_")
    }

