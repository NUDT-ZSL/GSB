from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable

from .aggregation import AggregateResult, aggregate_metric
from .decision import RuleRuntime, WorkloadDecision, decide_workload
from .errors import BusinessError, fail
from .locks import AdvisoryFileLock
from .model import (
    ActionOutcomeRule,
    PolicyBundle,
    parse_bundle,
    parse_outcomes,
    parse_samples,
    load_json,
)
from .patch import (
    COOLDOWN_AFFECTING_RULE_FIELDS,
    apply_patch_in_memory,
    build_invalidation_report,
    parse_patch_document,
)
from .snapshot import current_status, publish_snapshot, verify_state
from .storage import (
    assert_config_version,
    canonical_json,
    checksum_json,
    connect,
    ensure_schema,
    get_int_meta,
    immediate_transaction,
    initialize_state,
    insert_or_replace_policy,
    load_policy,
    load_samples,
    set_meta,
)


def _lock_for(db_file: str | Path) -> AdvisoryFileLock:
    return AdvisoryFileLock(str(Path(db_file).expanduser().resolve()) + ".lock")


def _normalize_scope(
    policy: PolicyBundle,
    workload_ids: Iterable[str] | None,
) -> list[str]:
    if workload_ids is None:
        return sorted(policy.workloads)
    scope = sorted(set(workload_ids))
    if not scope:
        return sorted(policy.workloads)
    missing = [workload_id for workload_id in scope if workload_id not in policy.workloads]
    if missing:
        fail(
            "E_WORKLOAD_NOT_FOUND",
            "one or more scoped workloads do not exist",
            {"workloadIds": missing},
        )
    return scope


def _scope_key(scope: list[str]) -> str:
    digest = hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()[:12]
    return f"{len(scope)}-{digest}"


def _round_id(logical_now_ms: int, config_version: int, scope: list[str]) -> str:
    return (
        f"round-{logical_now_ms:012d}-c{config_version:08d}-"
        f"{_scope_key(scope)}"
    )


def _resolve_at(connection: sqlite3.Connection, at: int | None) -> int:
    if at is not None:
        if isinstance(at, bool) or not isinstance(at, int) or at < 0:
            fail("E_INVALID_CLOCK", "logical clock must be a non-negative integer")
        return at
    row = connection.execute("SELECT MAX(timestamp_ms) AS at FROM samples").fetchone()
    if row is None or row["at"] is None:
        return 0
    return int(row["at"])


def _check_clock(
    connection: sqlite3.Connection,
    policy: PolicyBundle,
    logical_now_ms: int,
) -> None:
    current = get_int_meta(connection, "logical_clock_ms")
    tolerance = policy.budget.clock_rollback_tolerance_ms
    if logical_now_ms < current - tolerance:
        fail(
            "E_CLOCK_ROLLBACK",
            "logical clock moved backwards beyond the configured tolerance",
            {
                "currentLogicalClockMs": current,
                "requestedLogicalClockMs": logical_now_ms,
                "toleranceMs": tolerance,
            },
        )


def _sample_watermark(connection: sqlite3.Connection, logical_now_ms: int) -> int:
    row = connection.execute(
        "SELECT MAX(timestamp_ms) AS watermark FROM samples WHERE timestamp_ms <= ?",
        (logical_now_ms,),
    ).fetchone()
    return 0 if row is None or row["watermark"] is None else int(row["watermark"])


def _decision_row_payload(decision: WorkloadDecision) -> dict[str, Any]:
    return decision.to_json()


def _round_payload(
    *,
    round_id: str,
    logical_now_ms: int,
    config_version: int,
    scope: list[str],
    aggregates: list[AggregateResult],
    decisions: list[WorkloadDecision],
) -> dict[str, Any]:
    return {
        "roundId": round_id,
        "logicalNowMs": logical_now_ms,
        "configVersion": config_version,
        "scope": scope,
        "aggregates": [
            aggregate.to_json()
            for aggregate in sorted(
                aggregates,
                key=lambda item: item.metric_source_id,
            )
        ],
        "decisions": [
            decision.to_json()
            for decision in sorted(decisions, key=lambda item: item.workload_id)
        ],
        "scheduler": None,
    }


def _load_rule_runtimes(
    connection: sqlite3.Connection,
    rule_ids: Iterable[str],
) -> dict[str, RuleRuntime]:
    ids = sorted(set(rule_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM rule_state WHERE rule_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {
        row["rule_id"]: RuleRuntime(
            cooldown_until_ms=int(row["cooldown_until_ms"]),
            backoff_until_ms=int(row["backoff_until_ms"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_success_ms=row["last_success_ms"],
            last_failure_ms=row["last_failure_ms"],
        )
        for row in rows
    }


def _ensure_round(
    connection: sqlite3.Connection,
    *,
    logical_now_ms: int,
    scope: list[str],
) -> tuple[dict[str, Any], bool]:
    policy = load_policy(connection)
    _check_clock(connection, policy, logical_now_ms)
    config_version = assert_config_version(
        connection,
        get_int_meta(connection, "config_version"),
    )
    round_id = _round_id(logical_now_ms, config_version, scope)
    existing = connection.execute(
        "SELECT * FROM rounds WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    if existing is not None:
        return json.loads(existing["payload_json"]), True

    scoped_rules = {
        rule.id: rule
        for rule in policy.rules.values()
        if rule.workload_id in set(scope)
    }
    metric_ids = sorted(
        {rule.metric_source_id for rule in scoped_rules.values()}
    )
    samples = load_samples(connection)
    aggregates: dict[str, AggregateResult] = {}
    for metric_id in metric_ids:
        try:
            aggregates[metric_id] = aggregate_metric(
                policy.metrics[metric_id],
                samples,
                logical_now_ms,
            )
        except BusinessError as exc:
            if exc.code != "E_EMPTY_WINDOW":
                raise
    runtimes = _load_rule_runtimes(connection, scoped_rules.keys())

    decisions: list[WorkloadDecision] = []
    for workload_id in scope:
        workload = policy.workloads[workload_id]
        state_row = connection.execute(
            "SELECT replicas FROM workload_state WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        if state_row is None:
            fail(
                "E_INVALID_STATE",
                "workload state is missing",
                {"workloadId": workload_id},
            )
        rules = [
            rule
            for rule in scoped_rules.values()
            if rule.workload_id == workload_id
        ]
        decisions.append(
            decide_workload(
                workload=workload,
                current_replicas=int(state_row["replicas"]),
                rules=rules,
                metrics=policy.metrics,
                aggregates=aggregates,
                runtimes=runtimes,
                budget=policy.budget,
                logical_now_ms=logical_now_ms,
            )
        )

    payload = _round_payload(
        round_id=round_id,
        logical_now_ms=logical_now_ms,
        config_version=config_version,
        scope=scope,
        aggregates=list(aggregates.values()),
        decisions=decisions,
    )
    current_state_version = get_int_meta(connection, "state_version")
    connection.execute(
        """
        INSERT INTO rounds(
            round_id, logical_now_ms, config_version, scope_key,
            scope_json, status, payload_json, state_version
        ) VALUES(?, ?, ?, ?, ?, 'decided', ?, ?)
        """,
        (
            round_id,
            logical_now_ms,
            config_version,
            _scope_key(scope),
            canonical_json(scope),
            canonical_json(payload),
            current_state_version,
        ),
    )
    for decision in decisions:
        decision_payload = _decision_row_payload(decision)
        decision_json = canonical_json(decision_payload)
        connection.execute(
            """
            INSERT INTO decisions(
                round_id, workload_id, selected_rule_id, direction,
                from_replicas, to_replicas, rule_version,
                candidate_replicas, status, reason, config_version,
                payload_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                decision.workload_id,
                decision.selected_rule_id,
                decision.direction,
                decision.from_replicas,
                decision.target_replicas,
                decision.selected_rule_version,
                decision.candidate_replicas,
                decision.status,
                decision.reason,
                config_version,
                decision_json,
            ),
        )
        connection.execute(
            """
            INSERT INTO decision_cache(
                workload_id, round_id, config_version,
                decision_json, checksum, updated_state_version
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(workload_id) DO UPDATE SET
                round_id = excluded.round_id,
                config_version = excluded.config_version,
                decision_json = excluded.decision_json,
                checksum = excluded.checksum,
                updated_state_version = excluded.updated_state_version
            """,
            (
                decision.workload_id,
                round_id,
                config_version,
                decision_json,
                checksum_json(decision_payload),
                current_state_version,
            ),
        )
    for aggregate in aggregates.values():
        aggregate_payload = aggregate.to_json()
        connection.execute(
            """
            INSERT INTO aggregate_cache(
                metric_source_id, window_end_ms, payload_json, checksum
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(metric_source_id, window_end_ms) DO UPDATE SET
                payload_json = excluded.payload_json,
                checksum = excluded.checksum
            """,
            (
                aggregate.metric_source_id,
                aggregate.window_end_ms,
                canonical_json(aggregate_payload),
                checksum_json(aggregate_payload),
            ),
        )
    watermark = max(
        get_int_meta(connection, "sample_watermark_ms"),
        _sample_watermark(connection, logical_now_ms),
    )
    logical_clock = max(
        get_int_meta(connection, "logical_clock_ms"),
        logical_now_ms,
    )
    set_meta(connection, "sample_watermark_ms", watermark)
    set_meta(connection, "logical_clock_ms", logical_clock)
    return payload, False


def _action_id(
    *,
    round_id: str,
    workload_id: str,
    rule_id: str,
    rule_version: int,
    direction: str,
    from_replicas: int,
    to_replicas: int,
) -> str:
    key = (
        f"{round_id}|{workload_id}|{rule_id}|{rule_version}|{direction}|"
        f"{from_replicas}|{to_replicas}"
    )
    return "act-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _planned_outcome(
    outcomes: dict[tuple[str, int], ActionOutcomeRule],
    rule_id: str,
    logical_now_ms: int,
) -> tuple[str, str]:
    item = outcomes.get((rule_id, logical_now_ms))
    if item is None:
        return "success", "simulated action completed"
    return item.outcome, item.reason


def _action_duration(
    budget: Any,
    delta_replicas: int,
) -> int:
    return (
        budget.action_base_duration_ms
        + budget.action_duration_per_replica_ms * delta_replicas
    )


def _insert_action(
    connection: sqlite3.Connection,
    *,
    action_id: str,
    round_id: str,
    workload_id: str,
    rule_id: str,
    rule_version: int,
    direction: str,
    from_replicas: int,
    to_replicas: int,
    duration_ms: int,
    core_cost_units: int,
    memory_mib: int,
    priority: int,
    status: str,
    reason: str | None,
    planned_outcome: str,
    planned_reason: str,
    logical_now_ms: int,
) -> None:
    connection.execute(
        """
        INSERT INTO actions(
            action_id, round_id, workload_id, rule_id, rule_version,
            direction, from_replicas, to_replicas, delta_replicas,
            duration_ms, core_cost_units, memory_mib, priority,
            status, reason, planned_outcome, planned_reason,
            created_logical_ms
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            round_id,
            workload_id,
            rule_id,
            rule_version,
            direction,
            from_replicas,
            to_replicas,
            abs(to_replicas - from_replicas),
            duration_ms,
            core_cost_units,
            memory_mib,
            priority,
            status,
            reason,
            planned_outcome,
            planned_reason,
            logical_now_ms,
        ),
    )


def _plan_actions(
    connection: sqlite3.Connection,
    round_payload: dict[str, Any],
) -> tuple[list[str], bool]:
    round_id = round_payload["roundId"]
    existing_actions = connection.execute(
        """
        SELECT action_id, status FROM actions
        WHERE round_id = ?
        ORDER BY priority, action_id
        """,
        (round_id,),
    ).fetchall()
    if existing_actions:
        return [
            row["action_id"]
            for row in existing_actions
            if row["status"] in {"reserved", "started"}
        ], True

    policy = load_policy(connection)
    budget = policy.budget
    budget_state = connection.execute(
        "SELECT * FROM budget_state WHERE id = 'budget'"
    ).fetchone()
    if budget_state is None:
        fail("E_INVALID_STATE", "budget state is missing")
    core_remaining = budget.pool_core_units - int(budget_state["core_used_units"])
    memory_remaining = budget.memory_limit_mib - int(
        budget_state["memory_reserved_mib"]
    )
    round_time_remaining = budget.round_time_budget_ms
    outcomes = {
        (item.rule_id, item.logical_now_ms): item
        for item in (
            ActionOutcomeRule(
                rule_id=row["rule_id"],
                logical_now_ms=row["logical_now_ms"],
                outcome=row["outcome"],
                reason=row["reason"],
            )
            for row in connection.execute(
                "SELECT * FROM action_outcome_rules"
            ).fetchall()
        )
    }
    inflight_rows = connection.execute(
        """
        SELECT workload_id, COUNT(*) AS count
        FROM actions
        WHERE status IN ('reserved', 'started')
        GROUP BY workload_id
        """
    ).fetchall()
    inflight_by_workload = {
        row["workload_id"]: int(row["count"]) for row in inflight_rows
    }

    decisions = sorted(
        [
            decision
            for decision in round_payload["decisions"]
            if decision["status"] == "action"
        ],
        key=lambda decision: (
            policy.rules[decision["selectedRuleId"]].priority,
            decision["workloadId"],
            decision["selectedRuleId"],
        ),
    )
    planned_action_ids: list[str] = []
    budget_cutoff = False
    cutoff_reason: str | None = None
    for decision in decisions:
        workload_id = decision["workloadId"]
        rule_id = decision["selectedRuleId"]
        rule = policy.rules[rule_id]
        workload = policy.workloads[workload_id]
        workload_state = connection.execute(
            "SELECT replicas FROM workload_state WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        current_replicas = int(workload_state["replicas"])
        from_replicas = int(decision["fromReplicas"])
        to_replicas = int(decision["targetReplicas"])
        delta = abs(to_replicas - from_replicas)
        duration_ms = _action_duration(budget, delta)
        core_cost = (
            delta
            * workload.cpu_millicores_per_replica
            * duration_ms
        )
        memory_mib = delta * workload.memory_mib_per_replica
        action_id = _action_id(
            round_id=round_id,
            workload_id=workload_id,
            rule_id=rule_id,
            rule_version=int(decision["selectedRuleVersion"]),
            direction=decision["direction"],
            from_replicas=from_replicas,
            to_replicas=to_replicas,
        )
        outcome, outcome_reason = _planned_outcome(
            outcomes,
            rule_id,
            int(round_payload["logicalNowMs"]),
        )
        if budget_cutoff:
            _insert_action(
                connection,
                action_id=action_id,
                round_id=round_id,
                workload_id=workload_id,
                rule_id=rule_id,
                rule_version=int(decision["selectedRuleVersion"]),
                direction=decision["direction"],
                from_replicas=from_replicas,
                to_replicas=to_replicas,
                duration_ms=duration_ms,
                core_cost_units=core_cost,
                memory_mib=memory_mib,
                priority=rule.priority,
                status="abandoned",
                reason="dropped_after_higher_priority_budget_cutoff",
                planned_outcome=outcome,
                planned_reason=outcome_reason,
                logical_now_ms=int(round_payload["logicalNowMs"]),
            )
            continue
        if current_replicas != from_replicas:
            _insert_action(
                connection,
                action_id=action_id,
                round_id=round_id,
                workload_id=workload_id,
                rule_id=rule_id,
                rule_version=int(decision["selectedRuleVersion"]),
                direction=decision["direction"],
                from_replicas=from_replicas,
                to_replicas=to_replicas,
                duration_ms=duration_ms,
                core_cost_units=core_cost,
                memory_mib=memory_mib,
                priority=rule.priority,
                status="abandoned",
                reason="stale_decision_after_replica_state_change",
                planned_outcome=outcome,
                planned_reason=outcome_reason,
                logical_now_ms=int(round_payload["logicalNowMs"]),
            )
            continue
        if (
            inflight_by_workload.get(workload_id, 0)
            >= budget.max_concurrent_actions_per_workload
        ):
            _insert_action(
                connection,
                action_id=action_id,
                round_id=round_id,
                workload_id=workload_id,
                rule_id=rule_id,
                rule_version=int(decision["selectedRuleVersion"]),
                direction=decision["direction"],
                from_replicas=from_replicas,
                to_replicas=to_replicas,
                duration_ms=duration_ms,
                core_cost_units=core_cost,
                memory_mib=memory_mib,
                priority=rule.priority,
                status="not_started",
                reason="workload_concurrency_limit",
                planned_outcome=outcome,
                planned_reason=outcome_reason,
                logical_now_ms=int(round_payload["logicalNowMs"]),
            )
            continue

        constraint_reason: str | None = None
        if core_cost > core_remaining:
            constraint_reason = "core_hours_budget_exhausted"
        elif memory_mib > memory_remaining:
            constraint_reason = "memory_budget_exhausted"
        elif duration_ms > round_time_remaining:
            constraint_reason = "round_time_budget_exhausted"
        if constraint_reason is not None:
            budget_cutoff = True
            cutoff_reason = constraint_reason
            _insert_action(
                connection,
                action_id=action_id,
                round_id=round_id,
                workload_id=workload_id,
                rule_id=rule_id,
                rule_version=int(decision["selectedRuleVersion"]),
                direction=decision["direction"],
                from_replicas=from_replicas,
                to_replicas=to_replicas,
                duration_ms=duration_ms,
                core_cost_units=core_cost,
                memory_mib=memory_mib,
                priority=rule.priority,
                status="budget_not_started",
                reason=constraint_reason,
                planned_outcome=outcome,
                planned_reason=outcome_reason,
                logical_now_ms=int(round_payload["logicalNowMs"]),
            )
            continue

        _insert_action(
            connection,
            action_id=action_id,
            round_id=round_id,
            workload_id=workload_id,
            rule_id=rule_id,
            rule_version=int(decision["selectedRuleVersion"]),
            direction=decision["direction"],
            from_replicas=from_replicas,
            to_replicas=to_replicas,
            duration_ms=duration_ms,
            core_cost_units=core_cost,
            memory_mib=memory_mib,
            priority=rule.priority,
            status="reserved",
            reason=None,
            planned_outcome=outcome,
            planned_reason=outcome_reason,
            logical_now_ms=int(round_payload["logicalNowMs"]),
        )
        connection.execute(
            """
            UPDATE budget_state
            SET core_used_units = core_used_units + ?,
                memory_reserved_mib = memory_reserved_mib + ?
            WHERE id = 'budget'
            """,
            (core_cost, memory_mib),
        )
        core_remaining -= core_cost
        memory_remaining -= memory_mib
        round_time_remaining -= duration_ms
        inflight_by_workload[workload_id] = (
            inflight_by_workload.get(workload_id, 0) + 1
        )
        planned_action_ids.append(action_id)

    plan = {
        "budgetCutoff": budget_cutoff,
        "cutoffReason": cutoff_reason,
        "plannedActionIds": planned_action_ids,
        "reservedCoreUnits": (
            budget.pool_core_units
            - int(budget_state["core_used_units"])
            - core_remaining
        ),
        "reservedMemoryMiB": (
            budget.memory_limit_mib
            - int(budget_state["memory_reserved_mib"])
            - memory_remaining
        ),
    }
    round_payload["schedulerPlan"] = plan
    connection.execute(
        """
        UPDATE rounds
        SET status = 'dispatched', payload_json = ?
        WHERE round_id = ?
        """,
        (canonical_json(round_payload), round_id),
    )
    return planned_action_ids, False


def _backoff_delay(
    budget: Any,
    *,
    failures: int,
    rule_id: str,
    action_id: str,
    logical_now_ms: int,
) -> tuple[int, int]:
    exponential = budget.failure_backoff_base_ms * (2 ** max(0, failures - 1))
    capped = min(exponential, budget.failure_backoff_max_ms)
    jitter_span = int(
        (Decimal(capped) * budget.failure_backoff_jitter_ratio).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    digest = hashlib.sha256(
        f"{rule_id}|{action_id}|{failures}|{logical_now_ms}".encode("utf-8")
    ).digest()
    jitter = (
        0
        if jitter_span <= 0
        else int.from_bytes(digest[:8], "big") % (jitter_span + 1)
    )
    delay = min(capped + jitter, budget.failure_backoff_max_ms)
    return delay, jitter


def _action_summary(row: sqlite3.Row) -> dict[str, Any]:
    result = None if row["result_json"] is None else json.loads(row["result_json"])
    return {
        "actionId": row["action_id"],
        "workloadId": row["workload_id"],
        "ruleId": row["rule_id"],
        "ruleVersion": int(row["rule_version"]),
        "direction": row["direction"],
        "fromReplicas": int(row["from_replicas"]),
        "toReplicas": int(row["to_replicas"]),
        "status": row["status"],
        "reason": row["reason"],
        "result": result,
        "durationMs": int(row["duration_ms"]),
        "coreCostUnits": int(row["core_cost_units"]),
        "memoryMiB": int(row["memory_mib"]),
    }


def _fault(name: str) -> None:
    if os.environ.get("ELASTICCTL_FAULT") == name:
        os._exit(
            {
                "after_reservation": 86,
                "after_start": 87,
                "before_effect_commit": 88,
                "after_effect_commit": 89,
            }.get(name, 90)
        )


def _execute_action(db_file: str | Path, action_id: str) -> dict[str, Any]:
    connection = connect(db_file)
    try:
        with immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                fail("E_ACTION_NOT_FOUND", "action does not exist", {"actionId": action_id})
            status = row["status"]
            if status in {"succeeded", "failed", "not_started", "budget_not_started", "abandoned"}:
                return _action_summary(row)
            if status not in {"reserved", "started"}:
                fail(
                    "E_INVALID_STATE",
                    "action has an unsupported status",
                    {"actionId": action_id, "status": status},
                )
            if status == "reserved":
                connection.execute(
                    """
                    UPDATE actions
                    SET status = 'started', started_logical_ms = created_logical_ms
                    WHERE action_id = ? AND status = 'reserved'
                    """,
                    (action_id,),
                )
                row = connection.execute(
                    "SELECT * FROM actions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
        _fault("after_start")
        _fault("before_effect_commit")

        planned_outcome = row["planned_outcome"]
        planned_reason = row["planned_reason"]
        with immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                fail("E_ACTION_NOT_FOUND", "action does not exist", {"actionId": action_id})
            if row["status"] in {"succeeded", "failed"}:
                return _action_summary(row)
            if row["status"] in {
                "not_started",
                "budget_not_started",
                "abandoned",
            }:
                return _action_summary(row)
            policy = load_policy(connection)
            budget = policy.budget
            workload_id = row["workload_id"]
            rule_id = row["rule_id"]
            rule = policy.rules.get(rule_id)
            if rule is None:
                raise RuntimeError(f"rule {rule_id} disappeared while action was in flight")
            state_row = connection.execute(
                "SELECT replicas FROM workload_state WHERE workload_id = ?",
                (workload_id,),
            ).fetchone()
            if state_row is None:
                raise RuntimeError(
                    f"workload state {workload_id} disappeared while action was in flight"
                )
            current_replicas = int(state_row["replicas"])
            from_replicas = int(row["from_replicas"])
            to_replicas = int(row["to_replicas"])
            logical_now_ms = int(row["created_logical_ms"])
            action_ok = planned_outcome == "success" and current_replicas == from_replicas
            failure_reason = planned_reason
            if planned_outcome == "success" and current_replicas != from_replicas:
                action_ok = False
                failure_reason = (
                    "stale_replica_precondition:"
                    f"expected={from_replicas},actual={current_replicas}"
                )
            budget_row = connection.execute(
                "SELECT * FROM budget_state WHERE id = 'budget'"
            ).fetchone()
            reserved_memory = int(row["memory_mib"])
            if int(budget_row["memory_reserved_mib"]) < reserved_memory:
                raise RuntimeError("memory reservation ledger underflow")
            connection.execute(
                """
                UPDATE budget_state
                SET memory_reserved_mib = memory_reserved_mib - ?
                WHERE id = 'budget'
                """,
                (reserved_memory,),
            )
            current_state_version = get_int_meta(connection, "state_version")
            new_state_version = current_state_version + 1
            if action_ok:
                connection.execute(
                    """
                    UPDATE workload_state
                    SET replicas = ?, state_version = ?
                    WHERE workload_id = ?
                    """,
                    (to_replicas, new_state_version, workload_id),
                )
                connection.execute(
                    """
                    INSERT INTO replica_ledger(
                        action_id, workload_id, delta, replicas_after,
                        state_version
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        workload_id,
                        to_replicas - from_replicas,
                        to_replicas,
                        new_state_version,
                    ),
                )
                connection.execute(
                    """
                    UPDATE rule_state
                    SET cooldown_until_ms = ?,
                        backoff_until_ms = 0,
                        consecutive_failures = 0,
                        last_success_ms = ?
                    WHERE rule_id = ?
                    """,
                    (
                        logical_now_ms + rule.cooldown_ms,
                        logical_now_ms,
                        rule_id,
                    ),
                )
                result = {
                    "outcome": "success",
                    "replicasBefore": from_replicas,
                    "replicasAfter": to_replicas,
                }
                connection.execute(
                    """
                    INSERT INTO action_effects(
                        action_id, applied, replicas_after, result_json
                    ) VALUES(?, 1, ?, ?)
                    """,
                    (action_id, to_replicas, canonical_json(result)),
                )
                connection.execute(
                    """
                    UPDATE actions
                    SET status = 'succeeded',
                        result_json = ?,
                        finished_state_version = ?
                    WHERE action_id = ?
                    """,
                    (canonical_json(result), new_state_version, action_id),
                )
                connection.execute(
                    "DELETE FROM decision_cache WHERE workload_id = ?",
                    (workload_id,),
                )
            else:
                runtime = connection.execute(
                    "SELECT * FROM rule_state WHERE rule_id = ?",
                    (rule_id,),
                ).fetchone()
                failures = int(runtime["consecutive_failures"]) + 1
                delay, jitter = _backoff_delay(
                    budget,
                    failures=failures,
                    rule_id=rule_id,
                    action_id=action_id,
                    logical_now_ms=logical_now_ms,
                )
                backoff_until = logical_now_ms + delay
                connection.execute(
                    """
                    UPDATE rule_state
                    SET cooldown_until_ms = 0,
                        backoff_until_ms = ?,
                        consecutive_failures = ?,
                        last_failure_ms = ?
                    WHERE rule_id = ?
                    """,
                    (backoff_until, failures, logical_now_ms, rule_id),
                )
                result = {
                    "outcome": "failure",
                    "reason": failure_reason,
                    "consecutiveFailures": failures,
                    "backoffDelayMs": delay,
                    "jitterMs": jitter,
                    "backoffUntilMs": backoff_until,
                }
                connection.execute(
                    """
                    UPDATE actions
                    SET status = 'failed',
                        result_json = ?,
                        finished_state_version = ?
                    WHERE action_id = ?
                    """,
                    (canonical_json(result), new_state_version, action_id),
                )
                connection.execute(
                    "DELETE FROM decision_cache WHERE workload_id = ?",
                    (workload_id,),
                )
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (row["round_id"],),
            ).fetchone()
            round_id = None if round_row is None else round_row["round_id"]
            publish_snapshot(
                connection,
                expected_state_version=current_state_version,
                round_id=round_id,
                increment=True,
            )
            _fault("before_effect_commit")
        _fault("after_effect_commit")
        final = connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return _action_summary(final)
    finally:
        connection.close()


def _round_summary(
    connection: sqlite3.Connection,
    round_id: str,
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT * FROM actions WHERE round_id = ? ORDER BY priority, action_id",
        (round_id,),
    ).fetchall()
    succeeded = [_action_summary(row) for row in rows if row["status"] == "succeeded"]
    failed = [_action_summary(row) for row in rows if row["status"] == "failed"]
    not_started = [
        _action_summary(row)
        for row in rows
        if row["status"] in {"not_started", "budget_not_started"}
    ]
    abandoned = [_action_summary(row) for row in rows if row["status"] == "abandoned"]
    unconfirmed = [
        _action_summary(row)
        for row in rows
        if row["status"] in {"reserved", "started"}
    ]
    return {
        "startedSucceeded": succeeded,
        "failedAfterStart": failed,
        "notStarted": not_started,
        "activelyAbandoned": abandoned,
        "unconfirmed": unconfirmed,
        "counts": {
            "succeeded": len(succeeded),
            "failed": len(failed),
            "notStarted": len(not_started),
            "abandoned": len(abandoned),
            "unconfirmed": len(unconfirmed),
        },
    }


def _finalize_round(
    connection: sqlite3.Connection,
    round_id: str,
) -> dict[str, Any]:
    round_row = connection.execute(
        "SELECT * FROM rounds WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    if round_row is None:
        fail("E_ROUND_NOT_FOUND", "round does not exist", {"roundId": round_id})
    payload = json.loads(round_row["payload_json"])
    summary = _round_summary(connection, round_id)
    payload["scheduler"] = summary
    status = "completed" if not summary["unconfirmed"] else "dispatched"
    if (
        round_row["status"] == status
        and payload.get("scheduler") == summary
        and json.loads(round_row["payload_json"]).get("scheduler") == summary
        and status == "completed"
    ):
        return payload
    current_state_version = get_int_meta(connection, "state_version")
    connection.execute(
        """
        UPDATE rounds
        SET status = ?, payload_json = ?
        WHERE round_id = ?
        """,
        (status, canonical_json(payload), round_id),
    )
    snapshot = publish_snapshot(
        connection,
        expected_state_version=current_state_version,
        round_id=round_id,
        increment=True,
    )
    connection.execute(
        "UPDATE rounds SET state_version = ? WHERE round_id = ?",
        (snapshot["stateVersion"], round_id),
    )
    return payload


def _run_actions(
    db_file: str | Path,
    action_ids: list[str],
    *,
    workers: int,
) -> list[dict[str, Any]]:
    if not action_ids:
        return []
    max_workers = max(1, min(workers, len(action_ids)))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_execute_action, db_file, action_id)
            for action_id in action_ids
        ]
        for future in futures:
            results.append(future.result())
    results.sort(key=lambda item: item["actionId"])
    return results


def load_database(
    db_file: str | Path,
    *,
    workloads_path: str | Path,
    metrics_path: str | Path,
    rules_path: str | Path,
    budget_path: str | Path,
    samples_path: str | Path,
    outcomes_path: str | Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    workloads_payload = load_json(workloads_path)
    metrics_payload = load_json(metrics_path)
    rules_payload = load_json(rules_path)
    budget_payload = load_json(budget_path)
    bundle = parse_bundle(
        workloads_payload,
        metrics_payload,
        rules_payload,
        budget_payload,
    )
    samples = parse_samples(load_json(samples_path), bundle.metrics)
    outcomes: list[ActionOutcomeRule] = []
    if outcomes_path is not None:
        outcomes = parse_outcomes(load_json(outcomes_path), bundle.rules)

    with _lock_for(db_file):
        connection = connect(db_file, create=True)
        try:
            ensure_schema(connection)
            with immediate_transaction(connection):
                initialize_state(
                    connection,
                    bundle,
                    samples,
                    outcomes,
                    replace=replace,
                )
                state_version = get_int_meta(connection, "state_version")
                snapshot = publish_snapshot(
                    connection,
                    expected_state_version=state_version,
                    increment=False,
                )
            return {
                "ok": True,
                "database": str(Path(db_file).expanduser().resolve()),
                "configVersion": get_int_meta(connection, "config_version"),
                "stateVersion": snapshot["stateVersion"],
                "snapshotId": snapshot["snapshotId"],
                "counts": {
                    "workloads": len(bundle.workloads),
                    "metricSources": len(bundle.metrics),
                    "scalingRules": len(bundle.rules),
                    "samples": len(samples),
                    "actionOutcomes": len(outcomes),
                },
            }
        finally:
            connection.close()


def apply_patches(db_file: str | Path, patch_path: str | Path) -> dict[str, Any]:
    document = parse_patch_document(load_json(patch_path))
    document_checksum = checksum_json(
        {
            "patchId": document.patch_id,
            "baseConfigVersion": document.base_config_version,
            "operations": [
                {
                    "operationId": operation.operation_id,
                    "op": operation.op,
                    "objectType": operation.object_type,
                    "objectId": operation.object_id,
                    "changes": operation.changes,
                    "object": operation.object_payload,
                    "expectedVersion": operation.expected_version,
                }
                for operation in document.operations
            ],
        }
    )
    with _lock_for(db_file):
        connection = connect(db_file)
        try:
            ensure_schema(connection)
            with immediate_transaction(connection):
                history = connection.execute(
                    "SELECT * FROM patch_history WHERE patch_id = ?",
                    (document.patch_id,),
                ).fetchone()
                if history is not None:
                    if history["checksum"] != document_checksum:
                        fail(
                            "E_PATCH_ALREADY_APPLIED",
                            "patchId was already applied with different content",
                            {"patchId": document.patch_id},
                        )
                    return json.loads(history["report_json"])
                config_version = get_int_meta(connection, "config_version")
                if (
                    document.base_config_version is not None
                    and document.base_config_version != config_version
                ):
                    fail(
                        "E_VERSION_CONFLICT",
                        "patch baseConfigVersion does not match current configuration",
                        {
                            "baseConfigVersion": document.base_config_version,
                            "currentConfigVersion": config_version,
                        },
                    )
                old_bundle = load_policy(connection)
                application = apply_patch_in_memory(old_bundle, document)
                new_bundle = application.bundle
                budget_state = connection.execute(
                    "SELECT * FROM budget_state WHERE id = 'budget'"
                ).fetchone()
                if new_bundle.budget.pool_core_units < int(
                    budget_state["core_used_units"]
                ):
                    fail(
                        "E_BUDGET_BELOW_COMMITTED",
                        "updated core budget is below committed core usage",
                        {
                            "committedCoreUnits": int(
                                budget_state["core_used_units"]
                            ),
                            "newPoolCoreUnits": new_bundle.budget.pool_core_units,
                        },
                    )
                if new_bundle.budget.memory_limit_mib < int(
                    budget_state["memory_reserved_mib"]
                ):
                    fail(
                        "E_BUDGET_BELOW_COMMITTED",
                        "updated memory budget is below in-flight reservations",
                        {
                            "reservedMemoryMiB": int(
                                budget_state["memory_reserved_mib"]
                            ),
                            "newMemoryLimitMiB": new_bundle.budget.memory_limit_mib,
                        },
                    )
                inflight_conflicts = []
                for workload_id in application.deleted_workloads:
                    row = connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM actions
                        WHERE workload_id = ?
                          AND status IN ('reserved', 'started')
                        """,
                        (workload_id,),
                    ).fetchone()
                    if int(row["count"]) > 0:
                        inflight_conflicts.append(
                            {"objectType": "workload", "objectId": workload_id}
                        )
                for rule_id in application.deleted_rules:
                    row = connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM actions
                        WHERE rule_id = ?
                          AND status IN ('reserved', 'started')
                        """,
                        (rule_id,),
                    ).fetchone()
                    if int(row["count"]) > 0:
                        inflight_conflicts.append(
                            {"objectType": "rule", "objectId": rule_id}
                        )
                if inflight_conflicts:
                    fail(
                        "E_INFLIGHT_CONFLICT",
                        "patch deletes objects with in-flight actions",
                        {"objects": inflight_conflicts},
                    )

                after_config_version = config_version + 1
                cursor = connection.execute(
                    """
                    UPDATE meta SET value = ?
                    WHERE key = 'config_version' AND value = ?
                    """,
                    (str(after_config_version), str(config_version)),
                )
                if cursor.rowcount != 1:
                    fail(
                        "E_VERSION_CONFLICT",
                        "configuration changed while applying the patch",
                    )
                for table in [
                    "freeze_windows",
                    "resource_budget",
                    "scaling_rules",
                    "metric_sources",
                    "policy_workloads",
                ]:
                    connection.execute(f"DELETE FROM {table}")
                insert_or_replace_policy(connection, new_bundle)

                for workload_id in application.deleted_workloads:
                    connection.execute(
                        "DELETE FROM workload_state WHERE workload_id = ?",
                        (workload_id,),
                    )
                    connection.execute(
                        "DELETE FROM replica_basis WHERE workload_id = ?",
                        (workload_id,),
                    )
                    connection.execute(
                        "DELETE FROM replica_ledger WHERE workload_id = ?",
                        (workload_id,),
                    )
                    connection.execute(
                        "DELETE FROM decision_cache WHERE workload_id = ?",
                        (workload_id,),
                    )
                for workload_id in application.added_workloads:
                    workload = new_bundle.workloads[workload_id]
                    connection.execute(
                        """
                        INSERT INTO workload_state(workload_id, replicas, state_version)
                        VALUES(?, ?, ?)
                        """,
                        (
                            workload_id,
                            workload.current_replicas,
                            get_int_meta(connection, "state_version"),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO replica_basis(workload_id, basis_replicas)
                        VALUES(?, ?)
                        """,
                        (workload_id, workload.current_replicas),
                    )
                for workload_id, replicas in application.workload_current_changes.items():
                    if workload_id in new_bundle.workloads:
                        connection.execute(
                            """
                            UPDATE workload_state SET replicas = ?
                            WHERE workload_id = ?
                            """,
                            (replicas, workload_id),
                        )
                        connection.execute(
                            """
                            INSERT INTO replica_basis(workload_id, basis_replicas)
                            VALUES(?, ?)
                            ON CONFLICT(workload_id) DO UPDATE SET
                                basis_replicas = excluded.basis_replicas
                            """,
                            (workload_id, replicas),
                        )
                        connection.execute(
                            "DELETE FROM replica_ledger WHERE workload_id = ?",
                            (workload_id,),
                        )
                for rule_id in application.deleted_rules:
                    connection.execute(
                        "DELETE FROM rule_state WHERE rule_id = ?",
                        (rule_id,),
                    )
                    connection.execute(
                        "DELETE FROM action_outcome_rules WHERE rule_id = ?",
                        (rule_id,),
                    )
                for rule_id in application.added_rules:
                    connection.execute(
                        """
                        INSERT INTO rule_state(
                            rule_id, cooldown_until_ms, backoff_until_ms,
                            consecutive_failures, last_success_ms, last_failure_ms
                        ) VALUES(?, 0, 0, 0, NULL, NULL)
                        """,
                        (rule_id,),
                    )
                for rule_id, fields in application.rule_field_changes.items():
                    if (
                        rule_id in new_bundle.rules
                        and ("*" in fields or fields & COOLDOWN_AFFECTING_RULE_FIELDS)
                    ):
                        connection.execute(
                            """
                            UPDATE rule_state
                            SET cooldown_until_ms = 0,
                                backoff_until_ms = 0,
                                consecutive_failures = 0,
                                last_success_ms = NULL,
                                last_failure_ms = NULL
                            WHERE rule_id = ?
                            """,
                            (rule_id,),
                        )
                report = build_invalidation_report(
                    old_bundle,
                    application,
                    before_config_version=config_version,
                    after_config_version=after_config_version,
                )
                report["patchId"] = document.patch_id
                report["operations"] = [
                    {
                        "operationId": operation.operation_id,
                        "op": operation.op,
                        "objectType": operation.object_type,
                        "objectId": operation.object_id,
                    }
                    for operation in document.operations
                ]
                affected = {
                    entry["workloadId"] for entry in report["affectedWorkloads"]
                }
                for workload_id in sorted(affected):
                    connection.execute(
                        "DELETE FROM decision_cache WHERE workload_id = ?",
                        (workload_id,),
                    )
                expected_state_version = get_int_meta(
                    connection,
                    "state_version",
                )
                snapshot = publish_snapshot(
                    connection,
                    expected_state_version=expected_state_version,
                    round_id=None,
                    increment=True,
                )
                report["stateVersion"] = snapshot["stateVersion"]
                report["snapshotId"] = snapshot["snapshotId"]
                connection.execute(
                    """
                    INSERT INTO patch_history(
                        patch_id, checksum, base_config_version,
                        after_config_version, report_json,
                        applied_state_version
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.patch_id,
                        document_checksum,
                        config_version,
                        after_config_version,
                        canonical_json(report),
                        snapshot["stateVersion"],
                    ),
                )
            return report
        finally:
            connection.close()


def decide(
    db_file: str | Path,
    *,
    at: int | None = None,
    workload_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    with _lock_for(db_file):
        connection = connect(db_file)
        try:
            ensure_schema(connection)
            with immediate_transaction(connection):
                policy = load_policy(connection)
                logical_now_ms = _resolve_at(connection, at)
                scope = _normalize_scope(policy, workload_ids)
                payload, reused = _ensure_round(
                    connection,
                    logical_now_ms=logical_now_ms,
                    scope=scope,
                )
                if not reused:
                    expected_state_version = get_int_meta(
                        connection,
                        "state_version",
                    )
                    snapshot = publish_snapshot(
                        connection,
                        expected_state_version=expected_state_version,
                        round_id=payload["roundId"],
                        increment=True,
                    )
                    connection.execute(
                        "UPDATE rounds SET state_version = ? WHERE round_id = ?",
                        (snapshot["stateVersion"], payload["roundId"]),
                    )
                return payload
        finally:
            connection.close()


def dispatch(
    db_file: str | Path,
    *,
    at: int | None = None,
    workload_ids: Iterable[str] | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    action_ids: list[str] = []
    newly_planned = False
    with _lock_for(db_file):
        connection = connect(db_file)
        try:
            ensure_schema(connection)
            with immediate_transaction(connection):
                policy = load_policy(connection)
                logical_now_ms = _resolve_at(connection, at)
                scope = _normalize_scope(policy, workload_ids)
                round_payload, _ = _ensure_round(
                    connection,
                    logical_now_ms=logical_now_ms,
                    scope=scope,
                )
                action_ids, existing_plan = _plan_actions(
                    connection,
                    round_payload,
                )
                newly_planned = not existing_plan
                if newly_planned:
                    expected_state_version = get_int_meta(
                        connection,
                        "state_version",
                    )
                    publish_snapshot(
                        connection,
                        expected_state_version=expected_state_version,
                        round_id=round_payload["roundId"],
                        increment=True,
                    )
        finally:
            connection.close()
    if newly_planned:
        _fault("after_reservation")
    _run_actions(db_file, action_ids, workers=workers)
    with _lock_for(db_file):
        connection = connect(db_file)
        try:
            ensure_schema(connection)
            with immediate_transaction(connection):
                payload = _finalize_round(connection, round_payload["roundId"])
            return payload
        finally:
            connection.close()


def resume(db_file: str | Path, *, workers: int = 4) -> dict[str, Any]:
    connection = connect(db_file)
    try:
        ensure_schema(connection)
        connection.execute("BEGIN")
        try:
            classification_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM actions
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            status_counts = {
                row["status"]: int(row["count"]) for row in classification_rows
            }
            rows = connection.execute(
                """
                SELECT action_id, round_id FROM actions
                WHERE status IN ('reserved', 'started')
                ORDER BY created_logical_ms, priority, action_id
                """
            ).fetchall()
            action_ids = [row["action_id"] for row in rows]
            round_ids = sorted({row["round_id"] for row in rows})
        finally:
            connection.execute("COMMIT")
    finally:
        connection.close()
    results = _run_actions(db_file, action_ids, workers=workers)
    finalized: list[dict[str, Any]] = []
    if round_ids:
        with _lock_for(db_file):
            connection = connect(db_file)
            try:
                ensure_schema(connection)
                with immediate_transaction(connection):
                    for round_id in round_ids:
                        finalized.append(
                            _finalize_round(connection, round_id)
                        )
            finally:
                connection.close()
    return {
        "recoveryClassification": {
            "committed": (
                status_counts.get("succeeded", 0)
                + status_counts.get("failed", 0)
            ),
            "reservedBeforeDispatch": status_counts.get("reserved", 0),
            "startedUnconfirmed": status_counts.get("started", 0),
            "terminalNotStarted": (
                status_counts.get("not_started", 0)
                + status_counts.get("budget_not_started", 0)
                + status_counts.get("abandoned", 0)
            ),
        },
        "resumedActionIds": action_ids,
        "actionResults": results,
        "rounds": finalized,
        "counts": {
            "resumed": len(action_ids),
            "confirmed": sum(
                1
                for result in results
                if result["status"] in {"succeeded", "failed"}
            ),
        },
    }


def status(db_file: str | Path) -> dict[str, Any]:
    connection = connect(db_file)
    try:
        ensure_schema(connection)
        connection.execute("BEGIN")
        try:
            return current_status(connection)
        finally:
            connection.execute("COMMIT")
    finally:
        connection.close()


def verify(db_file: str | Path) -> dict[str, Any]:
    connection = connect(db_file)
    try:
        ensure_schema(connection)
        connection.execute("BEGIN")
        try:
            return verify_state(connection)
        finally:
            connection.execute("COMMIT")
    finally:
        connection.close()

