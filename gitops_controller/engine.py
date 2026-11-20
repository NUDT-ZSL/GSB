"""Rollback execution, logical budgets, durable recovery, and idempotency."""

from __future__ import annotations

import os
import time
from typing import Any

from .cluster import app_matches_target, apply_operation, environment_apps, load_cluster_state
from .controller import Controller
from .errors import GitOpsError
from .planning import plan_environment
from .util import file_lock, process_alive, sha256_json

TERMINAL_OPERATION_STATUSES = {"abandoned", "committed", "failed"}
WAIT_INTERVAL_SECONDS = 0.05
MAX_WAIT_POLLS = 1200


def _operation_counts(operations: list[dict[str, Any]]) -> dict[str, int]:
    statuses = ("abandoned", "committed", "failed", "not_started", "pending", "running")
    return {
        status: sum(1 for operation in operations if operation["status"] == status)
        for status in statuses
    }


def _operation_output(operation: dict[str, Any]) -> dict[str, Any]:
    payload = operation["payload"]
    return {
        "action": payload["action"],
        "app": operation["app"],
        "idempotency_key": payload["idempotency_key"],
        "logical_end_ms": operation["logical_end_ms"],
        "logical_start_ms": operation["logical_start_ms"],
        "manifest_provenance": payload["manifest_provenance"],
        "outcome": operation["outcome"],
        "reason": operation["reason"],
        "sequence": operation["sequence"],
        "started": operation["started"],
        "status": operation["status"],
        "target": payload["target"],
        "target_digest": payload["target_digest"],
        "trigger": {
            "fields": payload["trigger"]["fields"],
            "kind": payload["trigger"]["kind"],
            "source_format": payload["trigger"]["source_format"],
            "source_id": payload["trigger"]["source_id"],
        },
        "upstream_rollback_condition": payload["upstream_rollback_condition"],
    }


def run_output(controller: Controller, run: dict[str, Any]) -> dict[str, Any]:
    operations = controller.store.get_operations(run["run_id"])
    return {
        "budget_ms": run["budget_ms"],
        "clock_end_ms": run["clock_end_ms"],
        "clock_start_ms": run["clock_start_ms"],
        "environment": run["environment"],
        "operation_cost_ms": run["operation_cost_ms"],
        "operation_counts": _operation_counts(operations),
        "operations": [_operation_output(operation) for operation in operations],
        "plan_id": run["plan_id"],
        "run_id": run["run_id"],
        "status": run["status"],
    }


def _run_id(
    environment: str,
    plan: dict[str, Any],
    budget_ms: int,
    operation_cost_ms: int,
    clock_start_ms: int,
) -> str:
    digest = sha256_json(
        {
            "budget_ms": budget_ms,
            "clock_start_ms": clock_start_ms,
            "environment": environment,
            "operation_cost_ms": operation_cost_ms,
            "plan_digest": plan["plan_digest"],
        }
    )
    return "run-" + digest[:24]


def _refresh_operation(
    controller: Controller,
    run_id: str,
    sequence: int,
) -> dict[str, Any]:
    operation = controller.store.get_operation(run_id, sequence)
    if operation is None:
        raise GitOpsError(
            "E_STATE_CONFLICT",
            "operation disappeared from the durable run",
            {"run_id": run_id, "sequence": sequence},
        )
    return operation


def _wait_or_claim(
    controller: Controller,
    run_id: str,
    sequence: int,
    *,
    owner_pid: int,
    start_clock_ms: int,
) -> dict[str, Any] | None:
    for _ in range(MAX_WAIT_POLLS):
        operation = _refresh_operation(controller, run_id, sequence)
        if operation["status"] in TERMINAL_OPERATION_STATUSES:
            return operation
        if operation["status"] == "running":
            owner = operation["owner_pid"]
            if owner and owner != owner_pid and process_alive(owner):
                time.sleep(WAIT_INTERVAL_SECONDS)
                continue
        if controller.store.claim_operation(
            run_id,
            sequence,
            owner_pid=owner_pid,
            start_clock_ms=start_clock_ms,
        ):
            return _refresh_operation(controller, run_id, sequence)
        time.sleep(WAIT_INTERVAL_SECONDS)
    raise GitOpsError(
        "E_CONCURRENT_OWNER",
        "operation is owned by another live controller",
        {"run_id": run_id, "sequence": sequence},
    )


def _upstream_state(
    operations: list[dict[str, Any]],
    operation: dict[str, Any],
) -> tuple[str, str | None]:
    by_app = {item["app"]: item for item in operations}
    blocked_reason: str | None = None
    waiting = False
    for upstream in operation["payload"]["target"]["depends_on"]:
        upstream_operation = by_app.get(upstream)
        if upstream_operation is None:
            continue
        status = upstream_operation["status"]
        if status == "committed":
            continue
        if status in ("failed", "abandoned", "not_started"):
            blocked_reason = f"upstream_not_committed:{upstream}"
            break
        waiting = True
    if blocked_reason is not None:
        return "blocked", blocked_reason
    if waiting:
        return "waiting", None
    return "ready", None


def _mark_budget_remainder(
    controller: Controller,
    run_id: str,
    operations: list[dict[str, Any]],
    start_sequence: int,
) -> None:
    for operation in operations:
        if operation["sequence"] < start_sequence:
            continue
        if operation["status"] in ("pending", "not_started"):
            controller.store.mark_operation(
                run_id,
                operation["sequence"],
                status="not_started",
                phase="not_started",
                started=False,
                outcome_status="not_started",
                reason="budget_exhausted",
                logical_end_ms=None,
            )


def execute_run(
    controller: Controller,
    run: dict[str, Any],
    *,
    budget_ms: int,
    operation_cost_ms: int,
    crash_after: int | None = None,
    fail_apps: set[str] | None = None,
    abandon_apps: set[str] | None = None,
) -> dict[str, Any]:
    fail_apps = fail_apps or set()
    abandon_apps = abandon_apps or set()
    run_id = run["run_id"]
    environment = run["environment"]
    owner_pid = os.getpid()
    current_clock = controller.store.get_env_clock(environment)
    deadline = current_clock + budget_ms
    applied_count = 0

    operations = controller.store.get_operations(run_id)
    for planned_operation in operations:
        sequence = planned_operation["sequence"]
        while True:
            run = controller.store.get_run(run_id)
            if run is None:
                raise GitOpsError(
                    "E_STATE_CONFLICT",
                    "durable rollback run disappeared",
                    {"run_id": run_id},
                )
            if run["status"] == "complete":
                return run_output(controller, run)
            operation = _refresh_operation(controller, run_id, sequence)
            operations = controller.store.get_operations(run_id)
            if operation["status"] in TERMINAL_OPERATION_STATUSES:
                if operation["logical_end_ms"] is not None:
                    current_clock = max(current_clock, operation["logical_end_ms"])
                break

            if operation["status"] == "running":
                owner = operation["owner_pid"]
                if owner and owner != owner_pid and process_alive(owner):
                    time.sleep(WAIT_INTERVAL_SECONDS)
                    continue
                payload = operation["payload"]
                cluster = load_cluster_state(controller.project)
                actual = environment_apps(cluster, environment).get(operation["app"])
                if app_matches_target(actual, payload):
                    start_clock = operation["logical_start_ms"] or current_clock
                    commit_clock = start_clock + operation_cost_ms
                    controller.store.finalize_commit(
                        run_id=run_id,
                        operation=operation,
                        committed_clock_ms=commit_clock,
                        result={
                            "app": operation["app"],
                            "target_digest": payload["target_digest"],
                        },
                    )
                    current_clock = max(current_clock, commit_clock)
                    break
                claimed = _wait_or_claim(
                    controller,
                    run_id,
                    sequence,
                    owner_pid=owner_pid,
                    start_clock_ms=current_clock,
                )
                if claimed is None or claimed["status"] in TERMINAL_OPERATION_STATUSES:
                    continue
                operation = claimed

            upstream_status, blocked_reason = _upstream_state(operations, operation)
            if upstream_status == "waiting":
                time.sleep(WAIT_INTERVAL_SECONDS)
                continue
            if upstream_status == "blocked":
                controller.store.mark_operation(
                    run_id,
                    sequence,
                    status="not_started",
                    phase="not_started",
                    started=False,
                    outcome_status="not_started",
                    reason=blocked_reason,
                    logical_end_ms=None,
                )
                break

            if operation["status"] != "running":
                if current_clock + operation_cost_ms > deadline:
                    _mark_budget_remainder(
                        controller,
                        run_id,
                        operations,
                        sequence,
                    )
                    current_clock = controller.store.get_env_clock(environment)
                    run = controller.store.get_run(run_id)
                    assert run is not None
                    controller.store.update_run_status(
                        run_id,
                        status="partial",
                        clock_end_ms=current_clock,
                        owner_pid=owner_pid,
                    )
                    run = controller.store.get_run(run_id)
                    assert run is not None
                    return run_output(controller, run)
                operation = _wait_or_claim(
                    controller,
                    run_id,
                    sequence,
                    owner_pid=owner_pid,
                    start_clock_ms=current_clock,
                )
                if operation is None:
                    continue
                if operation["status"] in TERMINAL_OPERATION_STATUSES:
                    continue

            operation = _refresh_operation(controller, run_id, sequence)
            if operation["status"] == "running":
                pass
            if operation["status"] != "running":
                continue
            start_clock = operation["logical_start_ms"]
            if start_clock is None:
                raise GitOpsError(
                    "E_STATE_CONFLICT",
                    "started operation is missing its logical start clock",
                    {"run_id": run_id, "sequence": sequence},
                )

            if operation["app"] in abandon_apps:
                abandon_clock = start_clock + operation_cost_ms
                controller.store.mark_operation(
                    run_id,
                    sequence,
                    status="abandoned",
                    phase="abandoned",
                    started=True,
                    outcome_status="abandoned",
                    reason="policy_abandoned",
                    logical_end_ms=abandon_clock,
                    advance_clock_to_ms=abandon_clock,
                )
                current_clock = max(current_clock, abandon_clock)
                break

            if operation["app"] in fail_apps:
                failure_clock = start_clock + operation_cost_ms
                controller.store.mark_operation(
                    run_id,
                    sequence,
                    status="failed",
                    phase="failed",
                    started=True,
                    outcome_status="failed_after_start",
                    reason="injected_failure",
                    logical_end_ms=failure_clock,
                    advance_clock_to_ms=failure_clock,
                )
                current_clock = max(current_clock, failure_clock)
                break

            payload = operation["payload"]
            apply_operation(
                controller.project,
                environment,
                payload,
            )
            applied_count += 1
            if crash_after is not None and applied_count == crash_after:
                # Simulate SIGKILL after the cluster write and before the
                # SQLite commit. Recovery must inspect the actual cluster.
                os._exit(75)
            commit_clock = start_clock + operation_cost_ms
            controller.store.finalize_commit(
                run_id=run_id,
                operation=operation,
                committed_clock_ms=commit_clock,
                result={
                    "app": operation["app"],
                    "target_digest": payload["target_digest"],
                },
            )
            current_clock = max(current_clock, commit_clock)
            break

    operations = controller.store.get_operations(run_id)
    current_clock = max(current_clock, controller.store.get_env_clock(environment))
    if all(operation["status"] == "committed" for operation in operations):
        status = "complete"
    elif any(operation["status"] == "failed" for operation in operations):
        status = "failed"
    elif any(operation["status"] == "abandoned" for operation in operations):
        status = "abandoned"
    else:
        status = "partial"
    controller.store.update_run_status(
        run_id,
        status=status,
        clock_end_ms=current_clock,
        owner_pid=owner_pid,
    )
    run = controller.store.get_run(run_id)
    assert run is not None
    return run_output(controller, run)


def rollback(
    controller: Controller,
    environment: str,
    *,
    budget_ms: int,
    operation_cost_ms: int,
    clock_ms: int | None = None,
    tolerance_ms: int | None = None,
    crash_after: int | None = None,
    fail_apps: set[str] | None = None,
    abandon_apps: set[str] | None = None,
) -> dict[str, Any]:
    lock_name = "rollback-" + sha256_json(environment)[:16] + ".lock"
    with file_lock(controller.project.runtime_db.parent / lock_name):
        return _rollback_locked(
            controller,
            environment,
            budget_ms=budget_ms,
            operation_cost_ms=operation_cost_ms,
            clock_ms=clock_ms,
            tolerance_ms=tolerance_ms,
            crash_after=crash_after,
            fail_apps=fail_apps,
            abandon_apps=abandon_apps,
        )


def _rollback_locked(
    controller: Controller,
    environment: str,
    *,
    budget_ms: int,
    operation_cost_ms: int,
    clock_ms: int | None = None,
    tolerance_ms: int | None = None,
    crash_after: int | None = None,
    fail_apps: set[str] | None = None,
    abandon_apps: set[str] | None = None,
) -> dict[str, Any]:
    controller.load(clock_ms=clock_ms, tolerance_ms=tolerance_ms)
    plan = plan_environment(controller.project, controller.store, environment)
    if not plan["operations"]:
        status = controller.status([environment])["environments"][environment]
        return {
            "converged": status["converged"],
            "environment": environment,
            "format": "rollback-result-v1",
            "idempotent": True,
            "kind": "rollback",
            "ok": True,
            "plan": {
                "operation_count": 0,
                "plan_digest": plan["plan_digest"],
                "plan_id": plan["plan_id"],
                "version_checks": plan["version_checks"],
            },
            "run": None,
            "schema_version": 1,
        }

    existing = controller.store.get_latest_incomplete_run(environment)
    if existing is not None and existing["plan_digest"] == plan["plan_digest"]:
        run = existing
        resumed_existing = True
    else:
        clock_start = controller.store.get_env_clock(environment)
        run_id = _run_id(
            environment,
            plan,
            budget_ms,
            operation_cost_ms,
            clock_start,
        )
        run = controller.store.create_or_get_run(
            run_id=run_id,
            plan=plan,
            operations=plan["operations"],
            budget_ms=budget_ms,
            operation_cost_ms=operation_cost_ms,
            clock_start_ms=clock_start,
            owner_pid=os.getpid(),
        )
        resumed_existing = run["status"] != "running"
    executed = execute_run(
        controller,
        run,
        budget_ms=budget_ms,
        operation_cost_ms=operation_cost_ms,
        crash_after=crash_after,
        fail_apps=fail_apps,
        abandon_apps=abandon_apps,
    )
    status = controller.status([environment])["environments"][environment]
    return {
        "converged": status["converged"],
        "environment": environment,
        "format": "rollback-result-v1",
        "idempotent": resumed_existing,
        "kind": "rollback",
        "ok": True,
        "plan": {
            "operation_count": len(plan["operations"]),
            "plan_digest": plan["plan_digest"],
            "plan_id": plan["plan_id"],
            "version_checks": plan["version_checks"],
        },
        "run": executed,
        "schema_version": 1,
    }


def resume(
    controller: Controller,
    environment: str,
    *,
    budget_ms: int | None = None,
    operation_cost_ms: int | None = None,
    clock_ms: int | None = None,
    tolerance_ms: int | None = None,
    crash_after: int | None = None,
    fail_apps: set[str] | None = None,
    abandon_apps: set[str] | None = None,
) -> dict[str, Any]:
    lock_name = "rollback-" + sha256_json(environment)[:16] + ".lock"
    with file_lock(controller.project.runtime_db.parent / lock_name):
        return _resume_locked(
            controller,
            environment,
            budget_ms=budget_ms,
            operation_cost_ms=operation_cost_ms,
            clock_ms=clock_ms,
            tolerance_ms=tolerance_ms,
            crash_after=crash_after,
            fail_apps=fail_apps,
            abandon_apps=abandon_apps,
        )


def _resume_locked(
    controller: Controller,
    environment: str,
    *,
    budget_ms: int | None = None,
    operation_cost_ms: int | None = None,
    clock_ms: int | None = None,
    tolerance_ms: int | None = None,
    crash_after: int | None = None,
    fail_apps: set[str] | None = None,
    abandon_apps: set[str] | None = None,
) -> dict[str, Any]:
    controller.load(clock_ms=clock_ms, tolerance_ms=tolerance_ms)
    run = controller.store.get_latest_incomplete_run(environment)
    if run is None:
        latest = controller.store.get_latest_run(environment)
        return {
            "environment": environment,
            "format": "resume-result-v1",
            "kind": "resume",
            "ok": True,
            "resumed": False,
            "run": run_output(controller, latest) if latest is not None else None,
            "schema_version": 1,
        }
    effective_budget = run["budget_ms"] if budget_ms is None else budget_ms
    effective_cost = (
        run["operation_cost_ms"] if operation_cost_ms is None else operation_cost_ms
    )
    executed = execute_run(
        controller,
        run,
        budget_ms=effective_budget,
        operation_cost_ms=effective_cost,
        crash_after=crash_after,
        fail_apps=fail_apps,
        abandon_apps=abandon_apps,
    )
    status = controller.status([environment])["environments"][environment]
    return {
        "converged": status["converged"],
        "environment": environment,
        "format": "resume-result-v1",
        "kind": "resume",
        "ok": True,
        "resumed": True,
        "run": executed,
        "schema_version": 1,
    }
