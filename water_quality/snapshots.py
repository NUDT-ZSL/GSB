"""SQLite snapshots, rule-version checks and resumable incremental runs."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import canonical_json, sha256_text
from .engine import PartitionVersionMismatchError, PushdownEngine
from .rules import PartitionRules, get_active_rules


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"snap-{stamp}-{uuid.uuid4().hex[:8]}"


RUN_TO_SNAPSHOT_COPIES = (
    (
        "snapshot_partition_assignments",
        """
        INSERT INTO snapshot_partition_assignments(
            snapshot_id, entity_type, entity_key, entity_id, partition_id
        )
        SELECT ?, entity_type, entity_key, entity_id, partition_id
        FROM run_partition_assignments
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_query_plans",
        """
        INSERT INTO snapshot_query_plans(
            snapshot_id, query_id, query_json, plan_json, join_type
        )
        SELECT ?, query_id, query_json, plan_json, join_type
        FROM query_plans
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_plan_partitions",
        """
        INSERT INTO snapshot_plan_partitions(
            snapshot_id, query_id, part_type, partition_id,
            predicate_sql, execution_sql, result_count
        )
        SELECT ?, query_id, part_type, partition_id,
               predicate_sql, execution_sql, result_count
        FROM query_plan_partitions
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_pipe_hits",
        """
        INSERT INTO snapshot_pipe_hits(
            snapshot_id, query_id, partition_id,
            pipe_row_id, pipe_id, row_json
        )
        SELECT ?, query_id, partition_id, pipe_row_id, pipe_id, row_json
        FROM run_pipe_hits
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_point_hits",
        """
        INSERT INTO snapshot_point_hits(
            snapshot_id, query_id, partition_id, reading_id,
            point_id, sampled_at, row_json
        )
        SELECT ?, query_id, partition_id, reading_id, point_id, sampled_at, row_json
        FROM run_point_hits
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_null_verdicts",
        """
        INSERT INTO snapshot_null_verdicts(
            snapshot_id, query_id, source_type, partition_id, row_key,
            point_id, sampled_at, predicate_id, field_name, operator,
            threshold_text, predicate_sql, verdict
        )
        SELECT ?, query_id, source_type, partition_id, row_key,
               point_id, sampled_at, predicate_id, field_name, operator,
               threshold_text, predicate_sql, verdict
        FROM run_null_verdicts
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_join_results",
        """
        INSERT INTO snapshot_join_results(
            snapshot_id, query_id, join_task_key,
            pipe_partition_id, point_partition_id,
            row_index, row_json, row_signature
        )
        SELECT ?, query_id, join_task_key, pipe_partition_id,
               point_partition_id, row_index, row_json, row_signature
        FROM run_join_results
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_query_results",
        """
        INSERT INTO snapshot_query_results(
            snapshot_id, query_id, row_index, row_json, row_signature
        )
        SELECT ?, query_id, row_index, row_json, row_signature
        FROM run_query_results
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_consistency",
        """
        INSERT INTO snapshot_consistency(
            snapshot_id, query_id, is_consistent, downpush_count, central_count,
            missing_in_pushdown_json, missing_in_central_json,
            marker_mismatches_json, order_mismatches_json,
            divergence_partition, divergence_predicate, details_json
        )
        SELECT ?, query_id, is_consistent, downpush_count, central_count,
               missing_in_pushdown_json, missing_in_central_json,
               marker_mismatches_json, order_mismatches_json,
               divergence_partition, divergence_predicate, details_json
        FROM consistency_reports
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_violations",
        """
        INSERT INTO snapshot_violations(
            snapshot_id, id, point_id, sampled_at, metric, value,
            rule_name, threshold_low, threshold_high,
            point_partition_id, pipe_partition_id
        )
        SELECT ?, id, point_id, sampled_at, metric, value,
               rule_name, threshold_low, threshold_high,
               point_partition_id, pipe_partition_id
        FROM run_violations
        WHERE run_id = ?
        """,
    ),
    (
        "snapshot_manual_reviews",
        """
        INSERT INTO snapshot_manual_reviews(
            snapshot_id, id, point_id, metric, interval_start,
            interval_end, missing_count, point_partition_id, pipe_partition_id
        )
        SELECT ?, id, point_id, metric, interval_start,
               interval_end, missing_count, point_partition_id, pipe_partition_id
        FROM run_manual_reviews
        WHERE run_id = ?
        """,
    ),
)


SNAPSHOT_TO_RUN_COPIES = (
    (
        "run_partition_assignments",
        """
        INSERT INTO run_partition_assignments(
            run_id, entity_type, entity_key, entity_id, partition_id
        )
        SELECT ?, entity_type, entity_key, entity_id, partition_id
        FROM snapshot_partition_assignments
        WHERE snapshot_id = ?
        """,
    ),
    (
        "query_plans",
        """
        INSERT INTO query_plans(
            run_id, query_id, query_json, plan_json, join_type, created_at
        )
        SELECT ?, query_id, query_json, plan_json, join_type, ?
        FROM snapshot_query_plans
        WHERE snapshot_id = ?
        """,
    ),
    (
        "query_plan_partitions",
        """
        INSERT INTO query_plan_partitions(
            run_id, query_id, part_type, partition_id,
            predicate_sql, execution_sql, result_count
        )
        SELECT ?, query_id, part_type, partition_id,
               predicate_sql, execution_sql, result_count
        FROM snapshot_plan_partitions
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_pipe_hits",
        """
        INSERT INTO run_pipe_hits(
            run_id, query_id, partition_id, pipe_row_id, pipe_id, row_json
        )
        SELECT ?, query_id, partition_id, pipe_row_id, pipe_id, row_json
        FROM snapshot_pipe_hits
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_point_hits",
        """
        INSERT INTO run_point_hits(
            run_id, query_id, partition_id, reading_id, point_id, sampled_at, row_json
        )
        SELECT ?, query_id, partition_id, reading_id, point_id, sampled_at, row_json
        FROM snapshot_point_hits
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_null_verdicts",
        """
        INSERT INTO run_null_verdicts(
            run_id, query_id, source_type, partition_id, row_key,
            point_id, sampled_at, predicate_id, field_name, operator,
            threshold_text, predicate_sql, verdict
        )
        SELECT ?, query_id, source_type, partition_id, row_key,
               point_id, sampled_at, predicate_id, field_name, operator,
               threshold_text, predicate_sql, verdict
        FROM snapshot_null_verdicts
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_join_results",
        """
        INSERT INTO run_join_results(
            run_id, query_id, join_task_key, pipe_partition_id,
            point_partition_id, row_index, row_json, row_signature
        )
        SELECT ?, query_id, join_task_key, pipe_partition_id,
               point_partition_id, row_index, row_json, row_signature
        FROM snapshot_join_results
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_query_results",
        """
        INSERT INTO run_query_results(
            run_id, query_id, row_index, row_json, row_signature
        )
        SELECT ?, query_id, row_index, row_json, row_signature
        FROM snapshot_query_results
        WHERE snapshot_id = ?
        """,
    ),
    (
        "consistency_reports",
        """
        INSERT INTO consistency_reports(
            run_id, query_id, is_consistent, downpush_count, central_count,
            missing_in_pushdown_json, missing_in_central_json,
            marker_mismatches_json, order_mismatches_json,
            divergence_partition, divergence_predicate, details_json
        )
        SELECT ?, query_id, is_consistent, downpush_count, central_count,
               missing_in_pushdown_json, missing_in_central_json,
               marker_mismatches_json, order_mismatches_json,
               divergence_partition, divergence_predicate, details_json
        FROM snapshot_consistency
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_violations",
        """
        INSERT INTO run_violations(
            run_id, id, point_id, sampled_at, metric, value,
            rule_name, threshold_low, threshold_high,
            point_partition_id, pipe_partition_id
        )
        SELECT ?, id, point_id, sampled_at, metric, value,
               rule_name, threshold_low, threshold_high,
               point_partition_id, pipe_partition_id
        FROM snapshot_violations
        WHERE snapshot_id = ?
        """,
    ),
    (
        "run_manual_reviews",
        """
        INSERT INTO run_manual_reviews(
            run_id, id, point_id, metric, interval_start,
            interval_end, missing_count, point_partition_id, pipe_partition_id
        )
        SELECT ?, id, point_id, metric, interval_start,
               interval_end, missing_count, point_partition_id, pipe_partition_id
        FROM snapshot_manual_reviews
        WHERE snapshot_id = ?
        """,
    ),
)


PAYLOAD_TABLES = (
    ("query_plans", "query_id"),
    ("query_plan_partitions", "query_id, part_type, partition_id"),
    ("run_pipe_hits", "query_id, partition_id, pipe_row_id"),
    (
        "run_point_hits",
        "query_id, partition_id, reading_id",
    ),
    (
        "run_null_verdicts",
        "query_id, source_type, partition_id, row_key, predicate_id",
    ),
    (
        "run_join_results",
        "query_id, join_task_key, row_index",
    ),
    ("run_query_results", "query_id, row_index"),
    ("consistency_reports", "query_id"),
    ("run_violations", "id"),
    ("run_manual_reviews", "id"),
    ("run_partition_assignments", "entity_type, entity_key"),
)


def _run_payload_digest(conn: sqlite3.Connection, run_id: str) -> str:
    digest_parts: dict[str, Any] = {}
    for table, order_by in PAYLOAD_TABLES:
        rows = []
        for raw_row in conn.execute(
            f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order_by}",
            (run_id,),
        ):
            row = dict(raw_row)
            # A restored run has a different id and plan timestamp; neither is
            # part of the logical snapshot payload.
            row.pop("run_id", None)
            row.pop("created_at", None)
            rows.append(row)
        digest_parts[table] = {
            "count": len(rows),
            "sha256": sha256_text(canonical_json(rows)),
        }
    return sha256_text(canonical_json(digest_parts))


def create_snapshot(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run_id: {run_id}")
    if run["status"] != "complete":
        raise ValueError(f"run {run_id} is not complete (status={run['status']})")
    snapshot_id = snapshot_id or _snapshot_id()
    existing = conn.execute(
        "SELECT 1 FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if existing is not None:
        raise ValueError(f"snapshot already exists: {snapshot_id}")

    query_count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM query_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    consistency_row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN is_consistent = 1 THEN 1 ELSE 0 END) AS consistent
        FROM consistency_reports
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    digest = _run_payload_digest(conn, run_id)
    all_consistent = (
        consistency_row["total"] == consistency_row["consistent"]
        and consistency_row["total"] > 0
    )
    conn.execute(
        """
        INSERT INTO snapshots(
            snapshot_id, source_run_id, created_at, seed,
            partition_rules_version, partition_rules_json,
            data_fingerprint, source_data_fingerprint,
            payload_digest, query_count, all_consistent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            run_id,
            _now(),
            run["seed"],
            run["partition_rules_version"],
            run["partition_rules_json"],
            run["data_fingerprint"],
            run["source_data_fingerprint"],
            digest,
            query_count_row["n"],
            1 if all_consistent else 0,
        ),
    )
    for _table, sql in RUN_TO_SNAPSHOT_COPIES:
        conn.execute(sql, (snapshot_id, run_id))
    conn.commit()
    return get_snapshot(conn, snapshot_id)


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown snapshot_id: {snapshot_id}")
    result = dict(row)
    result["all_consistent"] = bool(result["all_consistent"])
    result["counts"] = {
        "query_plans": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_query_plans WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "plan_partitions": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_plan_partitions WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "point_hits": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_point_hits WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "pipe_hits": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_pipe_hits WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "null_verdicts": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_null_verdicts WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "join_results": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_join_results WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "query_results": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_query_results WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "violations": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_violations WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
        "manual_reviews": conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot_manual_reviews WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["n"],
    }
    return result


def assert_snapshot_rules_current(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    current_rules: PartitionRules | None = None,
) -> tuple[dict[str, Any], PartitionRules]:
    snapshot = get_snapshot(conn, snapshot_id)
    active = current_rules or get_active_rules(conn)
    snapshot_rules = PartitionRules.from_json(snapshot["partition_rules_json"])
    if (
        snapshot["partition_rules_version"] != active.version
        or snapshot_rules.to_dict() != active.to_dict()
    ):
        raise PartitionVersionMismatchError(
            "拒绝续算：快照分区规则版本 "
            f"{snapshot['partition_rules_version']!r} 与当前活动版本 "
            f"{active.version!r} 不一致。请先激活快照对应规则，"
            "或从匹配版本的快照继续。"
        )
    return snapshot, active


def hydrate_run_from_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    restored_run_id: str | None = None,
) -> str:
    snapshot = get_snapshot(conn, snapshot_id)
    restored_run_id = restored_run_id or (
        f"restored-{uuid.uuid4().hex[:12]}"
    )
    existing = conn.execute(
        "SELECT 1 FROM runs WHERE run_id = ?", (restored_run_id,)
    ).fetchone()
    if existing is not None:
        raise ValueError(f"restored run already exists: {restored_run_id}")
    created_at = _now()
    conn.execute(
        """
        INSERT INTO runs(
            run_id, mode, seed, partition_rules_version, partition_rules_json,
            source_run_id, snapshot_id, data_fingerprint,
            source_data_fingerprint, created_at, status
        ) VALUES (?, 'snapshot-restore', ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
        """,
        (
            restored_run_id,
            snapshot["seed"],
            snapshot["partition_rules_version"],
            snapshot["partition_rules_json"],
            snapshot["source_run_id"],
            snapshot_id,
            snapshot["data_fingerprint"],
            snapshot["source_data_fingerprint"],
            created_at,
        ),
    )
    for _table, sql in SNAPSHOT_TO_RUN_COPIES:
        # query_plans has an extra created_at placeholder; all others take two.
        if "SELECT ?, query_id, query_json" in sql:
            conn.execute(sql, (restored_run_id, created_at, snapshot_id))
        else:
            conn.execute(sql, (restored_run_id, snapshot_id))
    conn.commit()
    return restored_run_id


def resume_snapshot(
    engine: PushdownEngine,
    snapshot_id: str,
    *,
    query_changes: dict[str, dict[str, Any]] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Resume from a snapshot, refusing any partition-rule version drift."""

    snapshot, active = assert_snapshot_rules_current(
        engine.conn, snapshot_id, current_rules=engine.rules
    )
    # Also make the active version explicit in the error path if another
    # process changed DB metadata after the engine was opened.
    assert_snapshot_rules_current(engine.conn, snapshot_id)
    restored_run_id = hydrate_run_from_snapshot(engine.conn, snapshot_id)
    engine.rules = active
    result = engine.run_incremental(
        restored_run_id,
        query_changes=query_changes,
        run_id=run_id,
        snapshot_id=snapshot_id,
        verify_against_full=True,
    )
    result["resumed_from_snapshot"] = snapshot_id
    result["restored_previous_run_id"] = restored_run_id
    return result


def verify_snapshot_digest(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    snapshot = get_snapshot(conn, snapshot_id)
    restored = hydrate_run_from_snapshot(conn, snapshot_id)
    try:
        digest = _run_payload_digest(conn, restored)
    finally:
        for table in (
            "query_plans",
            "query_plan_partitions",
            "run_pipe_hits",
            "run_point_hits",
            "run_null_verdicts",
            "run_join_results",
            "run_query_results",
            "consistency_reports",
            "run_violations",
            "run_manual_reviews",
            "run_partition_assignments",
            "runs",
        ):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (restored,))
        conn.commit()
    return {
        "snapshot_id": snapshot_id,
        "expected_digest": snapshot["payload_digest"],
        "restored_digest": digest,
        "valid": snapshot["payload_digest"] == digest,
    }

