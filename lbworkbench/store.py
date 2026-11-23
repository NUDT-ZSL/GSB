from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .db import WorkbenchDB, connect
from .errors import WorkbenchError
from .merge import merge_snapshot
from .snapshots import parse_package
from .util import (
    canonical_json,
    compare_versions,
    pretty_json,
    sha256_json,
)


EMPTY_STATUS = {
    "schema": "lb-workbench-status/v1",
    "current_snapshot": None,
    "clock_waterline_ms": 0,
    "committed_rounds": {"count": 0, "rounds": []},
    "observed_error_codes": [],
    "probe_errors": [],
    "instances": [],
}


def _snapshot_public(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_version": snapshot["package_version"],
        "package_digest": snapshot["package_digest"],
        "logical_clock_ms": snapshot["logical_clock_ms"],
        "state_generation_floor": snapshot["state_generation_floor"],
        "instance_set_sha256": snapshot["instance_set_sha256"],
        "instance_ids": snapshot["instance_ids"],
        "source_hashes": snapshot["source_hashes"],
        "source_hashes_sha256": snapshot["source_hashes_sha256"],
        "v1": snapshot["v1"],
        "v2": snapshot["v2"],
    }


def _baseline_state_digest(merged: dict[str, Any]) -> str:
    rows = []
    for record in merged["instances"]:
        rows.append(
            {
                "instance_id": record["instance_id"],
                "health_status": record["health_status"],
                "state_generation": record["state_generation"],
                "consecutive_failures": record["consecutive_failures"],
                "consecutive_successes": record["consecutive_successes"],
                "effective_weight": record["effective_weight"],
                "tombstone": record["tombstone"],
                "manual": record["manual"],
            }
        )
    return sha256_json(rows)


def _classify_load(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    current_version: str,
) -> str:
    if not current_version:
        return "full_replacement"
    previous = conn.execute(
        "SELECT * FROM snapshots WHERE version = ?", (current_version,)
    ).fetchone()
    if previous is None:
        return "full_replacement"
    previous_package = json.loads(previous["package_json"])
    previous_shards = {
        shard["sha256"] for shard in previous_package["v2"].get("shards", [])
    }
    new_shards = {shard["sha256"] for shard in snapshot["v2"].get("shards", [])}
    previous_ids = set(previous_package.get("instance_ids", []))
    new_ids = set(snapshot["instance_ids"])
    unchanged_shards = previous_shards.issubset(new_shards)
    appended_instances = previous_ids.issubset(new_ids)
    generation_not_rolled_back = (
        snapshot["state_generation_floor"] >= previous["state_generation_floor"]
    )
    if unchanged_shards and appended_instances and generation_not_rolled_back:
        if previous_shards == new_shards and previous_ids == new_ids:
            return "no_op"
        return "incremental_append"
    return "full_replacement"


_STAGE_TABLE_STATEMENTS = (
    "DROP TABLE IF EXISTS temp.stage_records",
    "DROP TABLE IF EXISTS temp.stage_merged",
    "DROP TABLE IF EXISTS temp.stage_state",
    """CREATE TEMP TABLE stage_records (
    source_format TEXT NOT NULL,
    canonical_instance_id TEXT NOT NULL,
    original_instance_id TEXT NOT NULL,
    record_json TEXT NOT NULL
)""",
    """CREATE TEMP TABLE stage_merged (
    instance_id TEXT PRIMARY KEY,
    selected_source TEXT,
    health_status TEXT NOT NULL,
    state_generation INTEGER NOT NULL,
    consecutive_failures INTEGER,
    consecutive_successes INTEGER,
    effective_weight INTEGER NOT NULL,
    base_weight INTEGER NOT NULL,
    failure_at_ms INTEGER,
    tombstone INTEGER NOT NULL,
    manual INTEGER NOT NULL,
    sources_json TEXT NOT NULL,
    decision_json TEXT NOT NULL
)""",
    """CREATE TEMP TABLE stage_state (
    instance_id TEXT PRIMARY KEY,
    health_status TEXT NOT NULL,
    state_generation INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    consecutive_successes INTEGER NOT NULL,
    effective_weight INTEGER NOT NULL,
    base_weight INTEGER NOT NULL,
    failure_at_ms INTEGER,
    tombstone INTEGER NOT NULL
)""",
)


def load_package(db: WorkbenchDB, package_path: str | Path) -> dict[str, Any]:
    # Parsing/validation intentionally completes before any write transaction.
    snapshot = parse_package(package_path)
    merged = merge_snapshot(snapshot)
    db.initialize()

    with db.lock:
        conn = connect(db.db_path)
        try:
            for statement in _STAGE_TABLE_STATEMENTS:
                conn.execute(statement)
            conn.execute("BEGIN IMMEDIATE")
            try:
                current_version = db.get_meta(conn, "current_snapshot_version")
                if current_version:
                    current = conn.execute(
                        "SELECT * FROM snapshots WHERE version = ?",
                        (current_version,),
                    ).fetchone()
                    if current is None:
                        raise WorkbenchError(
                            "E_STATE_CORRUPT",
                            "current snapshot pointer references a missing snapshot",
                            {"current_snapshot_version": current_version},
                            http_status=500,
                        )
                    version_order = compare_versions(
                        snapshot["package_version"], current_version
                    )
                    if version_order < 0:
                        raise WorkbenchError(
                            "E_VERSION_ROLLBACK",
                            "snapshot package version is older than the current version",
                            {
                                "incoming_version": snapshot["package_version"],
                                "current_version": current_version,
                            },
                            http_status=409,
                            exit_code=3,
                        )
                    if (
                        snapshot["state_generation_floor"]
                        < current["state_generation_floor"]
                    ):
                        raise WorkbenchError(
                            "E_VERSION_ROLLBACK",
                            "snapshot state generation floor is below the current version",
                            {
                                "incoming_generation_floor": snapshot[
                                    "state_generation_floor"
                                ],
                                "current_generation_floor": current[
                                    "state_generation_floor"
                                ],
                            },
                            http_status=409,
                            exit_code=3,
                        )
                    if version_order == 0:
                        if snapshot["package_digest"] == current["package_digest"]:
                            conn.execute("ROLLBACK")
                            return {
                                "ok": True,
                                "result": "no_op",
                                "version": current_version,
                                "package_digest": snapshot["package_digest"],
                                "current_snapshot_version": current_version,
                            }
                        raise WorkbenchError(
                            "E_VERSION_CONFLICT",
                            "same snapshot version has different content",
                            {
                                "version": snapshot["package_version"],
                                "current_package_digest": current["package_digest"],
                                "incoming_package_digest": snapshot["package_digest"],
                            },
                            http_status=409,
                        )

                load_kind = _classify_load(conn, snapshot, current_version)
                if load_kind == "no_op":
                    conn.execute("ROLLBACK")
                    return {
                        "ok": True,
                        "result": "no_op",
                        "version": current_version,
                        "incoming_version": snapshot["package_version"],
                        "package_digest": snapshot["package_digest"],
                        "current_snapshot_version": current_version,
                    }
                public_snapshot = _snapshot_public(snapshot)
                baseline_digest = _baseline_state_digest(merged)
                for source_format, source in (("v1", snapshot["v1"]), ("v2", snapshot["v2"])):
                    for instance_id in sorted(source["instances"]):
                        item = source["instances"][instance_id]
                        conn.execute(
                            "INSERT INTO temp.stage_records VALUES(?, ?, ?, ?)",
                            (
                                source_format,
                                instance_id,
                                item["original_instance_id"],
                                canonical_json(item),
                            ),
                        )

                for record in merged["instances"]:
                    base_weight = int(
                        record["effective_weight"]
                        if record["effective_weight"] is not None
                        else 0
                    )
                    conn.execute(
                        "INSERT INTO temp.stage_merged VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["instance_id"],
                            record["selected_source"],
                            record["health_status"],
                            record["state_generation"],
                            record["consecutive_failures"],
                            record["consecutive_successes"],
                            record["effective_weight"],
                            base_weight,
                            record["failure_at_ms"],
                            int(record["tombstone"]),
                            int(record["manual"]),
                            canonical_json(record["sources"]),
                            canonical_json(record["decision"]),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO temp.stage_state VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["instance_id"],
                            record["health_status"],
                            record["state_generation"],
                            record["consecutive_failures"] or 0,
                            record["consecutive_successes"] or 0,
                            record["effective_weight"] or 0,
                            base_weight,
                            record["failure_at_ms"],
                            int(record["tombstone"]),
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO snapshots(
                        version, package_digest, instance_set_sha256,
                        logical_clock_ms, state_generation_floor, package_json,
                        source_hashes_json, v1_snapshot_id, v2_snapshot_id,
                        merged_digest, baseline_state_digest, record_count, load_kind
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot["package_version"],
                        snapshot["package_digest"],
                        snapshot["instance_set_sha256"],
                        snapshot["logical_clock_ms"],
                        snapshot["state_generation_floor"],
                        canonical_json(public_snapshot),
                        canonical_json(snapshot["source_hashes"]),
                        snapshot["v1"]["snapshot_id"],
                        snapshot["v2"]["snapshot_id"],
                        merged["merged_digest"],
                        baseline_digest,
                        len(snapshot["instance_ids"]),
                        load_kind,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO snapshot_records(
                        snapshot_version, source_format, canonical_instance_id,
                        original_instance_id, record_json
                    )
                    SELECT ?, source_format, canonical_instance_id,
                           original_instance_id, record_json
                    FROM temp.stage_records
                    ORDER BY source_format, canonical_instance_id
                    """,
                    (snapshot["package_version"],),
                )
                conn.execute(
                    """
                    INSERT INTO merged_instances(
                        snapshot_version, instance_id, selected_source,
                        health_status, state_generation, consecutive_failures,
                        consecutive_successes, effective_weight, base_weight,
                        failure_at_ms, tombstone, manual, sources_json, decision_json
                    )
                    SELECT ?, instance_id, selected_source, health_status,
                           state_generation, consecutive_failures,
                           consecutive_successes, effective_weight, base_weight,
                           failure_at_ms, tombstone, manual, sources_json,
                           decision_json
                    FROM temp.stage_merged
                    ORDER BY instance_id
                    """,
                    (snapshot["package_version"],),
                )
                conn.execute(
                    """
                    INSERT INTO instance_state(
                        snapshot_version, instance_id, health_status,
                        state_generation, consecutive_failures,
                        consecutive_successes, effective_weight, base_weight,
                        failure_at_ms, tombstone, revision, last_round_id
                    )
                    SELECT ?, instance_id, health_status, state_generation,
                           consecutive_failures, consecutive_successes,
                           effective_weight, base_weight, failure_at_ms,
                           tombstone, 0, NULL
                    FROM temp.stage_state
                    ORDER BY instance_id
                    """,
                    (snapshot["package_version"],),
                )

                if os.environ.get("LB_CRASH_DURING_LOAD") == "1":
                    os._exit(98)

                db.set_meta(conn, "current_snapshot_version", snapshot["package_version"])
                watermark = int(db.get_meta(conn, "clock_waterline_ms", "0"))
                db.set_meta(
                    conn,
                    "clock_waterline_ms",
                    str(max(watermark, snapshot["logical_clock_ms"])),
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "result": load_kind,
                    "version": snapshot["package_version"],
                    "package_digest": snapshot["package_digest"],
                    "instance_set_sha256": snapshot["instance_set_sha256"],
                    "instance_count": len(snapshot["instance_ids"]),
                    "source_hashes": snapshot["source_hashes"],
                    "current_snapshot_version": snapshot["package_version"],
                }
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()


def get_current_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    current_version = ""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'current_snapshot_version'"
    ).fetchone()
    if row is not None:
        current_version = str(row["value"])
    if not current_version:
        return None
    snapshot = conn.execute(
        "SELECT * FROM snapshots WHERE version = ?", (current_version,)
    ).fetchone()
    if snapshot is None:
        raise WorkbenchError(
            "E_STATE_CORRUPT",
            "current snapshot pointer references a missing snapshot",
            {"current_snapshot_version": current_version},
            http_status=500,
        )
    result = dict(snapshot)
    result["package_json"] = json.loads(result["package_json"])
    result["source_hashes_json"] = json.loads(result["source_hashes_json"])
    return result


def get_status(db: WorkbenchDB) -> dict[str, Any]:
    db.initialize()
    with db.connection() as conn:
        conn.execute("BEGIN")
        try:
            snapshot = get_current_snapshot(conn)
            watermark = int(db.get_meta(conn, "clock_waterline_ms", "0"))
            if snapshot is None:
                conn.execute("COMMIT")
                return {
                    **EMPTY_STATUS,
                    "clock_waterline_ms": watermark,
                }
            version = snapshot["version"]
            state_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM instance_state WHERE snapshot_version = ?",
                    (version,),
                )
            }
            merged_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM merged_instances WHERE snapshot_version = ?",
                    (version,),
                )
            }
            drift_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM drift_attributions WHERE snapshot_version = ?",
                    (version,),
                )
            }
            committed_rounds = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT round_id, plan_id, round_index, logical_clock_ms,
                           expected_count, completed_count
                    FROM rounds
                    WHERE snapshot_version = ? AND status = 'committed'
                    ORDER BY logical_clock_ms, plan_id, round_index
                    """,
                    (version,),
                )
            ]
            error_codes = sorted(
                {
                    str(row["error_code"])
                    for row in conn.execute(
                        """
                        SELECT DISTINCT o.error_code
                        FROM probe_observations o
                        JOIN rounds r ON r.round_id = o.round_id
                        WHERE r.snapshot_version = ?
                          AND r.status = 'committed'
                          AND o.error_code IS NOT NULL
                        """,
                        (version,),
                    )
                }
            )
            probe_errors = []
            for row in conn.execute(
                """
                SELECT o.instance_id, o.error_code, o.result_json,
                       r.round_id, r.round_index, r.logical_clock_ms
                FROM probe_observations o
                JOIN rounds r ON r.round_id = o.round_id
                WHERE r.snapshot_version = ?
                  AND r.status = 'committed'
                  AND o.error_code IS NOT NULL
                ORDER BY r.logical_clock_ms, r.round_index, o.instance_id
                """,
                (version,),
            ):
                detail = json.loads(row["result_json"])
                probe_errors.append(
                    {
                        "instance_id": row["instance_id"],
                        "round_id": row["round_id"],
                        "round_index": row["round_index"],
                        "logical_clock_ms": row["logical_clock_ms"],
                        "error_code": row["error_code"],
                        "target": detail.get("target"),
                        "expected_status": detail.get("expected_status"),
                        "http_status": detail.get("http_status"),
                        "request_budget_ms": detail.get("request_budget_ms"),
                        "detail": detail.get("detail"),
                    }
                )
            instances = []
            for instance_id in sorted(merged_rows):
                merged = merged_rows[instance_id]
                state = state_rows.get(instance_id)
                if state is None:
                    raise WorkbenchError(
                        "E_STATE_CORRUPT",
                        "merged instance has no persistent state row",
                        {"instance_id": instance_id, "snapshot_version": version},
                        http_status=500,
                    )
                sources = json.loads(merged["sources_json"])
                source_versions = {
                    source_format: source["package_version"]
                    for source_format, source in sorted(sources.items())
                }
                drift = None
                if instance_id in drift_rows:
                    row = drift_rows[instance_id]
                    drift = {
                        "cause": row["cause"],
                        "round_id": row["round_id"],
                        "round_index": row["round_index"],
                        "logical_clock_ms": row["logical_clock_ms"],
                        "evidence": json.loads(row["evidence_json"]),
                    }
                last_round = None
                if state.get("last_round_id"):
                    round_row = conn.execute(
                        """
                        SELECT round_id, plan_id, round_index, logical_clock_ms
                        FROM rounds WHERE round_id = ?
                        """,
                        (state["last_round_id"],),
                    ).fetchone()
                    if round_row is not None:
                        last_round = dict(round_row)
                instances.append(
                    {
                        "instance_id": instance_id,
                        "selected_source": merged["selected_source"],
                        "source_versions": source_versions,
                        "selected_snapshot_version": version,
                        "health_status": state["health_status"],
                        "state_generation": state["state_generation"],
                        "consecutive_failures": state["consecutive_failures"],
                        "consecutive_successes": state["consecutive_successes"],
                        "effective_weight": state["effective_weight"],
                        "failure_at_ms": state["failure_at_ms"],
                        "tombstone": bool(state["tombstone"]),
                        "manual": bool(merged["manual"]),
                        "sources": sources,
                        "decision": json.loads(merged["decision_json"]),
                        "drift": drift,
                        "last_round": last_round,
                        "revision": state["revision"],
                    }
                )
            result = {
                "schema": "lb-workbench-status/v1",
                "current_snapshot": {
                    "version": version,
                    "package_digest": snapshot["package_digest"],
                    "instance_set_sha256": snapshot["instance_set_sha256"],
                    "logical_clock_ms": snapshot["logical_clock_ms"],
                    "state_generation_floor": snapshot["state_generation_floor"],
                    "load_kind": snapshot["load_kind"],
                    "v1_snapshot_id": snapshot["v1_snapshot_id"],
                    "v2_snapshot_id": snapshot["v2_snapshot_id"],
                    "source_hashes": snapshot["source_hashes_json"],
                },
                "clock_waterline_ms": watermark,
                "committed_rounds": {
                    "count": len(committed_rounds),
                    "max_logical_clock_ms": (
                        max((row["logical_clock_ms"] for row in committed_rounds), default=0)
                    ),
                    "rounds": committed_rounds,
                },
                "observed_error_codes": error_codes,
                "probe_errors": probe_errors,
                "instances": instances,
            }
            conn.execute("COMMIT")
            return result
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def get_merge_view(db: WorkbenchDB) -> dict[str, Any]:
    status = get_status(db)
    if status["current_snapshot"] is None:
        return {
            "schema": "lb-merge-view/v1",
            "snapshot_version": None,
            "logical_clock_ms": 0,
            "instances": [],
        }
    instances = []
    for item in status["instances"]:
        instances.append(
            {
                "instance_id": item["instance_id"],
                "selected_source": item["selected_source"],
                "source_versions": item["source_versions"],
                "health_status": item["health_status"],
                "state_generation": item["state_generation"],
                "consecutive_failures": item["consecutive_failures"],
                "consecutive_successes": item["consecutive_successes"],
                "effective_weight": item["effective_weight"],
                "failure_at_ms": item["failure_at_ms"],
                "tombstone": item["tombstone"],
                "manual": item["manual"],
                "sources": item["sources"],
                "decision": item["decision"],
            }
        )
    return {
        "schema": "lb-merge-view/v1",
        "snapshot_version": status["current_snapshot"]["version"],
        "logical_clock_ms": status["current_snapshot"]["logical_clock_ms"],
        "instances": instances,
    }


def list_snapshots(db: WorkbenchDB) -> dict[str, Any]:
    db.initialize()
    with db.connection() as conn:
        current = db.get_meta(conn, "current_snapshot_version")
        rows = []
        for row in conn.execute("SELECT * FROM snapshots ORDER BY version"):
            rows.append(
                {
                    "version": row["version"],
                    "package_digest": row["package_digest"],
                    "instance_set_sha256": row["instance_set_sha256"],
                    "logical_clock_ms": row["logical_clock_ms"],
                    "state_generation_floor": row["state_generation_floor"],
                    "record_count": row["record_count"],
                    "load_kind": row["load_kind"],
                    "current": row["version"] == current,
                }
            )
        return {
            "schema": "lb-snapshots/v1",
            "current_snapshot_version": current or None,
            "snapshots": sorted(rows, key=lambda item: item["version"]),
        }


def list_rounds(db: WorkbenchDB) -> dict[str, Any]:
    db.initialize()
    with db.connection() as conn:
        rows = []
        for row in conn.execute(
            """
            SELECT round_id, plan_id, round_index, snapshot_version,
                   logical_clock_ms, status, expected_count, completed_count
            FROM rounds
            ORDER BY logical_clock_ms, plan_id, round_index, snapshot_version
            """
        ):
            missing = [
                item["instance_id"]
                for item in conn.execute(
                    """
                    SELECT expected.value AS instance_id
                    FROM rounds r, json_each(r.expected_instance_ids_json) expected
                    LEFT JOIN probe_observations o
                      ON o.round_id = r.round_id
                     AND o.instance_id = expected.value
                    WHERE r.round_id = ? AND o.instance_id IS NULL
                    ORDER BY expected.value
                    """,
                    (row["round_id"],),
                )
            ]
            item = dict(row)
            item["missing_instance_ids"] = missing
            rows.append(item)
        return {
            "schema": "lb-rounds/v1",
            "rounds": sorted(
                rows,
                key=lambda item: (
                    item["logical_clock_ms"],
                    item["plan_id"],
                    item["round_index"],
                    item["snapshot_version"],
                ),
            ),
        }


def verify_state(db: WorkbenchDB) -> dict[str, Any]:
    db.initialize()
    differences: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    with db.connection() as conn:
        conn.execute("BEGIN")
        try:
            snapshot = get_current_snapshot(conn)
            watermark = int(db.get_meta(conn, "clock_waterline_ms", "0"))
            if snapshot is None:
                conn.execute("COMMIT")
                return {
                    "schema": "lb-verify/v1",
                    "ok": True,
                    "differences": [],
                    "checks": {"current_snapshot_present": False},
                }
            version = snapshot["version"]
            merged_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM merged_instances WHERE snapshot_version = ? ORDER BY instance_id",
                    (version,),
                )
            ]
            state_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM instance_state WHERE snapshot_version = ? ORDER BY instance_id",
                    (version,),
                )
            ]
            source_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT source_format, canonical_instance_id, original_instance_id,
                           record_json
                    FROM snapshot_records
                    WHERE snapshot_version = ?
                    ORDER BY source_format, canonical_instance_id
                    """,
                    (version,),
                )
            ]
            checks["current_snapshot_present"] = True
            checks["merged_instance_count"] = len(merged_rows)
            checks["state_instance_count"] = len(state_rows)
            checks["source_record_count"] = len(source_rows)
            stored_package = snapshot["package_json"]
            if stored_package.get("source_hashes") != snapshot["source_hashes_json"]:
                differences.append(
                    {
                        "path": "snapshot.source_hashes",
                        "expected": stored_package.get("source_hashes"),
                        "actual": snapshot["source_hashes_json"],
                    }
                )
            source_hashes_digest = sha256_json(snapshot["source_hashes_json"])
            checks["source_hashes_sha256"] = source_hashes_digest
            if (
                stored_package.get("source_hashes_sha256") is not None
                and stored_package.get("source_hashes_sha256") != source_hashes_digest
            ):
                differences.append(
                    {
                        "path": "snapshot.source_hashes_sha256",
                        "expected": stored_package.get("source_hashes_sha256"),
                        "actual": source_hashes_digest,
                    }
                )
            recomputed_package_digest = sha256_json(
                {
                    "package": {
                        "package_version": snapshot["version"],
                        "logical_clock_ms": snapshot["logical_clock_ms"],
                        "state_generation_floor": snapshot["state_generation_floor"],
                    },
                    "v1": stored_package["v1"],
                    "v2": stored_package["v2"],
                    "instance_set_sha256": snapshot["instance_set_sha256"],
                    "source_hashes": snapshot["source_hashes_json"],
                }
            )
            checks["recomputed_package_digest"] = recomputed_package_digest
            if recomputed_package_digest != snapshot["package_digest"]:
                differences.append(
                    {
                        "path": "snapshot.package_digest",
                        "expected": snapshot["package_digest"],
                        "actual": recomputed_package_digest,
                    }
                )

            merged_ids = [row["instance_id"] for row in merged_rows]
            state_ids = [row["instance_id"] for row in state_rows]
            if merged_ids != state_ids:
                differences.append(
                    {
                        "path": "instance_state.instance_ids",
                        "expected": merged_ids,
                        "actual": state_ids,
                    }
                )
            instance_digest = sha256_json(merged_ids)
            checks["recomputed_instance_set_sha256"] = instance_digest
            if instance_digest != snapshot["instance_set_sha256"]:
                differences.append(
                    {
                        "path": "snapshot.instance_set_sha256",
                        "expected": snapshot["instance_set_sha256"],
                        "actual": instance_digest,
                    }
                )
            if snapshot["record_count"] != len(merged_rows):
                differences.append(
                    {
                        "path": "snapshot.record_count",
                        "expected": len(merged_rows),
                        "actual": snapshot["record_count"],
                    }
                )

            merged_for_digest = []
            for row in merged_rows:
                merged_for_digest.append(
                    {
                        "instance_id": row["instance_id"],
                        "selected_source": row["selected_source"],
                        "health_status": row["health_status"],
                        "state_generation": row["state_generation"],
                        "consecutive_failures": row["consecutive_failures"],
                        "consecutive_successes": row["consecutive_successes"],
                        "effective_weight": row["effective_weight"],
                        "failure_at_ms": row["failure_at_ms"],
                        "tombstone": bool(row["tombstone"]),
                        "manual": bool(row["manual"]),
                        "sources": json.loads(row["sources_json"]),
                        "decision": json.loads(row["decision_json"]),
                    }
                )
            merged_digest = sha256_json(merged_for_digest)
            checks["recomputed_merged_digest"] = merged_digest
            if merged_digest != snapshot["merged_digest"]:
                differences.append(
                    {
                        "path": "snapshot.merged_digest",
                        "expected": snapshot["merged_digest"],
                        "actual": merged_digest,
                    }
                )

            expected_source_ids = set()
            duplicate_keys = set()
            for row in source_rows:
                key = (row["source_format"], row["canonical_instance_id"])
                if key in duplicate_keys:
                    differences.append(
                        {
                            "path": "snapshot_records.duplicate_key",
                            "expected": "unique",
                            "actual": list(key),
                        }
                    )
                duplicate_keys.add(key)
                expected_source_ids.add(row["canonical_instance_id"])
                try:
                    json.loads(row["record_json"])
                except json.JSONDecodeError:
                    differences.append(
                        {
                            "path": "snapshot_records.invalid_json",
                            "expected": "valid canonical JSON",
                            "actual": {
                                "source_format": row["source_format"],
                                "instance_id": row["canonical_instance_id"],
                            },
                        }
                    )
            if expected_source_ids != set(merged_ids):
                differences.append(
                    {
                        "path": "merged_instances.source_coverage",
                        "expected": sorted(set(merged_ids)),
                        "actual": sorted(expected_source_ids),
                    }
                )
            checks["source_coverage_ok"] = expected_source_ids == set(merged_ids)

            committed_rounds = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM rounds
                    WHERE snapshot_version = ? AND status = 'committed'
                    ORDER BY logical_clock_ms, round_index
                    """,
                    (version,),
                )
            ]
            max_committed_clock = max(
                (row["logical_clock_ms"] for row in committed_rounds), default=0
            )
            checks["committed_round_count"] = len(committed_rounds)
            checks["max_committed_clock_ms"] = max_committed_clock
            checks["clock_waterline_ms"] = watermark
            if watermark < max_committed_clock:
                differences.append(
                    {
                        "path": "meta.clock_waterline_ms",
                        "expected": max_committed_clock,
                        "actual": watermark,
                    }
                )

            for row in committed_rounds:
                expected_ids = json.loads(row["expected_instance_ids_json"])
                observed_ids = [
                    item["instance_id"]
                    for item in conn.execute(
                        """
                        SELECT instance_id FROM probe_observations
                        WHERE round_id = ? ORDER BY instance_id
                        """,
                        (row["round_id"],),
                    )
                ]
                if sorted(expected_ids) != observed_ids:
                    differences.append(
                        {
                            "path": f"rounds[{row['round_id']}].observations",
                            "expected": sorted(expected_ids),
                            "actual": observed_ids,
                        }
                    )
            checks["committed_round_observations_ok"] = not any(
                item["path"].startswith("rounds[") for item in differences
            )
            counter_errors = 0
            for state in state_rows:
                if not state["last_round_id"]:
                    continue
                observation = conn.execute(
                    """
                    SELECT o.observed_health, r.status, r.snapshot_version
                    FROM probe_observations o
                    JOIN rounds r ON r.round_id = o.round_id
                    WHERE o.round_id = ? AND o.instance_id = ?
                    """,
                    (state["last_round_id"], state["instance_id"]),
                ).fetchone()
                if observation is None or observation["status"] != "committed":
                    counter_errors += 1
                    differences.append(
                        {
                            "path": f"instance_state[{state['instance_id']}].last_round_id",
                            "expected": "a committed observation",
                            "actual": state["last_round_id"],
                        }
                    )
                    continue
                if observation["observed_health"] == "healthy":
                    coherent = (
                        state["consecutive_failures"] == 0
                        and state["consecutive_successes"] >= 1
                    )
                else:
                    coherent = (
                        state["consecutive_successes"] == 0
                        and state["consecutive_failures"] >= 1
                    )
                if not coherent:
                    counter_errors += 1
                    differences.append(
                        {
                            "path": f"instance_state[{state['instance_id']}].counters",
                            "expected": {
                                "last_observation": observation["observed_health"],
                                "counter_opposite_of_observation": 0,
                            },
                            "actual": {
                                "consecutive_failures": state["consecutive_failures"],
                                "consecutive_successes": state["consecutive_successes"],
                            },
                        }
                    )
            checks["counter_coherence_ok"] = counter_errors == 0

            for row in conn.execute(
                "SELECT * FROM drift_attributions WHERE snapshot_version = ?", (version,)
            ):
                round_row = conn.execute(
                    "SELECT status, snapshot_version FROM rounds WHERE round_id = ?",
                    (row["round_id"],),
                ).fetchone()
                if round_row is None or round_row["status"] != "committed":
                    differences.append(
                        {
                            "path": f"drift_attributions[{row['instance_id']}].round_id",
                            "expected": "committed round",
                            "actual": row["round_id"],
                        }
                    )
            checks["drift_references_ok"] = not any(
                item["path"].startswith("drift_attributions[") for item in differences
            )
            checks["current_version"] = version
            checks["package_digest"] = snapshot["package_digest"]
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    differences.sort(key=lambda item: canonical_json(item))
    return {
        "schema": "lb-verify/v1",
        "ok": not differences,
        "differences": differences,
        "checks": {key: checks[key] for key in sorted(checks)},
    }


def dumps(value: Any) -> str:
    return pretty_json(value)
