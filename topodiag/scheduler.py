"""Topology/resource adjudication, scheduling, ledgers, and recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import (
    build_workspace,
    candidates_from_rows,
    load_active_view,
    pending_policy_jobs,
)
from .errors import BusinessError
from .model import ClosureRecord, InstantiationRequest, PolicyCandidate
from .storage import (
    active_policy_version_id,
    advance_clock,
    clock_watermark,
    closure_rows,
    connect,
    init_schema,
    policy_rows,
    request_outcome_rows,
    semantic_resource_rows,
    set_runtime_state,
    transaction,
)
from .util import canonical_json, now_ms, read_json, sha256_json, workspace_lock
from .validation import load_validated_samples

LEASE_MS = 30_000


@dataclass
class BoundRequest:
    request: InstantiationRequest
    policy_version_id: int
    attempt: int
    run_id: str


@dataclass
class RunContext:
    policy_version_id: int
    candidates: dict[str, PolicyCandidate]
    closures: dict[str, ClosureRecord]
    manifest_versions: dict[str, int]
    template_resources: dict[str, dict[str, int]]
    namespaces: dict[str, dict[str, Any]]
    invalidated_scopes: set[str]
    ledger: dict[tuple[str, str], tuple[int, int]]
    concurrency: dict[tuple[str, str], tuple[int, int]]


def _crash(point: str, requested: str | None) -> None:
    if requested == point:
        os._exit(79)


def _latest_manifest_version(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """
        SELECT manifest_version FROM template_manifests
        ORDER BY loaded_at_ms DESC, manifest_version DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else str(row["manifest_version"])


def load_run_context(
    connection: sqlite3.Connection,
    policy_version_id: int,
) -> RunContext:
    candidates = candidates_from_rows(policy_rows(connection, policy_version_id))
    closures = {
        name: ClosureRecord(
            template_name=row["template_name"],
            closure=tuple(row["closure"]),
            closure_digest=row["closure_digest"],
            config_fingerprint=row["config_fingerprint"],
            dependency_digest=row["dependency_digest"],
            revision=row["revision"],
        )
        for name, row in closure_rows(connection, policy_version_id).items()
    }
    manifest_version = _latest_manifest_version(connection)
    manifest_versions: dict[str, int] = {}
    template_resources: dict[str, dict[str, int]] = {}
    if manifest_version is not None:
        rows = connection.execute(
            """
            SELECT template_name, version, resources_json
            FROM template_manifest_entries
            WHERE manifest_version=?
            """,
            (manifest_version,),
        ).fetchall()
        for row in rows:
            manifest_versions[row["template_name"]] = int(row["version"])
            template_resources[row["template_name"]] = json.loads(
                row["resources_json"]
            )
    topology_row = connection.execute(
        "SELECT value_json FROM runtime_state WHERE key='topology'"
    ).fetchone()
    topology = {} if topology_row is None else json.loads(topology_row["value_json"])
    invalidated = {
        row["scope"]
        for row in connection.execute(
            """
            SELECT scope FROM topology_invalidations
            WHERE policy_version_id=?
            """,
            (policy_version_id,),
        ).fetchall()
    }
    ledger = {
        (row["node_id"], row["resource_name"]): (
            int(row["capacity"]),
            int(row["used"]),
        )
        for row in connection.execute(
            """
            SELECT node_id, resource_name, capacity, used
            FROM resource_ledger
            """
        ).fetchall()
    }
    concurrency = {
        (row["node_id"], row["template_name"]): (
            int(row["used"]),
            int(row["limit_value"]),
        )
        for row in connection.execute(
            """
            SELECT node_id, template_name, used, limit_value
            FROM template_concurrency
            """
        ).fetchall()
    }
    return RunContext(
        policy_version_id=policy_version_id,
        candidates=candidates,
        closures=closures,
        manifest_versions=manifest_versions,
        template_resources=template_resources,
        namespaces=topology.get("namespaces", {}),
        invalidated_scopes=invalidated,
        ledger=ledger,
        concurrency=concurrency,
    )


def _topological_request_nodes(
    root: str,
    candidates: dict[str, PolicyCandidate],
) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            return
        visiting.add(name)
        candidate = candidates.get(name)
        if candidate is not None:
            for dependency in sorted(candidate.dependencies):
                visit(dependency)
        visiting.discard(name)
        visited.add(name)
        ordered.append(name)

    visit(root)
    return ordered


def _topology_break(
    request: InstantiationRequest,
    context: RunContext,
) -> dict[str, Any] | None:
    namespace = context.namespaces.get(request.target_topology_namespace)
    if namespace is None:
        return {
            "reason": "missing_namespace",
            "code": "TOPOLOGY_BREAK",
            "namespace": request.target_topology_namespace,
        }
    namespace_scope = f"namespace:{request.target_topology_namespace}"
    if namespace_scope in context.invalidated_scopes:
        return {
            "reason": "namespace_invalidated",
            "code": "TOPOLOGY_BREAK",
            "namespace": request.target_topology_namespace,
            "scope": namespace_scope,
            "node_id": namespace["node_id"],
        }
    if request.template_name not in context.candidates:
        return {
            "reason": "missing_template",
            "code": "TOPOLOGY_BREAK",
            "template_name": request.template_name,
            "namespace": request.target_topology_namespace,
        }
    closure = context.closures.get(request.template_name)
    if closure is not None and len(closure.closure) > int(namespace.get("max_depth", 16)):
        return {
            "reason": "topology_depth_exceeded",
            "code": "TOPOLOGY_BREAK",
            "template_name": request.template_name,
            "depth": len(closure.closure),
            "max_depth": namespace.get("max_depth"),
            "namespace": request.target_topology_namespace,
        }
    for name in _topological_request_nodes(request.template_name, context.candidates):
        candidate = context.candidates.get(name)
        if candidate is None:
            return {
                "reason": "missing_template",
                "code": "TOPOLOGY_BREAK",
                "template_name": name,
                "namespace": request.target_topology_namespace,
            }
        template_scope = f"template:{name}"
        if template_scope in context.invalidated_scopes:
            return {
                "reason": "template_invalidated",
                "code": "TOPOLOGY_BREAK",
                "template_name": name,
                "namespace": request.target_topology_namespace,
                "scope": template_scope,
            }
        if candidate.tombstone:
            return {
                "reason": "tombstoned_template",
                "code": "TOPOLOGY_BREAK",
                "template_name": name,
                "version": candidate.version,
                "namespace": request.target_topology_namespace,
            }
        required_version = context.manifest_versions.get(name)
        if required_version is not None and candidate.version < required_version:
            return {
                "reason": "version_conflict",
                "code": "TOPOLOGY_BREAK",
                "template_name": name,
                "version": candidate.version,
                "required_version": required_version,
                "namespace": request.target_topology_namespace,
            }
        for dependency in sorted(candidate.dependencies):
            if dependency not in context.candidates:
                return {
                    "reason": "missing_dependency",
                    "code": "TOPOLOGY_BREAK",
                    "template_name": name,
                    "missing_dependency": dependency,
                    "namespace": request.target_topology_namespace,
                }
    return None


def _topology_snapshot(
    request: InstantiationRequest,
    context: RunContext,
) -> dict[str, Any]:
    closure = context.closures[request.template_name]
    template_names = sorted(set(closure.closure) | {request.template_name})
    nodes = [
        {
            "template_name": name,
            "version": context.candidates[name].version,
            "source_format": context.candidates[name].source_format,
            "source_version": context.candidates[name].source_version,
            "tombstone": context.candidates[name].tombstone,
        }
        for name in template_names
    ]
    edges = [
        {"from_template": name, "to_template": dependency}
        for name in template_names
        for dependency in sorted(context.candidates[name].dependencies)
        if dependency in template_names
    ]
    return {
        "root_template": request.template_name,
        "namespace": request.target_topology_namespace,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _resource_plan(
    request: InstantiationRequest,
    context: RunContext,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    namespace = context.namespaces[request.target_topology_namespace]
    node_id = namespace["node_id"]
    closure = context.closures[request.template_name]
    template_names = sorted(set(closure.closure) | {request.template_name})
    totals: dict[str, int] = {}
    breakdown: list[dict[str, Any]] = []
    for template_name in template_names:
        resources = context.template_resources.get(template_name, {})
        for resource_name, amount in sorted(resources.items()):
            totals[resource_name] = totals.get(resource_name, 0) + int(amount)
            breakdown.append(
                {
                    "template_name": template_name,
                    "resource_name": resource_name,
                    "amount": int(amount),
                }
            )
    observations: list[dict[str, Any]] = []
    first_failure: dict[str, Any] | None = None
    for resource_name in sorted(totals):
        capacity, used = context.ledger.get((node_id, resource_name), (0, 0))
        required = totals[resource_name]
        observation = {
            "node_id": node_id,
            "resource_name": resource_name,
            "capacity": capacity,
            "used_before": used,
            "available_before": capacity - used,
            "required": required,
            "sufficient": used + required <= capacity,
        }
        observations.append(observation)
        if first_failure is None and not observation["sufficient"]:
            first_failure = {
                "reason": "resource_insufficient",
                "code": "RESOURCE_INSUFFICIENT",
                "constraint": "node_resource_budget",
                "node_id": node_id,
                "resource_name": resource_name,
                "capacity": capacity,
                "used_before": used,
                "required": required,
                "available_before": capacity - used,
            }
    concurrency_observation: dict[str, Any] | None = None
    if (node_id, request.template_name) in context.concurrency:
        used, limit = context.concurrency[(node_id, request.template_name)]
        concurrency_observation = {
            "node_id": node_id,
            "template_name": request.template_name,
            "used_before": used,
            "limit": limit,
            "required": 1,
            "sufficient": used + 1 <= limit,
        }
        if first_failure is None and used + 1 > limit:
            first_failure = {
                "reason": "template_concurrency_exceeded",
                "code": "TEMPLATE_CONCURRENCY_EXCEEDED",
                "constraint": "template_concurrency",
                "node_id": node_id,
                "template_name": request.template_name,
                "used_before": used,
                "limit": limit,
            }
    plan = {
        "node_id": node_id,
        "namespace": request.target_topology_namespace,
        "templates": template_names,
        "required_resources": totals,
        "resource_breakdown": breakdown,
        "resource_observations": observations,
        "concurrency_observation": concurrency_observation,
    }
    return plan, first_failure


def preflight_request(
    request: InstantiationRequest,
    context: RunContext,
) -> dict[str, Any]:
    if request.desired_state == "abandoned":
        return {
            "kind": "abandoned",
            "status": "abandoned",
            "outcome": "abandoned",
            "reason_code": "ACTIVE_ABANDON",
            "detail": "request was explicitly abandoned before resource allocation",
            "decision": {"desired_state": request.desired_state},
        }
    if request.desired_state == "paused":
        return {
            "kind": "rejected",
            "status": "rejected",
            "outcome": "rejected",
            "reason_code": "DESIRED_STATE_NOT_ACTIVE",
            "detail": "request desired_state does not permit instantiation",
            "decision": {"desired_state": request.desired_state},
        }
    breakpoint = _topology_break(request, context)
    if breakpoint is not None:
        return {
            "kind": "rejected",
            "status": "rejected",
            "outcome": "rejected",
            "reason_code": breakpoint["code"],
            "detail": breakpoint["reason"],
            "decision": {"topology": breakpoint},
        }
    topology_snapshot = _topology_snapshot(request, context)
    if request.inject_start_failure:
        return {
            "kind": "started_failed",
            "status": "started_failed",
            "outcome": "started_failed",
            "reason_code": "INSTANCE_START_FAILED",
            "detail": "request was started and then failed during instance startup",
            "decision": {
                "topology": {"closed": True, "graph": topology_snapshot}
            },
        }
    plan, first_failure = _resource_plan(request, context)
    if first_failure is not None:
        return {
            "kind": "rejected",
            "status": "rejected",
            "outcome": "rejected",
            "reason_code": first_failure["code"],
            "detail": first_failure["reason"],
            "decision": {
                "topology": {
                    "closed": True,
                    "graph": _topology_snapshot(request, context),
                },
                "resource_plan": plan,
                "first_constraint": first_failure,
            },
        }
    return {
        "kind": "success",
        "status": "processing",
        "outcome": None,
        "reason_code": None,
        "detail": None,
        "decision": {
            "topology": {"closed": True, "graph": topology_snapshot},
            "resource_plan": plan,
        },
    }


def _claim_request(
    connection: sqlite3.Connection,
    request: InstantiationRequest,
    *,
    run_id: str,
    run_policy_version_id: int,
) -> tuple[str, BoundRequest | None]:
    current = now_ms()
    with transaction(connection):
        row = connection.execute(
            "SELECT * FROM request_state WHERE request_seq=?",
            (request.request_seq,),
        ).fetchone()
        if row is not None and row["outcome"] is not None:
            return "existing", None
        if row is None:
            connection.execute(
                """
                INSERT INTO request_state(
                    request_seq, instance_id, template_name,
                    target_topology_namespace, desired_state,
                    logical_clock_ms, round_no, logical_cost_ms,
                    policy_version_id, status, outcome, reason_code,
                    reason_detail, decision_json, run_id, attempt,
                    heartbeat_ms, started_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', NULL,
                          NULL, NULL, '{}', ?, 1, ?, ?, ?)
                """,
                (
                    request.request_seq,
                    request.instance_id,
                    request.template_name,
                    request.target_topology_namespace,
                    request.desired_state,
                    request.logical_clock,
                    request.round_no,
                    request.logical_cost_ms,
                    run_policy_version_id,
                    run_id,
                    current,
                    current,
                    current,
                ),
            )
            return (
                "claimed",
                BoundRequest(
                    request=request,
                    policy_version_id=run_policy_version_id,
                    attempt=1,
                    run_id=run_id,
                ),
            )
        if row["status"] == "processing":
            if row["run_id"] != run_id and current - int(
                row["heartbeat_ms"] or 0
            ) < LEASE_MS:
                return "busy", None
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """
                UPDATE request_state
                SET run_id=?, attempt=?, heartbeat_ms=?, updated_at_ms=?
                WHERE request_seq=?
                """,
                (run_id, attempt, current, current, request.request_seq),
            )
            return (
                "claimed",
                BoundRequest(
                    request=request,
                    policy_version_id=int(row["policy_version_id"]),
                    attempt=attempt,
                    run_id=run_id,
                ),
            )
        if row["outcome"] is None:
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """
                UPDATE request_state
                SET status='processing', run_id=?, attempt=?,
                    heartbeat_ms=?, updated_at_ms=?
                WHERE request_seq=?
                """,
                (run_id, attempt, current, current, request.request_seq),
            )
            return (
                "claimed",
                BoundRequest(
                    request=request,
                    policy_version_id=int(row["policy_version_id"]),
                    attempt=attempt,
                    run_id=run_id,
                ),
            )
        return "busy", None


def _finish_terminal(
    connection: sqlite3.Connection,
    bound: BoundRequest,
    result: dict[str, Any],
) -> bool:
    with transaction(connection):
        row = connection.execute(
            """
            SELECT status, attempt, run_id FROM request_state
            WHERE request_seq=?
            """,
            (bound.request.request_seq,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                "RECOVERY_INCONSISTENT",
                "Request row disappeared while finishing",
                request_seq=bound.request.request_seq,
            )
        if (
            row["status"] != "processing"
            or int(row["attempt"]) != bound.attempt
            or row["run_id"] != bound.run_id
        ):
            return False
        advance_clock(
            connection,
            bound.request.logical_clock,
            request_seq=bound.request.request_seq,
        )
        current = now_ms()
        connection.execute(
            """
            UPDATE request_state
            SET status=?, outcome=?, reason_code=?, reason_detail=?,
                decision_json=?, heartbeat_ms=NULL, updated_at_ms=?,
                committed_at_ms=?
            WHERE request_seq=?
            """,
            (
                result["status"],
                result["outcome"],
                result["reason_code"],
                result["detail"],
                canonical_json(result["decision"]),
                current,
                current,
                bound.request.request_seq,
            ),
        )
        return True


def _record_not_started(
    connection: sqlite3.Connection,
    request: InstantiationRequest,
    *,
    run_policy_version_id: int,
    reason: str,
) -> None:
    current = now_ms()
    with transaction(connection):
        existing = connection.execute(
            "SELECT request_seq FROM request_state WHERE request_seq=?",
            (request.request_seq,),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO request_state(
                request_seq, instance_id, template_name,
                target_topology_namespace, desired_state, logical_clock_ms,
                round_no, logical_cost_ms, policy_version_id, status,
                outcome, reason_code, reason_detail, decision_json,
                run_id, attempt, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_started',
                      'not_started', 'ROUND_BUDGET_EXHAUSTED', ?, '{}',
                      NULL, 0, ?)
            """,
            (
                request.request_seq,
                request.instance_id,
                request.template_name,
                request.target_topology_namespace,
                request.desired_state,
                request.logical_clock,
                request.round_no,
                request.logical_cost_ms,
                run_policy_version_id,
                reason,
                current,
            ),
        )


def _commit_success(
    connection: sqlite3.Connection,
    bound: BoundRequest,
    context: RunContext,
    *,
    crash_at: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    request = bound.request
    with transaction(connection):
        row = connection.execute(
            """
            SELECT status, attempt, run_id, policy_version_id
            FROM request_state WHERE request_seq=?
            """,
            (request.request_seq,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "processing"
            or int(row["attempt"]) != bound.attempt
            or row["run_id"] != bound.run_id
        ):
            return False, None
        existing_instance = connection.execute(
            "SELECT instance_id FROM instances WHERE request_seq=?",
            (request.request_seq,),
        ).fetchone()
        if existing_instance is not None:
            return True, {"duplicate_instance_prevented": True}
        live_context = load_run_context(connection, bound.policy_version_id)
        plan, first_failure = _resource_plan(request, live_context)
        topology_snapshot = _topology_snapshot(request, live_context)
        if first_failure is not None:
            return False, {
                "kind": "rejected",
                "status": "rejected",
                "outcome": "rejected",
                "reason_code": first_failure["code"],
                "detail": first_failure["reason"],
                "decision": {
                    "topology": {"closed": True, "graph": topology_snapshot},
                    "resource_plan": plan,
                    "first_constraint": first_failure,
                },
            }
        node_id = plan["node_id"]
        current = now_ms()
        connection.execute(
            """
            INSERT INTO instances(
                instance_id, request_seq, policy_version_id, node_id,
                template_name, target_topology_namespace, status,
                committed_seq, committed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 'committed', ?, ?)
            """,
            (
                request.instance_id,
                request.request_seq,
                bound.policy_version_id,
                node_id,
                request.template_name,
                request.target_topology_namespace,
                request.request_seq,
                current,
            ),
        )
        for item in plan["resource_breakdown"]:
            connection.execute(
                """
                INSERT INTO instance_resources(
                    instance_id, node_id, template_name,
                    resource_name, amount
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.instance_id,
                    node_id,
                    item["template_name"],
                    item["resource_name"],
                    item["amount"],
                ),
            )
        for resource_name in sorted(plan["required_resources"]):
            required = plan["required_resources"][resource_name]
            ledger_row = connection.execute(
                """
                SELECT capacity, used FROM resource_ledger
                WHERE node_id=? AND resource_name=?
                """,
                (node_id, resource_name),
            ).fetchone()
            if ledger_row is None:
                raise BusinessError(
                    "RESOURCE_LEDGER_MISSING",
                    "Resource ledger entry disappeared during commit",
                    node_id=node_id,
                    resource_name=resource_name,
                    request_seq=request.request_seq,
                )
            before = int(ledger_row["used"])
            capacity = int(ledger_row["capacity"])
            if before + required > capacity:
                raise BusinessError(
                    "RESOURCE_INSUFFICIENT",
                    "Resource ledger changed during commit and is insufficient",
                    node_id=node_id,
                    resource_name=resource_name,
                    request_seq=request.request_seq,
                    capacity=capacity,
                    used_before=before,
                    required=required,
                )
            cursor = connection.execute(
                """
                UPDATE resource_ledger
                SET used=used+?
                WHERE node_id=? AND resource_name=? AND used+?<=capacity
                """,
                (required, node_id, resource_name, required),
            )
            if cursor.rowcount != 1:
                raise BusinessError(
                    "RESOURCE_LEDGER_CONFLICT",
                    "Resource ledger compare-and-set failed",
                    node_id=node_id,
                    resource_name=resource_name,
                    request_seq=request.request_seq,
                )
            connection.execute(
                """
                INSERT INTO ledger_events(
                    instance_id, node_id, resource_name,
                    before_value, delta, after_value, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.instance_id,
                    node_id,
                    resource_name,
                    before,
                    required,
                    before + required,
                    current,
                ),
            )
        concurrency_row = connection.execute(
            """
            SELECT used, limit_value FROM template_concurrency
            WHERE node_id=? AND template_name=?
            """,
            (node_id, request.template_name),
        ).fetchone()
        if concurrency_row is not None:
            used = int(concurrency_row["used"])
            limit = int(concurrency_row["limit_value"])
            if used + 1 > limit:
                raise BusinessError(
                    "TEMPLATE_CONCURRENCY_EXCEEDED",
                    "Template concurrency changed during commit",
                    node_id=node_id,
                    template_name=request.template_name,
                    request_seq=request.request_seq,
                    used_before=used,
                    limit=limit,
                )
            connection.execute(
                """
                UPDATE template_concurrency
                SET used=used+1
                WHERE node_id=? AND template_name=? AND used+1<=limit_value
                """,
                (node_id, request.template_name),
            )
        advance_clock(
            connection,
            request.logical_clock,
            request_seq=request.request_seq,
        )
        connection.execute(
            """
            UPDATE request_state
            SET status='completed', outcome='completed', reason_code=NULL,
                reason_detail=NULL, decision_json=?, heartbeat_ms=NULL,
                updated_at_ms=?, committed_at_ms=?
            WHERE request_seq=? AND status='processing'
            """,
            (
                canonical_json(
                    {
                        "topology": {
                            "closed": True,
                            "graph": topology_snapshot,
                        },
                        "resource_plan": plan,
                        "ledger_transaction": "committed",
                    }
                ),
                current,
                current,
                request.request_seq,
            ),
        )
        watermark = connection.execute(
            "SELECT value_json FROM runtime_state WHERE key='committed_instance_watermark'"
        ).fetchone()
        previous_watermark = (
            0 if watermark is None else int(json.loads(watermark["value_json"]))
        )
        set_runtime_state(
            connection,
            "committed_instance_watermark",
            max(previous_watermark, request.request_seq),
        )
        _crash("resource", crash_at)
        return True, {
            "kind": "success",
            "status": "completed",
            "outcome": "completed",
            "reason_code": None,
            "detail": None,
            "decision": {
                "topology": {"closed": True, "graph": topology_snapshot},
                "resource_plan": plan,
                "ledger_transaction": "committed",
            },
        }


def _context_for(
    connection: sqlite3.Connection,
    policy_version_id: int,
    cache: dict[int, RunContext],
) -> RunContext:
    context = cache.get(policy_version_id)
    if context is None:
        context = load_run_context(connection, policy_version_id)
        cache[policy_version_id] = context
    return context


def _refresh_context_after_commit(context: RunContext) -> None:
    """Resource snapshots are refreshed by loading a new context per round."""


def _schedule_round(
    connection: sqlite3.Connection,
    requests: list[InstantiationRequest],
    *,
    run_id: str,
    run_policy_version_id: int,
    context_cache: dict[int, RunContext],
    budget_ms: int,
    workers: int,
    crash_at: str | None,
    workdir: Path,
) -> dict[str, Any]:
    spent = 0
    selected: list[BoundRequest] = []
    skipped: list[dict[str, Any]] = []
    for request in sorted(requests, key=lambda item: item.request_seq):
        existing = connection.execute(
            """
            SELECT status, outcome, logical_cost_ms
            FROM request_state WHERE request_seq=?
            """,
            (request.request_seq,),
        ).fetchone()
        if existing is not None and existing["outcome"] is not None:
            if existing["outcome"] != "not_started":
                spent += int(existing["logical_cost_ms"])
            skipped.append(
                {
                    "request_seq": request.request_seq,
                    "disposition": "already_terminal",
                    "outcome": existing["outcome"],
                }
            )
            continue
        if spent + request.logical_cost_ms > budget_ms:
            _record_not_started(
                connection,
                request,
                run_policy_version_id=run_policy_version_id,
                reason=(
                    f"round budget {budget_ms} ms exhausted before request "
                    f"could start"
                ),
            )
            skipped.append(
                {
                    "request_seq": request.request_seq,
                    "disposition": "not_started",
                    "reason_code": "ROUND_BUDGET_EXHAUSTED",
                }
            )
            continue
        spent += request.logical_cost_ms
        with workspace_lock(workdir, "request-claims"):
            claim, bound = _claim_request(
                connection,
                request,
                run_id=run_id,
                run_policy_version_id=run_policy_version_id,
            )
        if claim == "existing":
            skipped.append(
                {
                    "request_seq": request.request_seq,
                    "disposition": "already_terminal",
                    "outcome": "existing",
                }
            )
            continue
        if claim == "busy" or bound is None:
            skipped.append(
                {
                    "request_seq": request.request_seq,
                    "disposition": "busy",
                }
            )
            continue
        selected.append(bound)

    if crash_at == "dependency" and selected:
        _crash("dependency", crash_at)

    grouped: dict[str, list[BoundRequest]] = {}
    for bound in selected:
        grouped.setdefault(
            bound.request.target_topology_namespace,
            [],
        ).append(bound)

    def preflight_group(
        group: list[tuple[BoundRequest, RunContext]],
    ) -> list[tuple[BoundRequest, dict[str, Any]]]:
        results: list[tuple[BoundRequest, dict[str, Any]]] = []
        for bound, context in sorted(
            group,
            key=lambda item: item[0].request.request_seq,
        ):
            results.append(
                (bound, preflight_request(bound.request, context))
            )
        return results

    prefetched: list[tuple[BoundRequest, dict[str, Any]]] = []
    if grouped:
        context_groups: dict[str, list[tuple[BoundRequest, RunContext]]] = {}
        for namespace, group in grouped.items():
            context_groups[namespace] = [
                (
                    bound,
                    _context_for(
                        connection,
                        bound.policy_version_id,
                        context_cache,
                    ),
                )
                for bound in group
            ]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(grouped)))) as pool:
            futures = [
                pool.submit(preflight_group, context_groups[namespace])
                for namespace in sorted(context_groups)
            ]
            for future in futures:
                prefetched.extend(future.result())
    prefetched.sort(key=lambda item: item[0].request.request_seq)

    outcomes: list[dict[str, Any]] = []
    for bound, result in prefetched:
        if result["kind"] == "success":
            committed, commit_result = _commit_success(
                connection,
                bound,
                _context_for(
                    connection,
                    bound.policy_version_id,
                    context_cache,
                ),
                crash_at=crash_at,
            )
            if not committed and commit_result is not None:
                _finish_terminal(connection, bound, commit_result)
                result = commit_result
            elif not committed:
                result = {
                    "kind": "busy",
                    "status": "processing",
                    "outcome": None,
                    "reason_code": "REQUEST_LEASE_LOST",
                    "detail": "request lease was lost before commit",
                    "decision": {},
                }
        if result["kind"] not in {"busy"}:
            _finish_terminal(connection, bound, result)
            # A committed success already updated the row; the conditional
            # update in _finish_terminal becomes a no-op for that request.
        outcomes.append(
            {
                "request_seq": bound.request.request_seq,
                "instance_id": bound.request.instance_id,
                "template_name": bound.request.template_name,
                "namespace": bound.request.target_topology_namespace,
                "status": result["status"],
                "outcome": result["outcome"],
                "reason_code": result["reason_code"],
                "detail": result["detail"],
            }
        )
        context_cache.clear()
    return {
        "spent_ms": spent,
        "budget_ms": budget_ms,
        "namespace_groups": sorted(grouped),
        "claimed": len(selected),
        "skipped": skipped,
        "outcomes": outcomes,
    }


def schedule_workspace(
    workdir: Path,
    *,
    round_budget_ms: int,
    workers: int = 4,
    crash_at: str | None = None,
    recovery: bool = False,
) -> dict[str, Any]:
    _, _, _, requests, _ = load_validated_samples(workdir)
    connection = connect(workdir)
    try:
        init_schema(connection)
        run_policy_version_id = active_policy_version_id(connection)
        if run_policy_version_id is None:
            raise BusinessError(
                "POLICY_NOT_BUILT",
                "No active policy version exists; run build first",
            )
        run_id = uuid.uuid4().hex
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO schedule_runs(
                    run_id, policy_version_id, round_budget_ms,
                    worker_pid, started_at_ms, status, recovered
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    run_policy_version_id,
                    round_budget_ms,
                    os.getpid(),
                    now_ms(),
                    1 if recovery else 0,
                ),
            )
        context_cache: dict[int, RunContext] = {}
        rounds: dict[int, list[InstantiationRequest]] = {}
        for request in requests:
            rounds.setdefault(request.round_no, []).append(request)
        round_results: list[dict[str, Any]] = []
        for round_no in sorted(rounds):
            with workspace_lock(workdir, "schedule-round"):
                result = _schedule_round(
                    connection,
                    rounds[round_no],
                    run_id=run_id,
                    run_policy_version_id=run_policy_version_id,
                    context_cache=context_cache,
                    budget_ms=round_budget_ms,
                    workers=workers,
                    crash_at=crash_at,
                    workdir=workdir,
                )
            result["round"] = round_no
            round_results.append(result)
        with transaction(connection):
            connection.execute(
                """
                UPDATE schedule_runs
                SET status='completed', finished_at_ms=?
                WHERE run_id=?
                """,
                (now_ms(), run_id),
            )
        outcomes = request_outcome_rows(connection)
        summary: dict[str, int] = {}
        for row in outcomes:
            outcome = row["outcome"] or row["status"]
            summary[outcome] = summary.get(outcome, 0) + 1
        return {
            "ok": True,
            "command": "schedule",
            "run_id": run_id,
            "policy_version_id": run_policy_version_id,
            "round_budget_ms": round_budget_ms,
            "rounds": round_results,
            "summary": dict(sorted(summary.items())),
            "outcomes": outcomes,
            "resources": semantic_resource_rows(connection),
            "clock_watermark_ms": clock_watermark(connection),
        }
    finally:
        connection.close()


def _rebuild_ledger_and_concurrency(
    connection: sqlite3.Connection,
    actions: list[dict[str, Any]],
) -> None:
    expected: dict[tuple[str, str], int] = {}
    for row in connection.execute(
        """
        SELECT ir.node_id, ir.resource_name, SUM(ir.amount) AS amount
        FROM instance_resources ir
        JOIN instances i ON i.instance_id=ir.instance_id
        WHERE i.status='committed'
        GROUP BY ir.node_id, ir.resource_name
        """
    ).fetchall():
        expected[(row["node_id"], row["resource_name"])] = int(row["amount"])
    ledger_rows = connection.execute(
        "SELECT node_id, resource_name, capacity, used FROM resource_ledger"
    ).fetchall()
    for row in ledger_rows:
        key = (row["node_id"], row["resource_name"])
        wanted = expected.get(key, 0)
        if wanted > int(row["capacity"]):
            raise BusinessError(
                "RECOVERY_INCONSISTENT",
                "Committed instance resources exceed node capacity",
                node_id=row["node_id"],
                resource_name=row["resource_name"],
                required=wanted,
                capacity=row["capacity"],
            )
        if int(row["used"]) != wanted:
            connection.execute(
                """
                UPDATE resource_ledger SET used=?
                WHERE node_id=? AND resource_name=?
                """,
                (wanted, row["node_id"], row["resource_name"]),
            )
            actions.append(
                {
                    "action": "ledger_repaired",
                    "node_id": row["node_id"],
                    "resource_name": row["resource_name"],
                    "before": int(row["used"]),
                    "after": wanted,
                }
            )
    event_totals = {
        (row["node_id"], row["resource_name"]): int(row["amount"])
        for row in connection.execute(
            """
            SELECT e.node_id, e.resource_name, SUM(e.delta) AS amount
            FROM ledger_events e
            JOIN instances i ON i.instance_id=e.instance_id
            WHERE i.status='committed'
            GROUP BY e.node_id, e.resource_name
            """
        ).fetchall()
    }
    if event_totals != expected:
        connection.execute("DELETE FROM ledger_events")
        for key in sorted(expected):
            rows = connection.execute(
                """
                SELECT ir.instance_id, i.committed_seq
                FROM instance_resources ir
                JOIN instances i ON i.instance_id=ir.instance_id
                WHERE ir.node_id=? AND ir.resource_name=?
                ORDER BY i.committed_seq, ir.instance_id, ir.template_name
                """,
                key,
            ).fetchall()
            running = 0
            for row in rows:
                amount = connection.execute(
                    """
                    SELECT amount FROM instance_resources
                    WHERE instance_id=? AND node_id=? AND resource_name=?
                    """,
                    (row["instance_id"], key[0], key[1]),
                ).fetchone()["amount"]
                connection.execute(
                    """
                    INSERT INTO ledger_events(
                        instance_id, node_id, resource_name,
                        before_value, delta, after_value, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["instance_id"],
                        key[0],
                        key[1],
                        running,
                        int(amount),
                        running + int(amount),
                        now_ms(),
                    ),
                )
                running += int(amount)
        actions.append({"action": "ledger_events_rebuilt"})
    expected_concurrency: dict[tuple[str, str], int] = {}
    for row in connection.execute(
        """
        SELECT node_id, template_name, COUNT(*) AS count
        FROM instances
        WHERE status='committed'
        GROUP BY node_id, template_name
        """
    ).fetchall():
        expected_concurrency[(row["node_id"], row["template_name"])] = int(
            row["count"]
        )
    for row in connection.execute(
        "SELECT node_id, template_name, used, limit_value FROM template_concurrency"
    ).fetchall():
        key = (row["node_id"], row["template_name"])
        wanted = expected_concurrency.get(key, 0)
        if wanted > int(row["limit_value"]):
            raise BusinessError(
                "RECOVERY_INCONSISTENT",
                "Committed concurrency exceeds the configured limit",
                node_id=row["node_id"],
                template_name=row["template_name"],
                used=wanted,
                limit=row["limit_value"],
            )
        if int(row["used"]) != wanted:
            connection.execute(
                """
                UPDATE template_concurrency SET used=?
                WHERE node_id=? AND template_name=?
                """,
                (wanted, row["node_id"], row["template_name"]),
            )
            actions.append(
                {
                    "action": "concurrency_repaired",
                    "node_id": row["node_id"],
                    "template_name": row["template_name"],
                    "before": int(row["used"]),
                    "after": wanted,
                }
            )


def recover_workspace(
    workdir: Path,
    *,
    round_budget_ms: int,
    workers: int = 4,
) -> dict[str, Any]:
    connection = connect(workdir)
    actions: list[dict[str, Any]] = []
    try:
        init_schema(connection)
        with transaction(connection):
            staging = connection.execute(
                """
                SELECT policy_version_id FROM policy_versions
                WHERE state='staging'
                """
            ).fetchall()
            for row in staging:
                connection.execute(
                    "DELETE FROM policy_versions WHERE policy_version_id=?",
                    (row["policy_version_id"],),
                )
                actions.append(
                    {
                        "action": "staging_policy_removed",
                        "policy_version_id": row["policy_version_id"],
                    }
                )
            connection.execute(
                """
                UPDATE schedule_runs
                SET status='interrupted', finished_at_ms=?
                WHERE status='running'
                """,
                (now_ms(),),
            )
            processing = connection.execute(
                """
                SELECT request_seq, policy_version_id, attempt
                FROM request_state
                WHERE status='processing'
                ORDER BY request_seq
                """
            ).fetchall()
            for row in processing:
                connection.execute(
                    """
                    UPDATE request_state
                    SET status='recovery_pending', run_id=NULL,
                        heartbeat_ms=NULL, updated_at_ms=?
                    WHERE request_seq=?
                    """,
                    (now_ms(), row["request_seq"]),
                )
                actions.append(
                    {
                        "action": "incomplete_topology_released",
                        "request_seq": row["request_seq"],
                        "policy_version_id": row["policy_version_id"],
                        "attempt": row["attempt"],
                    }
                )
            committed_without_completed = connection.execute(
                """
                SELECT i.instance_id, i.request_seq
                FROM instances i
                JOIN request_state r ON r.request_seq=i.request_seq
                WHERE i.status='committed' AND r.status!='completed'
                """
            ).fetchall()
            for row in committed_without_completed:
                connection.execute(
                    """
                    UPDATE request_state
                    SET status='completed', outcome='completed',
                        reason_code=NULL, reason_detail=NULL,
                        updated_at_ms=?, committed_at_ms=?
                    WHERE request_seq=?
                    """,
                    (now_ms(), now_ms(), row["request_seq"]),
                )
                actions.append(
                    {
                        "action": "committed_instance_reconciled",
                        "request_seq": row["request_seq"],
                        "instance_id": row["instance_id"],
                    }
                )
            completed_without_instance = connection.execute(
                """
                SELECT r.request_seq
                FROM request_state r
                LEFT JOIN instances i ON i.request_seq=r.request_seq
                WHERE r.outcome='completed' AND i.instance_id IS NULL
                """
            ).fetchall()
            for row in completed_without_instance:
                connection.execute(
                    """
                    UPDATE request_state
                    SET status='recovery_pending', outcome=NULL,
                        reason_code='MISSING_INSTANCE_RECORD',
                        run_id=NULL, updated_at_ms=?
                    WHERE request_seq=?
                    """,
                    (now_ms(), row["request_seq"]),
                )
                actions.append(
                    {
                        "action": "missing_instance_record_released",
                        "request_seq": row["request_seq"],
                    }
                )
            _rebuild_ledger_and_concurrency(connection, actions)
            watermark_row = connection.execute(
                "SELECT MAX(committed_seq) AS value FROM instances"
            ).fetchone()
            watermark = 0 if watermark_row["value"] is None else int(
                watermark_row["value"]
            )
            set_runtime_state(
                connection,
                "committed_instance_watermark",
                watermark,
            )
            for action in actions:
                connection.execute(
                    """
                    INSERT INTO recovery_events(
                        action, details_json, created_at_ms
                    ) VALUES (?, ?, ?)
                    """,
                    (action["action"], canonical_json(action), now_ms()),
                )
        pending_jobs = pending_policy_jobs(connection)
    finally:
        connection.close()
    policy_recovery: list[dict[str, Any]] = []
    for job in pending_jobs:
        result = build_workspace(
            workdir,
            policy_package=Path(job["package_path"]),
        )
        policy_recovery.append(
            {
                "job_id": job["job_id"],
                "package_path": job["package_path"],
                "mode": result.get("mode"),
            }
        )
    connection = connect(workdir)
    try:
        has_active_policy = active_policy_version_id(connection) is not None
    finally:
        connection.close()
    if not has_active_policy:
        result = build_workspace(workdir)
        policy_recovery.append(
            {
                "job_id": None,
                "package_path": None,
                "mode": result.get("mode"),
                "action": "initial_policy_rebuilt",
            }
        )
    schedule_result = schedule_workspace(
        workdir,
        round_budget_ms=round_budget_ms,
        workers=workers,
        recovery=True,
    )
    return {
        "ok": True,
        "command": "recover",
        "actions": actions,
        "policy_recovery": policy_recovery,
        "schedule": schedule_result,
        "resources": schedule_result["resources"],
        "clock_watermark_ms": schedule_result["clock_watermark_ms"],
    }
