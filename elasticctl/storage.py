from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .errors import fail
from .fixed import fixed_text, to_fixed
from .model import (
    ActionOutcomeRule,
    FreezeWindow,
    MetricSample,
    MetricSource,
    PolicyBundle,
    ResourceBudget,
    ScalingRule,
    Workload,
)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_workloads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    min_replicas INTEGER NOT NULL,
    max_replicas INTEGER NOT NULL,
    current_replicas INTEGER NOT NULL,
    cpu_millicores_per_replica INTEGER NOT NULL,
    memory_mib_per_replica INTEGER NOT NULL,
    topology_domain TEXT NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    aggregation TEXT NOT NULL,
    percentile TEXT,
    window_ms INTEGER NOT NULL,
    target_raw TEXT NOT NULL,
    target_fixed INTEGER NOT NULL,
    alert_threshold_raw TEXT NOT NULL,
    alert_threshold_fixed INTEGER NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scaling_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    workload_id TEXT NOT NULL,
    metric_source_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    step INTEGER NOT NULL,
    percent_raw TEXT NOT NULL,
    priority INTEGER NOT NULL UNIQUE,
    cooldown_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_budget (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    pool_core_hours_raw TEXT NOT NULL,
    pool_core_units INTEGER NOT NULL,
    memory_limit_mib INTEGER NOT NULL,
    round_time_budget_ms INTEGER NOT NULL,
    max_concurrent_actions_per_workload INTEGER NOT NULL,
    action_base_duration_ms INTEGER NOT NULL,
    action_duration_per_replica_ms INTEGER NOT NULL,
    clock_rollback_tolerance_ms INTEGER NOT NULL,
    failure_backoff_base_ms INTEGER NOT NULL,
    failure_backoff_max_ms INTEGER NOT NULL,
    failure_backoff_jitter_ratio_raw TEXT NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS freeze_windows (
    id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    metric_source_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    value_raw TEXT NOT NULL,
    value_fixed INTEGER NOT NULL,
    PRIMARY KEY (metric_source_id, timestamp_ms)
);

CREATE TABLE IF NOT EXISTS action_outcome_rules (
    rule_id TEXT NOT NULL,
    logical_now_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (rule_id, logical_now_ms)
);

CREATE TABLE IF NOT EXISTS workload_state (
    workload_id TEXT PRIMARY KEY,
    replicas INTEGER NOT NULL,
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_basis (
    workload_id TEXT PRIMARY KEY,
    basis_replicas INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_ledger (
    action_id TEXT PRIMARY KEY,
    workload_id TEXT NOT NULL,
    delta INTEGER NOT NULL,
    replicas_after INTEGER NOT NULL,
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_state (
    rule_id TEXT PRIMARY KEY,
    cooldown_until_ms INTEGER NOT NULL,
    backoff_until_ms INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    last_success_ms INTEGER,
    last_failure_ms INTEGER
);

CREATE TABLE IF NOT EXISTS budget_state (
    id TEXT PRIMARY KEY,
    core_used_units INTEGER NOT NULL,
    memory_reserved_mib INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    logical_now_ms INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    scope_key TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    round_id TEXT NOT NULL,
    workload_id TEXT NOT NULL,
    selected_rule_id TEXT,
    direction TEXT,
    from_replicas INTEGER NOT NULL,
    to_replicas INTEGER NOT NULL,
    rule_version INTEGER,
    candidate_replicas INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    config_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (round_id, workload_id)
);

CREATE TABLE IF NOT EXISTS decision_cache (
    workload_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    updated_state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    workload_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    direction TEXT NOT NULL,
    from_replicas INTEGER NOT NULL,
    to_replicas INTEGER NOT NULL,
    delta_replicas INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    core_cost_units INTEGER NOT NULL,
    memory_mib INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    planned_outcome TEXT NOT NULL,
    planned_reason TEXT NOT NULL,
    result_json TEXT,
    created_logical_ms INTEGER NOT NULL,
    started_logical_ms INTEGER,
    finished_state_version INTEGER,
    UNIQUE (round_id, workload_id, rule_id, rule_version)
);

CREATE TABLE IF NOT EXISTS action_effects (
    action_id TEXT PRIMARY KEY,
    applied INTEGER NOT NULL,
    replicas_after INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aggregate_cache (
    metric_source_id TEXT NOT NULL,
    window_end_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    PRIMARY KEY (metric_source_id, window_end_ms)
);

CREATE TABLE IF NOT EXISTS patch_history (
    patch_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    base_config_version INTEGER NOT NULL,
    after_config_version INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    applied_state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    version INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL UNIQUE,
    body_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    round_id TEXT,
    created_state_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_actions_round_status
    ON actions(round_id, status);
CREATE INDEX IF NOT EXISTS ix_actions_workload_status
    ON actions(workload_id, status);
CREATE INDEX IF NOT EXISTS ix_samples_metric_time
    ON samples(metric_source_id, timestamp_ms);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def db_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def connect(path: str | Path, *, create: bool = False) -> sqlite3.Connection:
    resolved = db_path(path)
    if not create and not resolved.exists():
        fail("E_NOT_LOADED", "control-plane database does not exist", {"path": str(resolved)})
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(resolved),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def read_only_transaction(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN")


def end_read_transaction(connection: sqlite3.Connection) -> None:
    connection.execute("COMMIT")


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def require_meta(connection: sqlite3.Connection, key: str) -> str:
    value = get_meta(connection, key)
    if value is None:
        fail("E_NOT_LOADED", "control-plane metadata is incomplete", {"key": key})
    return value


def get_int_meta(connection: sqlite3.Connection, key: str) -> int:
    value = require_meta(connection, key)
    try:
        return int(value)
    except ValueError:
        fail("E_INVALID_STATE", "control-plane metadata is not an integer", {"key": key})


def set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def advance_state_version(connection: sqlite3.Connection, expected: int) -> int:
    new_version = expected + 1
    cursor = connection.execute(
        "UPDATE meta SET value = ? WHERE key = 'state_version' AND value = ?",
        (str(new_version), str(expected)),
    )
    if cursor.rowcount != 1:
        fail(
            "E_VERSION_CONFLICT",
            "state version changed concurrently",
            {"expectedStateVersion": expected},
        )
    return new_version


def assert_config_version(
    connection: sqlite3.Connection,
    expected: int,
) -> int:
    current = get_int_meta(connection, "config_version")
    if current != expected:
        fail(
            "E_VERSION_CONFLICT",
            "configuration version changed concurrently",
            {"expectedConfigVersion": expected, "actualConfigVersion": current},
        )
    return current


def workload_from_row(row: sqlite3.Row) -> Workload:
    return Workload(
        id=row["id"],
        name=row["name"],
        min_replicas=row["min_replicas"],
        max_replicas=row["max_replicas"],
        current_replicas=row["current_replicas"],
        cpu_millicores_per_replica=row["cpu_millicores_per_replica"],
        memory_mib_per_replica=row["memory_mib_per_replica"],
        topology_domain=row["topology_domain"],
        version=row["version"],
    )


def metric_from_row(row: sqlite3.Row) -> MetricSource:
    percentile = None if row["percentile"] is None else Decimal(row["percentile"])
    return MetricSource(
        id=row["id"],
        name=row["name"],
        aggregation=row["aggregation"],
        percentile=percentile,
        window_ms=row["window_ms"],
        target_value=Decimal(row["target_raw"]),
        alert_threshold=Decimal(row["alert_threshold_raw"]),
        version=row["version"],
    )


def rule_from_row(row: sqlite3.Row) -> ScalingRule:
    return ScalingRule(
        id=row["id"],
        name=row["name"],
        workload_id=row["workload_id"],
        metric_source_id=row["metric_source_id"],
        direction=row["direction"],
        step=row["step"],
        percent=Decimal(row["percent_raw"]),
        priority=row["priority"],
        cooldown_ms=row["cooldown_ms"],
        version=row["version"],
    )


def budget_from_rows(
    budget_row: sqlite3.Row,
    freeze_rows: list[sqlite3.Row],
) -> ResourceBudget:
    return ResourceBudget(
        id=budget_row["id"],
        name=budget_row["name"],
        pool_core_hours=Decimal(budget_row["pool_core_hours_raw"]),
        pool_core_units=budget_row["pool_core_units"],
        memory_limit_mib=budget_row["memory_limit_mib"],
        round_time_budget_ms=budget_row["round_time_budget_ms"],
        max_concurrent_actions_per_workload=budget_row[
            "max_concurrent_actions_per_workload"
        ],
        action_base_duration_ms=budget_row["action_base_duration_ms"],
        action_duration_per_replica_ms=budget_row[
            "action_duration_per_replica_ms"
        ],
        clock_rollback_tolerance_ms=budget_row["clock_rollback_tolerance_ms"],
        failure_backoff_base_ms=budget_row["failure_backoff_base_ms"],
        failure_backoff_max_ms=budget_row["failure_backoff_max_ms"],
        failure_backoff_jitter_ratio=Decimal(
            budget_row["failure_backoff_jitter_ratio_raw"]
        ),
        freeze_windows=tuple(
            FreezeWindow(
                id=row["id"],
                start_ms=row["start_ms"],
                end_ms=row["end_ms"],
            )
            for row in freeze_rows
        ),
        version=budget_row["version"],
    )


def load_policy(connection: sqlite3.Connection) -> PolicyBundle:
    workloads = {
        row["id"]: workload_from_row(row)
        for row in connection.execute(
            "SELECT * FROM policy_workloads ORDER BY id"
        ).fetchall()
    }
    metrics = {
        row["id"]: metric_from_row(row)
        for row in connection.execute(
            "SELECT * FROM metric_sources ORDER BY id"
        ).fetchall()
    }
    rules = {
        row["id"]: rule_from_row(row)
        for row in connection.execute(
            "SELECT * FROM scaling_rules ORDER BY id"
        ).fetchall()
    }
    budget_row = connection.execute(
        "SELECT * FROM resource_budget ORDER BY id LIMIT 1"
    ).fetchone()
    if budget_row is None:
        fail("E_NOT_LOADED", "resource budget is missing from persistent state")
    freeze_rows = connection.execute(
        "SELECT * FROM freeze_windows WHERE budget_id = ? ORDER BY start_ms, end_ms, id",
        (budget_row["id"],),
    ).fetchall()
    return PolicyBundle(
        workloads=workloads,
        metrics=metrics,
        rules=rules,
        budget=budget_from_rows(budget_row, list(freeze_rows)),
    )


def load_samples(connection: sqlite3.Connection) -> list[MetricSample]:
    return [
        MetricSample(
            metric_source_id=row["metric_source_id"],
            timestamp_ms=row["timestamp_ms"],
            value=Decimal(row["value_raw"]),
            value_fixed=row["value_fixed"],
        )
        for row in connection.execute(
            "SELECT * FROM samples ORDER BY metric_source_id, timestamp_ms"
        ).fetchall()
    ]


def load_outcomes(connection: sqlite3.Connection) -> list[ActionOutcomeRule]:
    return [
        ActionOutcomeRule(
            rule_id=row["rule_id"],
            logical_now_ms=row["logical_now_ms"],
            outcome=row["outcome"],
            reason=row["reason"],
        )
        for row in connection.execute(
            "SELECT * FROM action_outcome_rules ORDER BY logical_now_ms, rule_id"
        ).fetchall()
    ]


def insert_or_replace_policy(
    connection: sqlite3.Connection,
    bundle: PolicyBundle,
) -> None:
    for workload in bundle.workloads.values():
        connection.execute(
            """
            INSERT INTO policy_workloads(
                id, name, min_replicas, max_replicas, current_replicas,
                cpu_millicores_per_replica, memory_mib_per_replica,
                topology_domain, version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workload.id,
                workload.name,
                workload.min_replicas,
                workload.max_replicas,
                workload.current_replicas,
                workload.cpu_millicores_per_replica,
                workload.memory_mib_per_replica,
                workload.topology_domain,
                workload.version,
            ),
        )
    for metric in bundle.metrics.values():
        connection.execute(
            """
            INSERT INTO metric_sources(
                id, name, aggregation, percentile, window_ms,
                target_raw, target_fixed, alert_threshold_raw,
                alert_threshold_fixed, version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metric.id,
                metric.name,
                metric.aggregation,
                None if metric.percentile is None else str(metric.percentile),
                metric.window_ms,
                str(metric.target_value),
                to_fixed(metric.target_value, "targetValue"),
                str(metric.alert_threshold),
                to_fixed(metric.alert_threshold, "alertThreshold"),
                metric.version,
            ),
        )
    for rule in bundle.rules.values():
        connection.execute(
            """
            INSERT INTO scaling_rules(
                id, name, workload_id, metric_source_id, direction,
                step, percent_raw, priority, cooldown_ms, version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule.id,
                rule.name,
                rule.workload_id,
                rule.metric_source_id,
                rule.direction,
                rule.step,
                str(rule.percent),
                rule.priority,
                rule.cooldown_ms,
                rule.version,
            ),
        )
    budget = bundle.budget
    connection.execute(
        """
        INSERT INTO resource_budget(
            id, name, pool_core_hours_raw, pool_core_units,
            memory_limit_mib, round_time_budget_ms,
            max_concurrent_actions_per_workload,
            action_base_duration_ms, action_duration_per_replica_ms,
            clock_rollback_tolerance_ms, failure_backoff_base_ms,
            failure_backoff_max_ms, failure_backoff_jitter_ratio_raw, version
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            budget.id,
            budget.name,
            str(budget.pool_core_hours),
            budget.pool_core_units,
            budget.memory_limit_mib,
            budget.round_time_budget_ms,
            budget.max_concurrent_actions_per_workload,
            budget.action_base_duration_ms,
            budget.action_duration_per_replica_ms,
            budget.clock_rollback_tolerance_ms,
            budget.failure_backoff_base_ms,
            budget.failure_backoff_max_ms,
            str(budget.failure_backoff_jitter_ratio),
            budget.version,
        ),
    )
    for window in budget.freeze_windows:
        connection.execute(
            """
            INSERT INTO freeze_windows(id, budget_id, start_ms, end_ms)
            VALUES(?, ?, ?, ?)
            """,
            (window.id, budget.id, window.start_ms, window.end_ms),
        )


def insert_samples(
    connection: sqlite3.Connection,
    samples: list[MetricSample],
) -> None:
    connection.executemany(
        """
        INSERT INTO samples(metric_source_id, timestamp_ms, value_raw, value_fixed)
        VALUES(?, ?, ?, ?)
        """,
        [
            (
                sample.metric_source_id,
                sample.timestamp_ms,
                str(sample.value),
                sample.value_fixed,
            )
            for sample in samples
        ],
    )


def insert_outcomes(
    connection: sqlite3.Connection,
    outcomes: list[ActionOutcomeRule],
) -> None:
    connection.executemany(
        """
        INSERT INTO action_outcome_rules(rule_id, logical_now_ms, outcome, reason)
        VALUES(?, ?, ?, ?)
        """,
        [
            (item.rule_id, item.logical_now_ms, item.outcome, item.reason)
            for item in outcomes
        ],
    )


def initialize_state(
    connection: sqlite3.Connection,
    bundle: PolicyBundle,
    samples: list[MetricSample],
    outcomes: list[ActionOutcomeRule],
    *,
    replace: bool,
) -> None:
    initialized = get_meta(connection, "schema_version") is not None
    if initialized and not replace:
        fail(
            "E_ALREADY_LOADED",
            "control-plane database is already initialized; use --replace",
        )
    if replace:
        for table in [
            "snapshots",
            "patch_history",
            "aggregate_cache",
            "action_effects",
            "actions",
            "decision_cache",
            "decisions",
            "rounds",
            "budget_state",
            "rule_state",
            "replica_ledger",
            "replica_basis",
            "workload_state",
            "action_outcome_rules",
            "samples",
            "freeze_windows",
            "resource_budget",
            "scaling_rules",
            "metric_sources",
            "policy_workloads",
            "meta",
        ]:
            connection.execute(f"DELETE FROM {table}")
    insert_or_replace_policy(connection, bundle)
    insert_samples(connection, samples)
    insert_outcomes(connection, outcomes)
    for workload in bundle.workloads.values():
        connection.execute(
            """
            INSERT INTO workload_state(workload_id, replicas, state_version)
            VALUES(?, ?, 1)
            """,
            (workload.id, workload.current_replicas),
        )
        connection.execute(
            """
            INSERT INTO replica_basis(workload_id, basis_replicas)
            VALUES(?, ?)
            """,
            (workload.id, workload.current_replicas),
        )
    for rule in bundle.rules.values():
        connection.execute(
            """
            INSERT INTO rule_state(
                rule_id, cooldown_until_ms, backoff_until_ms,
                consecutive_failures, last_success_ms, last_failure_ms
            ) VALUES(?, 0, 0, 0, NULL, NULL)
            """,
            (rule.id,),
        )
    connection.execute(
        "INSERT INTO budget_state(id, core_used_units, memory_reserved_mib) VALUES('budget', 0, 0)"
    )
    watermark = 0
    if samples:
        watermark = max(sample.timestamp_ms for sample in samples if sample.timestamp_ms <= 0)
    set_meta(connection, "schema_version", SCHEMA_VERSION)
    set_meta(connection, "config_version", 1)
    set_meta(connection, "state_version", 1)
    set_meta(connection, "logical_clock_ms", 0)
    set_meta(connection, "sample_watermark_ms", watermark)
    set_meta(connection, "round_sequence", 0)
    set_meta(connection, "current_snapshot_id", "")


def delete_policy_object(
    connection: sqlite3.Connection,
    object_type: str,
    object_id: str,
) -> None:
    table = {
        "workload": "policy_workloads",
        "metric_source": "metric_sources",
        "rule": "scaling_rules",
    }.get(object_type)
    if table is None:
        fail("E_INVALID_PATCH", "unsupported delete object type", {"objectType": object_type})
    connection.execute(f"DELETE FROM {table} WHERE id = ?", (object_id,))


def wipe_all_state(connection: sqlite3.Connection) -> None:
    for table in [
        "snapshots",
        "patch_history",
        "aggregate_cache",
        "action_effects",
        "actions",
        "decision_cache",
        "decisions",
        "rounds",
        "budget_state",
        "rule_state",
        "replica_ledger",
        "replica_basis",
        "workload_state",
        "action_outcome_rules",
        "samples",
        "freeze_windows",
        "resource_budget",
        "scaling_rules",
        "metric_sources",
        "policy_workloads",
        "meta",
    ]:
        connection.execute(f"DELETE FROM {table}")


def fixed_value_from_text(value: str) -> int:
    return to_fixed(value)


def fixed_or_raw_to_text(value: int) -> str:
    return fixed_text(value)

