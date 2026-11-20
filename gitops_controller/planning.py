"""Rollback planning, dependency ordering, and version regression checks."""

from __future__ import annotations

from collections import deque
from typing import Any

from .cluster import environment_apps, load_cluster_state
from .errors import GitOpsError
from .project import Project
from .store import Store
from .util import sha256_json


def _target_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "depends_on": list(entry["depends_on"]),
        "image_version": entry["image_version"],
        "manifest_revision": entry["manifest_revision"],
        "replicas": entry["replicas"],
        "resources": entry["resources"],
        "tombstone": entry["tombstone"],
    }


def _topological_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_app = {operation["app"]: operation for operation in operations}
    dependency_edges: dict[str, set[str]] = {app: set() for app in by_app}
    for operation in operations:
        for upstream in operation["target"]["depends_on"]:
            if upstream in by_app:
                dependency_edges[operation["app"]].add(upstream)
    indegree = {app: len(dependencies) for app, dependencies in dependency_edges.items()}
    dependents: dict[str, set[str]] = {app: set() for app in by_app}
    for app, dependencies in dependency_edges.items():
        for upstream in dependencies:
            dependents[upstream].add(app)
    ready = deque(sorted(app for app, degree in indegree.items() if degree == 0))
    ordered_apps: list[str] = []
    while ready:
        app = ready.popleft()
        ordered_apps.append(app)
        for dependent in sorted(dependents[app]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        # Re-sort to keep the Kahn frontier deterministic.
        ready = deque(sorted(ready))
    if len(ordered_apps) != len(by_app):
        cycle = sorted(app for app, degree in indegree.items() if degree > 0)
        raise GitOpsError(
            "E_DEP_CYCLE",
            "rollback operation dependency cycle detected",
            {"apps": cycle},
        )
    sequenced = []
    for sequence, app in enumerate(ordered_apps, start=1):
        operation = dict(by_app[app])
        operation["sequence"] = sequence
        sequenced.append(operation)
    return sequenced


def build_plan(
    project: Project,
    store: Store,
    environment: str,
    desired_state: dict[str, Any],
    cluster_state: dict[str, Any],
) -> dict[str, Any]:
    if environment not in project.environments:
        raise GitOpsError(
            "E_BAD_CONFIG",
            "unknown environment",
            {"environment": environment, "known": list(project.environments)},
        )
    actual_apps = environment_apps(cluster_state, environment)
    operations: list[dict[str, Any]] = []
    version_checks: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for entry in sorted(desired_state["entries"], key=lambda item: item["app"]):
        app = entry["app"]
        actual = actual_apps.get(app)
        target_revision = int(entry["manifest_revision"])
        cluster_revision = (
            int(actual.get("committed_manifest_revision", 0)) if actual else 0
        )
        db_revision = store.max_committed_revision(environment, app) or 0
        committed_revision = max(cluster_revision, db_revision)
        accepted = target_revision >= committed_revision
        check = {
            "accepted": accepted,
            "app": app,
            "committed_manifest_revision": committed_revision,
            "committed_revision_sources": {
                "cluster": cluster_revision,
                "controller": db_revision,
            },
            "target_manifest_revision": target_revision,
        }
        version_checks.append(check)
        if not accepted:
            rejected.append(check)
            continue

        target = _target_payload(entry)
        action: str | None = None
        trigger_kind: str | None = None
        drift_fields: list[str] = []
        if entry["tombstone"]:
            if actual is not None:
                action = "delete"
                trigger_kind = "manifest_tombstone"
                drift_fields = ["presence"]
        elif actual is None:
            action = "create"
            trigger_kind = "missing_in_cluster"
            drift_fields = ["presence"]
        else:
            if actual.get("image_version") != entry["image_version"]:
                drift_fields.append("image_version")
            if actual.get("replicas") != entry["replicas"]:
                drift_fields.append("replicas")
            if (
                actual.get("committed_manifest_revision", 0)
                != entry["manifest_revision"]
            ):
                drift_fields.append("committed_manifest_revision")
            if drift_fields:
                action = "update"
                trigger_kind = "cluster_drift"
        if action is None:
            continue

        target_digest = sha256_json(
            {
                "action": action,
                "environment": environment,
                "target": target,
            }
        )
        idempotency_key = sha256_json(
            {
                "action": action,
                "app": app,
                "environment": environment,
                "target_digest": target_digest,
            }
        )
        operations.append(
            {
                "action": action,
                "app": app,
                "idempotency_key": idempotency_key,
                "manifest_provenance": {
                    "logical_clock_ms": entry["logical_clock_ms"],
                    "manifest_revision": entry["manifest_revision"],
                    "raw_time": entry["raw_time"],
                    "source_format": entry["source_format"],
                    "source_id": entry["source_id"],
                },
                "target": target,
                "target_digest": target_digest,
                "trigger": {
                    "actual": None
                    if actual is None
                    else {
                        "committed_manifest_revision": actual.get(
                            "committed_manifest_revision", 0
                        ),
                        "image_version": actual.get("image_version"),
                        "replicas": actual.get("replicas"),
                    },
                    "desired": target,
                    "fields": drift_fields,
                    "kind": trigger_kind,
                    "source_format": entry["source_format"],
                    "source_id": entry["source_id"],
                },
                "version_check": check,
            }
        )

    if rejected:
        first = rejected[0]
        raise GitOpsError(
            "E_VERSION_ROLLBACK",
            "target manifest version is lower than the committed version",
            {
                "app": first["app"],
                "committed_manifest_revision": first["committed_manifest_revision"],
                "environment": environment,
                "rejected": rejected,
                "target_manifest_revision": first["target_manifest_revision"],
            },
        )

    ordered_operations = _topological_operations(operations)
    app_sequences = {
        operation["app"]: operation["sequence"] for operation in ordered_operations
    }
    for operation in ordered_operations:
        upstream_conditions = []
        for upstream in operation["target"]["depends_on"]:
            upstream_conditions.append(
                {
                    "app": upstream,
                    "operation_sequence": app_sequences.get(upstream),
                    "required_status": "committed_or_already_converged",
                }
            )
        operation["upstream_rollback_condition"] = {
            "condition": "all_upstreams_committed_or_converged",
            "mode": "all",
            "upstreams": sorted(
                upstream_conditions,
                key=lambda item: (item["app"], item["operation_sequence"] or 0),
            ),
        }

    action_counts = {
        "create": sum(1 for op in ordered_operations if op["action"] == "create"),
        "delete": sum(1 for op in ordered_operations if op["action"] == "delete"),
        "update": sum(1 for op in ordered_operations if op["action"] == "update"),
    }
    body = {
        "environment": environment,
        "environment_logical_clock_ms": store.get_env_clock(environment),
        "format": "rollback-plan-v1",
        "kind": "rollback_plan",
        "merge_key": desired_state["merge_key"],
        "operations": ordered_operations,
        "ordering": {
            "algorithm": "reverse_dependency_edge_topological_sort",
            "direction": "upstream_before_downstream",
            "stable_tiebreak": "app_name",
        },
        "schema_version": 1,
        "summary": {
            "action_counts": action_counts,
            "active_apps": desired_state["summary"]["active_apps"],
            "operations": len(ordered_operations),
            "version_checks": len(version_checks),
        },
        "upstream_rollback_condition": {
            "condition": "all_upstreams_committed_or_converged",
            "mode": "all",
        },
        "version_checks": version_checks,
    }
    plan_digest = sha256_json(body)
    plan = dict(body)
    plan["plan_digest"] = plan_digest
    plan["plan_id"] = "plan-" + plan_digest[:24]

    return plan


def plan_environment(
    project: Project,
    store: Store,
    environment: str,
) -> dict[str, Any]:
    desired_state = store.get_desired(environment)
    if desired_state is None:
        raise GitOpsError(
            "E_NOT_LOADED",
            "environment has no persisted desired state; run load first",
            {"environment": environment},
        )
    cluster_state = load_cluster_state(project)
    plan = build_plan(project, store, environment, desired_state, cluster_state)
    store.save_plan(plan)
    return plan
