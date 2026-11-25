from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .jsonutil import dumps, loads


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watermarks (
    scope_id TEXT NOT NULL,
    window_version TEXT NOT NULL,
    watermark_ms INTEGER NOT NULL,
    PRIMARY KEY(scope_id, window_version)
);

CREATE TABLE IF NOT EXISTS commit_prep (
    prep_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    crash_point TEXT,
    bundle_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged','committed','aborted_recovery')),
    created_rowid INTEGER
);

CREATE TABLE IF NOT EXISTS staged_ledger (
    prep_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(prep_id, metric)
);

CREATE TABLE IF NOT EXISTS windows (
    window_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    step_ms INTEGER NOT NULL,
    watermark_ms INTEGER NOT NULL,
    max_event_clock_ms INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    dropped_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    prep_id TEXT,
    UNIQUE(scope_id, window_index, window_version)
);

CREATE TABLE IF NOT EXISTS metric_values (
    window_pk INTEGER NOT NULL REFERENCES windows(window_pk),
    metric TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    scale INTEGER NOT NULL,
    fixed_value INTEGER NOT NULL,
    raw_value_json TEXT NOT NULL,
    truth_value TEXT NOT NULL,
    absolute_deviation TEXT NOT NULL,
    relative_deviation TEXT NOT NULL,
    error_bound TEXT NOT NULL,
    within_budget INTEGER NOT NULL,
    chain_json TEXT NOT NULL,
    PRIMARY KEY(window_pk, metric, metric_version)
);

CREATE TABLE IF NOT EXISTS remainder_ledger (
    window_pk INTEGER NOT NULL REFERENCES windows(window_pk),
    metric TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    carry_in_num INTEGER NOT NULL,
    carry_in_den INTEGER NOT NULL,
    carry_out_num INTEGER NOT NULL,
    carry_out_den INTEGER NOT NULL,
    discarded_json TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    registration_seq INTEGER NOT NULL,
    PRIMARY KEY(window_pk, metric, metric_version)
);

CREATE TABLE IF NOT EXISTS conversion_ops (
    window_pk INTEGER NOT NULL REFERENCES windows(window_pk),
    metric TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    registration_seq INTEGER NOT NULL,
    register_id INTEGER NOT NULL,
    op TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(window_pk, metric, metric_version, registration_seq)
);

CREATE TABLE IF NOT EXISTS evaluations (
    window_pk INTEGER NOT NULL REFERENCES windows(window_pk),
    metric TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    threshold_version TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    safe_verdict TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    higher_scale_flip INTEGER NOT NULL,
    reused INTEGER NOT NULL DEFAULT 0,
    source_threshold_version TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(window_pk, metric, metric_version, threshold_version)
);

CREATE TABLE IF NOT EXISTS sensitivity_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_index INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    metric TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    threshold_version TEXT NOT NULL,
    scope_a TEXT NOT NULL,
    scope_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    sensitive INTEGER NOT NULL,
    higher_scale_flip INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(window_index, window_version, metric, metric_version, threshold_version, scope_a, scope_b)
);

CREATE TABLE IF NOT EXISTS threshold_replacements (
    replacement_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    threshold_version TEXT,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged','committed','aborted_recovery')),
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threshold_replacements_snapshot
ON threshold_replacements(snapshot_id);

CREATE TABLE IF NOT EXISTS threshold_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    effective_logical_ms INTEGER NOT NULL,
    threshold_version TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threshold_current (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    snapshot_id TEXT NOT NULL,
    threshold_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_hash TEXT NOT NULL,
    line_number INTEGER,
    error_code TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metric_values_lookup
ON metric_values(metric, metric_version);
CREATE INDEX IF NOT EXISTS idx_evaluations_version
ON evaluations(threshold_version, verdict);
"""


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Explicit immediate transaction for connections in autocommit mode."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def recover(
    conn: sqlite3.Connection,
    *,
    scope_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    include_thresholds: bool = True,
) -> dict[str, Any]:
    """Abort explicitly staged but not finally committed bundles.

    SQLite rolls back an uncommitted transaction automatically.  These rows
    represent the durable pre-commit stage used to detect a SIGKILL between
    staging and the final atomic transaction.
    """
    result: dict[str, Any] = {"window_preps": [], "threshold_replacements": []}
    with write_transaction(conn):
        if scope_ids is None:
            rows = conn.execute(
                "SELECT prep_id, scope_id, window_index, window_version, bundle_json "
                "FROM commit_prep WHERE status = 'staged' ORDER BY created_rowid"
            ).fetchall()
        else:
            scope_list = tuple(scope_ids)
            if not scope_list:
                rows = []
            else:
                placeholders = ",".join("?" for _ in scope_list)
                rows = conn.execute(
                    "SELECT prep_id, scope_id, window_index, window_version, bundle_json "
                    f"FROM commit_prep WHERE status = 'staged' AND scope_id IN ({placeholders}) "
                    "ORDER BY created_rowid",
                    scope_list,
                ).fetchall()

        for row in rows:
            result["window_preps"].append(
                {
                    "prep_id": row["prep_id"],
                    "scope_id": row["scope_id"],
                    "window_index": row["window_index"],
                    "window_version": row["window_version"],
                }
            )
        if rows:
            conn.executemany(
                "UPDATE commit_prep SET status='aborted_recovery' WHERE prep_id=?",
                [(row["prep_id"],) for row in rows],
            )

        threshold_rows = []
        if include_thresholds:
            threshold_rows = conn.execute(
                "SELECT replacement_id, snapshot_id, generation FROM threshold_replacements "
                "WHERE status='staged' ORDER BY generation"
            ).fetchall()
            for row in threshold_rows:
                result["threshold_replacements"].append(dict(row))
            conn.execute(
                "UPDATE threshold_replacements SET status='aborted_recovery' "
                "WHERE status='staged'"
            )
    return result


def has_window(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    window_index: int,
    window_version: str,
) -> int | None:
    row = conn.execute(
        "SELECT window_pk FROM windows WHERE scope_id=? AND window_index=? AND window_version=?",
        (scope_id, window_index, window_version),
    ).fetchone()
    return int(row["window_pk"]) if row else None


def has_metric_value(
    conn: sqlite3.Connection,
    *,
    window_pk: int,
    metric: str,
    metric_version: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM metric_values WHERE window_pk=? AND metric=? AND metric_version=?",
        (window_pk, metric, metric_version),
    ).fetchone()
    return row is not None


def previous_carry(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    window_index: int,
    window_version: str,
    metric: str,
    metric_version: str,
) -> tuple[int, int] | None:
    row = conn.execute(
        """
        SELECT r.carry_out_num, r.carry_out_den
        FROM remainder_ledger r
        JOIN windows w ON w.window_pk = r.window_pk
        WHERE w.scope_id=? AND w.window_index=? AND w.window_version=?
          AND r.metric=? AND r.metric_version=?
        """,
        (scope_id, window_index - 1, window_version, metric, metric_version),
    ).fetchone()
    if row is None:
        return None
    return int(row["carry_out_num"]), int(row["carry_out_den"])


def stage_window_bundle(conn: sqlite3.Connection, bundle: dict[str, Any]) -> str:
    prep_id = bundle.get("prep_id") or f"prep-{uuid.uuid4().hex}"
    bundle["prep_id"] = prep_id
    with write_transaction(conn):
        conn.execute(
            """
            INSERT INTO commit_prep(
                prep_id, scope_id, window_index, window_version,
                crash_point, bundle_json, status, created_rowid
            ) VALUES (?, ?, ?, ?, ?, ?, 'staged', ?)
            """,
            (
                prep_id,
                bundle["scope_id"],
                bundle["window_index"],
                bundle["window_version"],
                bundle.get("crash_point", "after_stage"),
                dumps(bundle),
                bundle["window_index"],
            ),
        )
        for metric_name, ledger in bundle.get("ledgers", {}).items():
            conn.execute(
                "INSERT INTO staged_ledger(prep_id, metric, payload_json) VALUES (?, ?, ?)",
                (prep_id, metric_name, dumps(ledger)),
            )
    return prep_id


def commit_window_bundle(conn: sqlite3.Connection, prep_id: str) -> int:
    prep = conn.execute(
        "SELECT bundle_json FROM commit_prep WHERE prep_id=?", (prep_id,)
    ).fetchone()
    if prep is None:
        raise KeyError(prep_id)
    bundle = loads(prep["bundle_json"])

    with write_transaction(conn):
        existing = conn.execute(
            "SELECT window_pk FROM windows WHERE scope_id=? AND window_index=? AND window_version=?",
            (bundle["scope_id"], bundle["window_index"], bundle["window_version"]),
        ).fetchone()
        if existing is not None:
            window_pk = int(existing["window_pk"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO windows(
                    scope_type, scope_id, window_index, window_version,
                    start_ms, end_ms, step_ms, watermark_ms, max_event_clock_ms,
                    request_count, success_count, dropped_count, summary_json,
                    assignment_hash, prep_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle["scope_type"],
                    bundle["scope_id"],
                    bundle["window_index"],
                    bundle["window_version"],
                    bundle["start_ms"],
                    bundle["end_ms"],
                    bundle["step_ms"],
                    bundle["watermark_ms"],
                    bundle["max_event_clock_ms"],
                    bundle["request_count"],
                    bundle["success_count"],
                    bundle["dropped_count"],
                    dumps(bundle["summary"]),
                    bundle["assignment_hash"],
                    prep_id,
                ),
            )
            window_pk = int(cursor.lastrowid)

        conn.execute(
            """
            INSERT INTO watermarks(scope_id, window_version, watermark_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(scope_id, window_version)
            DO UPDATE SET watermark_ms=excluded.watermark_ms
            """,
            (bundle["scope_id"], bundle["window_version"], bundle["watermark_ms"]),
        )

        for metric_name, metric_bundle in bundle.get("metrics", {}).items():
            if has_metric_value(
                conn,
                window_pk=window_pk,
                metric=metric_name,
                metric_version=metric_bundle["metric_version"],
            ):
                continue
            conn.execute(
                """
                INSERT INTO metric_values(
                    window_pk, metric, metric_version, scale, fixed_value,
                    raw_value_json, truth_value, absolute_deviation,
                    relative_deviation, error_bound, within_budget, chain_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_pk,
                    metric_name,
                    metric_bundle["metric_version"],
                    metric_bundle["scale"],
                    metric_bundle["fixed_value"],
                    dumps(metric_bundle["raw_value"]),
                    metric_bundle["truth_value"],
                    metric_bundle["absolute_deviation"],
                    metric_bundle["relative_deviation"],
                    metric_bundle["error_bound"],
                    1 if metric_bundle["within_budget"] else 0,
                    dumps(metric_bundle["chain"]),
                ),
            )
            ledger = metric_bundle["ledger"]
            conn.execute(
                """
                INSERT INTO remainder_ledger(
                    window_pk, metric, metric_version,
                    carry_in_num, carry_in_den, carry_out_num, carry_out_den,
                    discarded_json, rounding_direction, registration_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_pk,
                    metric_name,
                    metric_bundle["metric_version"],
                    ledger["carry_in_num"],
                    ledger["carry_in_den"],
                    ledger["carry_out_num"],
                    ledger["carry_out_den"],
                    dumps(ledger["discarded"]),
                    ledger["rounding_direction"],
                    ledger["registration_seq"],
                ),
            )
            for operation in metric_bundle["chain"]["operations"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conversion_ops(
                        window_pk, metric, metric_version,
                        registration_seq, register_id, op, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        window_pk,
                        metric_name,
                        metric_bundle["metric_version"],
                        operation["registration_seq"],
                        operation["register_id"],
                        operation["op"],
                        dumps(operation),
                    ),
                )

        for evaluation in bundle.get("evaluations", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO evaluations(
                    window_pk, metric, metric_version, threshold_version,
                    scope_id, verdict, safe_verdict, precision_sensitive,
                    higher_scale_flip, reused, source_threshold_version, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_pk,
                    evaluation["metric"],
                    evaluation["metric_version"],
                    evaluation["threshold_version"],
                    evaluation["scope_id"],
                    evaluation["verdict"],
                    evaluation["safe_verdict"],
                    1 if evaluation["precision_sensitive"] else 0,
                    1 if evaluation["higher_scale_flip"] else 0,
                    1 if evaluation.get("reused", False) else 0,
                    evaluation.get("source_threshold_version"),
                    dumps(evaluation),
                ),
            )

        conn.execute(
            "UPDATE commit_prep SET status='committed' WHERE prep_id=?",
            (prep_id,),
        )
    return window_pk


def current_threshold(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload_json FROM threshold_current WHERE id=1").fetchone()
    return loads(row["payload_json"]) if row else None


def applied_v2_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(generation) AS generation FROM threshold_snapshots WHERE source_version='v2'"
    ).fetchone()
    return int(row["generation"] or 0)
