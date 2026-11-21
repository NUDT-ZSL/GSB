from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .fixed import core_units_to_hours_text
from .storage import (
    advance_state_version,
    canonical_json,
    checksum_json,
    get_int_meta,
    load_policy,
    require_meta,
    set_meta,
)

CHARGED_ACTION_STATUSES = ("reserved", "started", "succeeded", "failed")
IN_FLIGHT_STATUSES = ("reserved", "started")


def _json_or_none(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def build_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    policy = load_policy(connection)
    state_version = get_int_meta(connection, "state_version")
    config_version = get_int_meta(connection, "config_version")
    logical_clock_ms = get_int_meta(connection, "logical_clock_ms")
    sample_watermark_ms = get_int_meta(connection, "sample_watermark_ms")

    budget_row = connection.execute(
        "SELECT * FROM budget_state WHERE id = 'budget'"
    ).fetchone()
    if budget_row is None:
        raise RuntimeError("budget_state is missing")
    core_used = int(budget_row["core_used_units"])
    memory_reserved = int(budget_row["memory_reserved_mib"])
    core_remaining = policy.budget.pool_core_units - core_used
    memory_available = policy.budget.memory_limit_mib - memory_reserved

    workload_rows: list[dict[str, Any]] = []
    for workload_id in sorted(policy.workloads):
        workload = policy.workloads[workload_id]
        state_row = connection.execute(
            "SELECT * FROM workload_state WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        if state_row is None:
            raise RuntimeError(f"workload_state is missing for {workload_id}")
        rule_states: list[dict[str, Any]] = []
        for rule in sorted(
            (rule for rule in policy.rules.values() if rule.workload_id == workload_id),
            key=lambda item: (item.priority, item.id),
        ):
            row = connection.execute(
                "SELECT * FROM rule_state WHERE rule_id = ?",
                (rule.id,),
            ).fetchone()
            if row is None:
                continue
            rule_states.append(
                {
                    "ruleId": rule.id,
                    "ruleVersion": rule.version,
                    "direction": rule.direction,
                    "priority": rule.priority,
                    "cooldownUntilMs": int(row["cooldown_until_ms"]),
                    "backoffUntilMs": int(row["backoff_until_ms"]),
                    "consecutiveFailures": int(row["consecutive_failures"]),
                    "lastSuccessMs": row["last_success_ms"],
                    "lastFailureMs": row["last_failure_ms"],
                }
            )
        cache_row = connection.execute(
            "SELECT * FROM decision_cache WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        last_decision = None
        if cache_row is not None:
            last_decision = {
                "payload": json.loads(cache_row["decision_json"]),
                "checksum": cache_row["checksum"],
                "roundId": cache_row["round_id"],
                "configVersion": int(cache_row["config_version"]),
            }
        workload_rows.append(
            {
                "id": workload.id,
                "name": workload.name,
                "replicas": int(state_row["replicas"]),
                "minReplicas": workload.min_replicas,
                "maxReplicas": workload.max_replicas,
                "topologyDomain": workload.topology_domain,
                "maxCooldownUntilMs": max(
                    (state["cooldownUntilMs"] for state in rule_states),
                    default=0,
                ),
                "maxBackoffUntilMs": max(
                    (state["backoffUntilMs"] for state in rule_states),
                    default=0,
                ),
                "ruleStates": rule_states,
                "lastDecision": last_decision,
            }
        )

    inflight = []
    for row in connection.execute(
        """
        SELECT * FROM actions
        WHERE status IN ('reserved', 'started')
        ORDER BY created_logical_ms, priority, action_id
        """
    ).fetchall():
        inflight.append(
            {
                "actionId": row["action_id"],
                "roundId": row["round_id"],
                "workloadId": row["workload_id"],
                "ruleId": row["rule_id"],
                "ruleVersion": int(row["rule_version"]),
                "direction": row["direction"],
                "fromReplicas": int(row["from_replicas"]),
                "toReplicas": int(row["to_replicas"]),
                "status": row["status"],
                "coreCostUnits": int(row["core_cost_units"]),
                "memoryMiB": int(row["memory_mib"]),
                "createdLogicalMs": int(row["created_logical_ms"]),
            }
        )

    latest_round_row = connection.execute(
        "SELECT * FROM rounds ORDER BY state_version DESC, round_id DESC LIMIT 1"
    ).fetchone()
    latest_round = None
    if latest_round_row is not None:
        latest_round = {
            "roundId": latest_round_row["round_id"],
            "logicalNowMs": int(latest_round_row["logical_now_ms"]),
            "configVersion": int(latest_round_row["config_version"]),
            "status": latest_round_row["status"],
        }

    return {
        "schemaVersion": 1,
        "stateVersion": state_version,
        "configVersion": config_version,
        "logicalClockMs": logical_clock_ms,
        "sampleWatermarkMs": sample_watermark_ms,
        "budget": {
            "budgetId": policy.budget.id,
            "budgetVersion": policy.budget.version,
            "poolCoreHours": str(policy.budget.pool_core_hours),
            "poolCoreUnits": policy.budget.pool_core_units,
            "coreUsedUnits": core_used,
            "coreUnitsRemaining": core_remaining,
            "coreHoursUsed": core_units_to_hours_text(core_used),
            "coreHoursRemaining": core_units_to_hours_text(core_remaining),
            "memoryLimitMiB": policy.budget.memory_limit_mib,
            "memoryReservedMiB": memory_reserved,
            "memoryAvailableMiB": memory_available,
            "roundTimeBudgetMs": policy.budget.round_time_budget_ms,
            "maxConcurrentActionsPerWorkload": policy.budget.max_concurrent_actions_per_workload,
        },
        "freezeWindows": [
            window.to_json() for window in policy.budget.freeze_windows
        ],
        "workloads": workload_rows,
        "inFlightActions": inflight,
        "latestRound": latest_round,
    }


def publish_snapshot(
    connection: sqlite3.Connection,
    *,
    expected_state_version: int,
    round_id: str | None = None,
    increment: bool = True,
) -> dict[str, Any]:
    if increment:
        state_version = advance_state_version(connection, expected_state_version)
    else:
        current = get_int_meta(connection, "state_version")
        if current != expected_state_version:
            raise RuntimeError(
                f"expected state version {expected_state_version}, got {current}"
            )
        state_version = current
    if round_id is not None:
        connection.execute(
            "UPDATE rounds SET state_version = ? WHERE round_id = ?",
            (state_version, round_id),
        )
    body = build_snapshot(connection)
    checksum = checksum_json(body)
    snapshot_id = "snap-" + hashlib.sha256(
        f"{state_version}:{checksum}".encode("utf-8")
    ).hexdigest()[:24]
    connection.execute(
        """
        INSERT INTO snapshots(
            version, snapshot_id, body_json, checksum, round_id,
            created_state_version
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            state_version,
            snapshot_id,
            canonical_json(body),
            checksum,
            round_id,
            state_version,
        ),
    )
    set_meta(connection, "current_snapshot_id", snapshot_id)
    set_meta(connection, "current_snapshot_version", state_version)
    return {
        "snapshotId": snapshot_id,
        "stateVersion": state_version,
        "checksum": checksum,
        "body": body,
    }


def current_status(connection: sqlite3.Connection) -> dict[str, Any]:
    snapshot_id = require_meta(connection, "current_snapshot_id")
    row = connection.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"snapshot pointer {snapshot_id} is dangling")
    body = json.loads(row["body_json"])
    return {
        "snapshotId": row["snapshot_id"],
        "snapshotVersion": int(row["version"]),
        "snapshotChecksum": row["checksum"],
        **body,
    }


def verify_state(connection: sqlite3.Connection) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    checks["sqliteIntegrity"] = integrity
    if integrity != "ok":
        errors.append(
            {"code": "E_SQLITE_INTEGRITY", "message": str(integrity)}
        )

    try:
        state_version = get_int_meta(connection, "state_version")
        snapshot_version = get_int_meta(connection, "current_snapshot_version")
        snapshot_id = require_meta(connection, "current_snapshot_id")
        row = connection.execute(
            "SELECT * FROM snapshots WHERE version = ?",
            (snapshot_version,),
        ).fetchone()
        checks["stateVersion"] = state_version
        checks["snapshotVersion"] = snapshot_version
        if row is None:
            errors.append(
                {
                    "code": "E_SNAPSHOT_MISSING",
                    "message": "current snapshot version has no snapshot row",
                    "snapshotVersion": snapshot_version,
                }
            )
        else:
            checks["snapshotId"] = snapshot_id
            checks["snapshotChecksum"] = row["checksum"]
            if row["snapshot_id"] != snapshot_id:
                errors.append(
                    {
                        "code": "E_SNAPSHOT_POINTER",
                        "message": "snapshot pointer does not match snapshot row",
                    }
                )
            stored_body = json.loads(row["body_json"])
            calculated_checksum = checksum_json(stored_body)
            checks["snapshotChecksumValid"] = calculated_checksum == row["checksum"]
            if calculated_checksum != row["checksum"]:
                errors.append(
                    {
                        "code": "E_SNAPSHOT_CHECKSUM",
                        "message": "snapshot checksum does not match stored body",
                    }
                )
            live_body = build_snapshot(connection)
            checks["snapshotMatchesLiveState"] = canonical_json(
                live_body
            ) == canonical_json(stored_body)
            if canonical_json(live_body) != canonical_json(stored_body):
                errors.append(
                    {
                        "code": "E_SNAPSHOT_STALE",
                        "message": "snapshot body does not match live state",
                    }
                )
            if snapshot_version != state_version:
                errors.append(
                    {
                        "code": "E_SNAPSHOT_VERSION",
                        "message": "snapshot version does not match state version",
                    }
                )
    except Exception as exc:
        errors.append(
            {
                "code": "E_VERIFY_SNAPSHOT",
                "message": f"snapshot verification failed: {exc}",
            }
        )

    try:
        policy = load_policy(connection)
        budget_row = connection.execute(
            "SELECT * FROM budget_state WHERE id = 'budget'"
        ).fetchone()
        charged = connection.execute(
            """
            SELECT COALESCE(SUM(core_cost_units), 0) AS core_units,
                   COALESCE(SUM(CASE WHEN status IN ('reserved', 'started')
                                     THEN memory_mib ELSE 0 END), 0) AS memory
            FROM actions
            WHERE status IN ('reserved', 'started', 'succeeded', 'failed')
            """
        ).fetchone()
        expected_core = int(charged["core_units"])
        expected_memory = int(charged["memory"])
        actual_core = int(budget_row["core_used_units"])
        actual_memory = int(budget_row["memory_reserved_mib"])
        checks["budgetLedger"] = {
            "coreUsedUnits": actual_core,
            "expectedCoreUsedUnits": expected_core,
            "memoryReservedMiB": actual_memory,
            "expectedMemoryReservedMiB": expected_memory,
            "poolCoreUnits": policy.budget.pool_core_units,
            "memoryLimitMiB": policy.budget.memory_limit_mib,
        }
        if actual_core != expected_core:
            errors.append(
                {
                    "code": "E_BUDGET_CORE_LEDGER",
                    "message": "core ledger does not match charged actions",
                }
            )
        if actual_memory != expected_memory:
            errors.append(
                {
                    "code": "E_BUDGET_MEMORY_LEDGER",
                    "message": "memory reservation does not match in-flight actions",
                }
            )
        if actual_core > policy.budget.pool_core_units:
            errors.append(
                {
                    "code": "E_BUDGET_CORE_OVERSELL",
                    "message": "core ledger exceeds pool capacity",
                }
            )
        if actual_memory > policy.budget.memory_limit_mib:
            errors.append(
                {
                    "code": "E_BUDGET_MEMORY_OVERSELL",
                    "message": "memory reservation exceeds pool capacity",
                }
            )
    except Exception as exc:
        errors.append(
            {
                "code": "E_VERIFY_BUDGET",
                "message": f"budget verification failed: {exc}",
            }
        )

    succeeded = connection.execute(
        "SELECT * FROM actions WHERE status = 'succeeded' ORDER BY action_id"
    ).fetchall()
    effect_rows = {
        row["action_id"]: row
        for row in connection.execute("SELECT * FROM action_effects").fetchall()
    }
    effects_valid = True
    for action in succeeded:
        effect = effect_rows.get(action["action_id"])
        if (
            effect is None
            or int(effect["applied"]) != 1
            or int(effect["replicas_after"]) != int(action["to_replicas"])
        ):
            effects_valid = False
            errors.append(
                {
                    "code": "E_ACTION_EFFECT",
                    "message": "succeeded action is missing its idempotent effect",
                    "actionId": action["action_id"],
                }
            )
    non_succeeded_effects = [
        action_id
        for action_id, effect in effect_rows.items()
        if int(effect["applied"]) == 1
        and connection.execute(
            "SELECT status FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()["status"]
        != "succeeded"
    ]
    if non_succeeded_effects:
        effects_valid = False
        errors.append(
            {
                "code": "E_ACTION_EFFECT_STATUS",
                "message": "non-succeeded actions have applied effects",
                "actionIds": sorted(non_succeeded_effects),
            }
        )
    checks["actionEffectsValid"] = effects_valid

    replica_ledger_valid = True
    replica_checks: dict[str, Any] = {}
    for workload_id in sorted(
        row["workload_id"]
        for row in connection.execute(
            "SELECT workload_id FROM workload_state ORDER BY workload_id"
        ).fetchall()
    ):
        state_row = connection.execute(
            "SELECT replicas FROM workload_state WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        basis_row = connection.execute(
            "SELECT basis_replicas FROM replica_basis WHERE workload_id = ?",
            (workload_id,),
        ).fetchone()
        ledger_rows = connection.execute(
            """
            SELECT * FROM replica_ledger
            WHERE workload_id = ?
            ORDER BY state_version, action_id
            """,
            (workload_id,),
        ).fetchall()
        if basis_row is None:
            replica_ledger_valid = False
            errors.append(
                {
                    "code": "E_REPLICA_BASIS",
                    "message": "workload replica basis is missing",
                    "workloadId": workload_id,
                }
            )
            continue
        basis = int(basis_row["basis_replicas"])
        expected_replicas = basis + sum(int(row["delta"]) for row in ledger_rows)
        actual_replicas = int(state_row["replicas"])
        replica_checks[workload_id] = {
            "basisReplicas": basis,
            "ledgerDelta": sum(int(row["delta"]) for row in ledger_rows),
            "expectedReplicas": expected_replicas,
            "actualReplicas": actual_replicas,
        }
        if expected_replicas != actual_replicas:
            replica_ledger_valid = False
            errors.append(
                {
                    "code": "E_REPLICA_LEDGER",
                    "message": "replica ledger does not reconstruct workload state",
                    "workloadId": workload_id,
                    "expectedReplicas": expected_replicas,
                    "actualReplicas": actual_replicas,
                }
            )
        for ledger in ledger_rows:
            action = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (ledger["action_id"],),
            ).fetchone()
            if (
                action is None
                or action["status"] != "succeeded"
                or int(action["workload_id"] != workload_id)
                or int(ledger["delta"])
                != int(action["to_replicas"]) - int(action["from_replicas"])
                or int(ledger["replicas_after"]) != int(action["to_replicas"])
            ):
                replica_ledger_valid = False
                errors.append(
                    {
                        "code": "E_REPLICA_LEDGER_ACTION",
                        "message": "replica ledger entry does not match a succeeded action",
                        "workloadId": workload_id,
                        "actionId": ledger["action_id"],
                    }
                )
    checks["replicaLedgerValid"] = replica_ledger_valid
    checks["replicaLedger"] = replica_checks

    rule_runtime_valid = True
    for row in connection.execute(
        "SELECT * FROM rule_state ORDER BY rule_id"
    ).fetchall():
        cooldown = int(row["cooldown_until_ms"])
        backoff = int(row["backoff_until_ms"])
        failures = int(row["consecutive_failures"])
        if (
            failures < 0
            or (cooldown > 0 and row["last_success_ms"] is None)
            or (backoff > 0 and (failures < 1 or row["last_failure_ms"] is None))
            or (cooldown > 0 and backoff > 0)
        ):
            rule_runtime_valid = False
            errors.append(
                {
                    "code": "E_RULE_RUNTIME_STATE",
                    "message": "cooldown/backoff runtime state is inconsistent",
                    "ruleId": row["rule_id"],
                }
            )
    checks["ruleRuntimeValid"] = rule_runtime_valid

    duplicate_actions = connection.execute(
        """
        SELECT round_id, workload_id, rule_id, rule_version, COUNT(*) AS count
        FROM actions
        GROUP BY round_id, workload_id, rule_id, rule_version
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    checks["duplicateActionKeys"] = len(duplicate_actions)
    if duplicate_actions:
        errors.append(
            {
                "code": "E_DUPLICATE_ACTION",
                "message": "duplicate round/workload/rule/version actions exist",
            }
        )

    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
    }

