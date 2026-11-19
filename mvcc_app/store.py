"""SQLite-backed, generation-versioned snapshot store.

Writes create a complete candidate generation, commit it as ``staging``, and
then atomically move the single ``snapshot_head`` pointer to it.  Readers only
resolve a ready generation, so a terminated writer can leave an orphan staging
generation but can never expose a half-written version chain.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .util import canonical_json, sha256_json

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS snapshot_generations (
    generation INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('staging', 'ready')),
    base_generation INTEGER,
    digest TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snap_meta (
    generation INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    table_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    sessions_json TEXT NOT NULL,
    last_clock_ms INTEGER NOT NULL,
    next_version_id INTEGER NOT NULL,
    gc_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snap_txns (
    generation INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    start_clock_ms INTEGER NOT NULL,
    isolation TEXT NOT NULL,
    status TEXT NOT NULL,
    commit_clock_ms INTEGER,
    planned_commit_clock_ms INTEGER,
    planned_outcome TEXT,
    start_order INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (generation, txn_id)
);

CREATE TABLE IF NOT EXISTS snap_versions (
    generation INTEGER NOT NULL,
    version_id INTEGER NOT NULL,
    row_key TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    create_txn_id INTEGER NOT NULL,
    delete_txn_id INTEGER,
    create_clock_ms INTEGER NOT NULL,
    update_clock_ms INTEGER NOT NULL,
    delete_clock_ms INTEGER,
    data_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    state TEXT NOT NULL,
    prev_version_id INTEGER,
    merge_count INTEGER NOT NULL,
    lineage_txns_json TEXT NOT NULL,
    PRIMARY KEY (generation, version_id)
);

CREATE INDEX IF NOT EXISTS ix_versions_row
    ON snap_versions (generation, row_key, version_no);

CREATE TABLE IF NOT EXISTS snap_views (
    generation INTEGER NOT NULL,
    view_id TEXT NOT NULL,
    txn_id INTEGER NOT NULL,
    isolation TEXT NOT NULL,
    view_clock_ms INTEGER NOT NULL,
    snapshot_watermark_ms INTEGER NOT NULL,
    watermark_txn_id INTEGER NOT NULL,
    active_txns_json TEXT NOT NULL,
    pinned_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    view_digest TEXT NOT NULL,
    PRIMARY KEY (generation, view_id)
);

CREATE INDEX IF NOT EXISTS ix_views_status
    ON snap_views (generation, status, created_clock_ms);

CREATE TABLE IF NOT EXISTS snap_applied_ops (
    generation INTEGER NOT NULL,
    op_key TEXT NOT NULL,
    txn_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    seq INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    applied_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (generation, op_key)
);

CREATE TABLE IF NOT EXISTS snap_gc_runs (
    generation INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    collected_count INTEGER NOT NULL,
    retained_count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (generation, run_id)
);

CREATE TABLE IF NOT EXISTS snap_gc_retained (
    generation INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    version_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    view_ids_json TEXT NOT NULL,
    PRIMARY KEY (generation, run_id, version_id)
);
"""

GENERATION_TABLES = (
    "snap_meta",
    "snap_txns",
    "snap_versions",
    "snap_views",
    "snap_applied_ops",
    "snap_gc_runs",
    "snap_gc_retained",
)

Mutation = Callable[[dict[str, Any], dict[str, int]], tuple[Any, bool]]


class Store:
    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    @staticmethod
    def exists(db_path: str | Path) -> bool:
        path = Path(db_path)
        return path.exists() and path.stat().st_size > 0

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=max(0.05, self.busy_timeout_ms / 1_000),
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def initialize(
        self,
        table: dict[str, Any],
        config: dict[str, Any],
        sessions: dict[str, Any],
    ) -> dict[str, Any]:
        if self.exists(self.db_path):
            raise BusinessError(
                ErrorCode.DATABASE_EXISTS,
                "database already exists; use a new db path or remove the existing file",
                {"db_path": str(self.db_path)},
            )
        Path(config["store_dir"]).mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": 1,
            "table": table,
            "config": config,
            "sessions": sessions,
            "last_clock_ms": config["initial_clock_ms"],
            "next_version_id": 1,
            "transactions": {},
            "versions": {},
            "read_views": {},
            "applied_ops": {},
            "gc": {
                "watermark_txn": config["gc_watermark_txn"],
                "watermark_clock_ms": 0,
                "collected_total": 0,
                "run_count": 0,
            },
            "gc_runs": {},
            "gc_retained": [],
        }
        for session in sessions["sessions"]:
            txn_id = session["transaction_id"]
            state["transactions"][txn_id] = {
                "txn_id": txn_id,
                "start_clock_ms": session["start_clock_ms"],
                "isolation": session["isolation"],
                "status": "pending",
                "commit_clock_ms": None,
                "planned_commit_clock_ms": session["commit_clock_ms"],
                "planned_outcome": session["outcome"],
                "start_order": session["start_order"],
                "updated_clock_ms": 0,
            }
        connection = self._connect()
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute("BEGIN IMMEDIATE")
            digest = self._insert_generation(connection, 1, None, state, "ready")
            connection.execute(
                """
                INSERT INTO snapshot_head(id, generation, updated_clock_ms, digest)
                VALUES (1, ?, ?, ?)
                """,
                (1, state["last_clock_ms"], digest),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return {
            "generation": 1,
            "db_path": str(self.db_path),
            "store_dir": config["store_dir"],
            "last_clock_ms": state["last_clock_ms"],
        }

    def mutate(self, mutator: Mutation, *, crash_after_stage: bool = False) -> Any:
        last_retry_reason = "generation conflict"
        for attempt in range(80):
            connection = self._connect()
            staged_generation: int | None = None
            base_generation: int | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                base_generation, state = self._load_head_conn(connection)
                staged_generation = connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM snapshot_generations"
                ).fetchone()[0]
                context = {
                    "base_generation": base_generation,
                    "new_generation": staged_generation,
                }
                result, changed = mutator(state, context)
                if not changed:
                    connection.execute("ROLLBACK")
                    return result
                self._insert_generation(
                    connection,
                    staged_generation,
                    base_generation,
                    state,
                    "staging",
                )
                connection.execute("COMMIT")
            except BusinessError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_retry_reason = str(exc)
                    time.sleep(min(0.05, 0.002 * (attempt + 1)))
                    continue
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

            if crash_after_stage:
                os._exit(91)

            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                head_row = connection.execute(
                    "SELECT generation FROM snapshot_head WHERE id = 1"
                ).fetchone()
                if head_row is None or head_row["generation"] != base_generation:
                    connection.execute("ROLLBACK")
                    last_retry_reason = "snapshot head changed before swap"
                    self._cleanup_generation_rows(staged_generation)
                    time.sleep(min(0.05, 0.002 * (attempt + 1)))
                    continue
                generation_row = connection.execute(
                    """
                    SELECT status, base_generation
                    FROM snapshot_generations
                    WHERE generation = ?
                    """,
                    (staged_generation,),
                ).fetchone()
                if (
                    generation_row is None
                    or generation_row["status"] != "staging"
                    or generation_row["base_generation"] != base_generation
                ):
                    connection.execute("ROLLBACK")
                    last_retry_reason = "staged generation is no longer available"
                    continue
                staged_meta = connection.execute(
                    """
                    SELECT g.digest AS digest, m.last_clock_ms AS last_clock_ms
                    FROM snapshot_generations AS g
                    JOIN snap_meta AS m ON m.generation = g.generation
                    WHERE g.generation = ?
                    """,
                    (staged_generation,),
                ).fetchone()
                digest = staged_meta["digest"]
                connection.execute(
                    "UPDATE snapshot_generations SET status = 'ready' WHERE generation = ?",
                    (staged_generation,),
                )
                cursor = connection.execute(
                    """
                    UPDATE snapshot_head
                    SET generation = ?, updated_clock_ms = ?, digest = ?
                    WHERE id = 1 AND generation = ?
                    """,
                    (
                        staged_generation,
                        int(staged_meta["last_clock_ms"]),
                        digest,
                        base_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.execute("ROLLBACK")
                    last_retry_reason = "compare-and-swap of snapshot head failed"
                    self._cleanup_generation_rows(staged_generation)
                    time.sleep(min(0.05, 0.002 * (attempt + 1)))
                    continue
                connection.execute("COMMIT")
                return result
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_retry_reason = str(exc)
                    time.sleep(min(0.05, 0.002 * (attempt + 1)))
                    continue
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        raise BusinessError(
            ErrorCode.RECOVERY_CONFLICT,
            "could not publish a snapshot generation after concurrent contention",
            {"reason": last_retry_reason},
        )

    def load_head(self) -> tuple[int, dict[str, Any]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            generation, state = self._load_head_conn(connection)
            connection.execute("COMMIT")
            return generation, state
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def load_generation(self, generation: int | None = None) -> tuple[int, dict[str, Any]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            if generation is None:
                generation, state = self._load_head_conn(connection)
            else:
                state = self._load_generation_conn(connection, generation)
            connection.execute("COMMIT")
            return int(generation), state
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def cleanup_orphans(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            head_row = connection.execute(
                "SELECT generation FROM snapshot_head WHERE id = 1"
            ).fetchone()
            if head_row is None:
                raise BusinessError(
                    ErrorCode.DATABASE_NOT_INITIALIZED,
                    "snapshot head is missing",
                    {"db_path": str(self.db_path)},
                )
            head_generation = int(head_row["generation"])
            rows = connection.execute(
                """
                SELECT generation, base_generation, created_clock_ms
                FROM snapshot_generations
                WHERE status = 'staging' AND generation <> ?
                ORDER BY generation
                """,
                (head_generation,),
            ).fetchall()
            orphan_generations: list[int] = []
            half_written_versions = 0
            half_written_transactions: set[int] = set()
            for row in rows:
                generation = int(row["generation"])
                orphan_generations.append(generation)
                half_written_versions += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM snap_versions WHERE generation = ?",
                        (generation,),
                    ).fetchone()[0]
                )
                for txn_row in connection.execute(
                    "SELECT txn_id FROM snap_txns WHERE generation = ?",
                    (generation,),
                ).fetchall():
                    half_written_transactions.add(int(txn_row["txn_id"]))
                self._delete_generation_rows_conn(connection, generation)
            connection.execute("COMMIT")
            return {
                "head_generation": head_generation,
                "orphan_generations": orphan_generations,
                "half_written_versions": half_written_versions,
                "half_written_transactions": sorted(half_written_transactions),
                "cleaned": len(orphan_generations),
            }
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def cleanup_unpinned_generations(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            head_row = connection.execute(
                "SELECT generation FROM snapshot_head WHERE id = 1"
            ).fetchone()
            if head_row is None:
                raise BusinessError(
                    ErrorCode.DATABASE_NOT_INITIALIZED,
                    "snapshot head is missing",
                    {"db_path": str(self.db_path)},
                )
            head_generation = int(head_row["generation"])
            pinned = {
                int(row["pinned_generation"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT pinned_generation
                    FROM snap_views
                    WHERE generation = ? AND status = 'retained'
                    """,
                    (head_generation,),
                ).fetchall()
            }
            candidates = [
                int(row["generation"])
                for row in connection.execute(
                    "SELECT generation FROM snapshot_generations WHERE generation < ?",
                    (head_generation,),
                ).fetchall()
            ]
            removed: list[int] = []
            for generation in candidates:
                if generation not in pinned:
                    self._delete_generation_rows_conn(connection, generation)
                    removed.append(generation)
            connection.execute("COMMIT")
            return {"removed_generations": removed, "pinned_generations": sorted(pinned)}
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def inspect_integrity(self) -> dict[str, Any]:
        generation, state = self.load_head()
        errors: list[dict[str, Any]] = []
        verified_versions = 0
        for version in state["versions"].values():
            verified_versions += 1
            expected = sha256_json(version["data"])
            if expected != version["digest"]:
                errors.append(
                    {
                        "code": ErrorCode.VERSION_DIGEST_MISMATCH,
                        "version_id": version["version_id"],
                        "expected": expected,
                        "actual": version["digest"],
                    }
                )
            prev_id = version.get("prev_version_id")
            if prev_id is not None and prev_id not in state["versions"]:
                errors.append(
                    {
                        "code": ErrorCode.INVALID_STATE,
                        "version_id": version["version_id"],
                        "reason": "dangling_prev_version",
                        "prev_version_id": prev_id,
                    }
                )
        for view in state["read_views"].values():
            expected = canonical_json(view["active_txns"])
            actual = canonical_json(sorted(view["active_txns"]))
            if expected != actual:
                errors.append(
                    {
                        "code": ErrorCode.INVALID_STATE,
                        "view_id": view["view_id"],
                        "reason": "active_transaction_set_not_sorted",
                    }
                )
            expected_digest = sha256_json(view["core"])
            if expected_digest != view["digest"]:
                errors.append(
                    {
                        "code": ErrorCode.INVALID_STATE,
                        "view_id": view["view_id"],
                        "reason": "read_view_digest_mismatch",
                        "expected": expected_digest,
                        "actual": view["digest"],
                    }
                )
        return {
            "generation": generation,
            "verified_versions": verified_versions,
            "read_views": len(state["read_views"]),
            "errors": errors,
            "ok": not errors,
        }

    def _load_head_conn(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[int, dict[str, Any]]:
        row = connection.execute(
            "SELECT generation, digest FROM snapshot_head WHERE id = 1"
        ).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.DATABASE_NOT_INITIALIZED,
                "database is not initialized",
                {"db_path": str(self.db_path)},
            )
        generation = int(row["generation"])
        state = self._load_generation_conn(connection, generation)
        if sha256_json(state) != row["digest"]:
            raise BusinessError(
                ErrorCode.SNAPSHOT_DIGEST_MISMATCH,
                "current snapshot digest does not match persisted state",
                {"generation": generation},
            )
        return generation, state

    def _load_generation_conn(
        self,
        connection: sqlite3.Connection,
        generation: int,
    ) -> dict[str, Any]:
        generation_row = connection.execute(
            "SELECT status, digest FROM snapshot_generations WHERE generation = ?",
            (generation,),
        ).fetchone()
        if generation_row is None:
            raise BusinessError(
                ErrorCode.STALE_SNAPSHOT,
                "snapshot generation is no longer retained",
                {"generation": generation},
            )
        if generation_row["status"] != "ready":
            raise BusinessError(
                ErrorCode.STALE_SNAPSHOT,
                "snapshot generation is not published",
                {"generation": generation},
            )
        meta = connection.execute(
            "SELECT * FROM snap_meta WHERE generation = ?",
            (generation,),
        ).fetchone()
        if meta is None:
            raise BusinessError(
                ErrorCode.INVALID_STATE,
                "snapshot metadata is missing",
                {"generation": generation},
            )
        state: dict[str, Any] = {
            "schema_version": int(meta["schema_version"]),
            "table": self._json_load(meta["table_json"]),
            "config": self._json_load(meta["config_json"]),
            "sessions": self._json_load(meta["sessions_json"]),
            "last_clock_ms": int(meta["last_clock_ms"]),
            "next_version_id": int(meta["next_version_id"]),
            "gc": self._json_load(meta["gc_json"]),
            "transactions": {},
            "versions": {},
            "read_views": {},
            "applied_ops": {},
            "gc_runs": {},
            "gc_retained": [],
        }
        for row in connection.execute(
            "SELECT * FROM snap_txns WHERE generation = ?",
            (generation,),
        ).fetchall():
            txn_id = int(row["txn_id"])
            state["transactions"][txn_id] = {
                "txn_id": txn_id,
                "start_clock_ms": int(row["start_clock_ms"]),
                "isolation": row["isolation"],
                "status": row["status"],
                "commit_clock_ms": row["commit_clock_ms"],
                "planned_commit_clock_ms": row["planned_commit_clock_ms"],
                "planned_outcome": row["planned_outcome"],
                "start_order": int(row["start_order"]),
                "updated_clock_ms": int(row["updated_clock_ms"]),
            }
        for row in connection.execute(
            "SELECT * FROM snap_versions WHERE generation = ?",
            (generation,),
        ).fetchall():
            version_id = int(row["version_id"])
            state["versions"][version_id] = {
                "version_id": version_id,
                "row_key": row["row_key"],
                "version_no": int(row["version_no"]),
                "create_txn_id": int(row["create_txn_id"]),
                "delete_txn_id": row["delete_txn_id"],
                "create_clock_ms": int(row["create_clock_ms"]),
                "update_clock_ms": int(row["update_clock_ms"]),
                "delete_clock_ms": row["delete_clock_ms"],
                "data": self._json_load(row["data_json"]),
                "digest": row["digest"],
                "state": row["state"],
                "prev_version_id": row["prev_version_id"],
                "merge_count": int(row["merge_count"]),
                "lineage_txns": self._json_load(row["lineage_txns_json"]),
            }
        for row in connection.execute(
            "SELECT * FROM snap_views WHERE generation = ?",
            (generation,),
        ).fetchall():
            view_id = row["view_id"]
            active_txns = self._json_load(row["active_txns_json"])
            state["read_views"][view_id] = {
                "view_id": view_id,
                "txn_id": int(row["txn_id"]),
                "isolation": row["isolation"],
                "view_clock_ms": int(row["view_clock_ms"]),
                "snapshot_watermark_ms": int(row["snapshot_watermark_ms"]),
                "watermark_txn_id": int(row["watermark_txn_id"]),
                "active_txns": active_txns,
                "pinned_generation": int(row["pinned_generation"]),
                "status": row["status"],
                "created_clock_ms": int(row["created_clock_ms"]),
                "digest": row["view_digest"],
                "core": {
                    "txn_id": int(row["txn_id"]),
                    "isolation": row["isolation"],
                    "view_clock_ms": int(row["view_clock_ms"]),
                    "snapshot_watermark_ms": int(row["snapshot_watermark_ms"]),
                    "watermark_txn_id": int(row["watermark_txn_id"]),
                    "active_txns": active_txns,
                },
            }
        for row in connection.execute(
            "SELECT * FROM snap_applied_ops WHERE generation = ?",
            (generation,),
        ).fetchall():
            state["applied_ops"][row["op_key"]] = {
                "op_key": row["op_key"],
                "txn_id": int(row["txn_id"]),
                "phase": row["phase"],
                "seq": int(row["seq"]),
                "fingerprint": row["fingerprint"],
                "result": self._json_load(row["result_json"]),
                "applied_clock_ms": int(row["applied_clock_ms"]),
            }
        for row in connection.execute(
            "SELECT * FROM snap_gc_runs WHERE generation = ?",
            (generation,),
        ).fetchall():
            run_id = row["run_id"]
            state["gc_runs"][run_id] = {
                "run_id": run_id,
                "clock_ms": int(row["clock_ms"]),
                "collected_count": int(row["collected_count"]),
                "retained_count": int(row["retained_count"]),
                "details": self._json_load(row["details_json"]),
                "retained": [],
            }
        for row in connection.execute(
            "SELECT * FROM snap_gc_retained WHERE generation = ?",
            (generation,),
        ).fetchall():
            run = state["gc_runs"].get(row["run_id"])
            if run is not None:
                run["retained"].append(
                    {
                        "version_id": int(row["version_id"]),
                        "reason": row["reason"],
                        "view_ids": self._json_load(row["view_ids_json"]),
                    }
                )
        for run in state["gc_runs"].values():
            run["retained"].sort(key=lambda item: item["version_id"])
        if sha256_json(state) != generation_row["digest"]:
            raise BusinessError(
                ErrorCode.SNAPSHOT_DIGEST_MISMATCH,
                "snapshot generation digest does not match persisted rows",
                {"generation": generation},
            )
        return state

    def _insert_generation(
        self,
        connection: sqlite3.Connection,
        generation: int,
        base_generation: int | None,
        state: dict[str, Any],
        status: str,
    ) -> str:
        digest = sha256_json(state)
        connection.execute(
            """
            INSERT INTO snapshot_generations(
                generation, status, base_generation, digest, created_clock_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                generation,
                status,
                base_generation,
                digest,
                int(state["last_clock_ms"]),
            ),
        )
        connection.execute(
            """
            INSERT INTO snap_meta(
                generation, schema_version, table_json, config_json, sessions_json,
                last_clock_ms, next_version_id, gc_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation,
                int(state["schema_version"]),
                canonical_json(state["table"]),
                canonical_json(state["config"]),
                canonical_json(state["sessions"]),
                int(state["last_clock_ms"]),
                int(state["next_version_id"]),
                canonical_json(state["gc"]),
            ),
        )
        txn_rows = []
        for txn in state["transactions"].values():
            txn_rows.append(
                (
                    generation,
                    int(txn["txn_id"]),
                    int(txn["start_clock_ms"]),
                    txn["isolation"],
                    txn["status"],
                    txn["commit_clock_ms"],
                    txn.get("planned_commit_clock_ms"),
                    txn.get("planned_outcome"),
                    int(txn["start_order"]),
                    int(txn["updated_clock_ms"]),
                )
            )
        connection.executemany(
            """
            INSERT INTO snap_txns(
                generation, txn_id, start_clock_ms, isolation, status,
                commit_clock_ms, planned_commit_clock_ms, planned_outcome,
                start_order, updated_clock_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            txn_rows,
        )
        version_rows = []
        for version in state["versions"].values():
            version_rows.append(
                (
                    generation,
                    int(version["version_id"]),
                    version["row_key"],
                    int(version["version_no"]),
                    int(version["create_txn_id"]),
                    version["delete_txn_id"],
                    int(version["create_clock_ms"]),
                    int(version["update_clock_ms"]),
                    version["delete_clock_ms"],
                    canonical_json(version["data"]),
                    version["digest"],
                    version["state"],
                    version["prev_version_id"],
                    int(version["merge_count"]),
                    canonical_json(version["lineage_txns"]),
                )
            )
        connection.executemany(
            """
            INSERT INTO snap_versions(
                generation, version_id, row_key, version_no, create_txn_id,
                delete_txn_id, create_clock_ms, update_clock_ms, delete_clock_ms,
                data_json, digest, state, prev_version_id, merge_count,
                lineage_txns_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            version_rows,
        )
        view_rows = []
        for view in state["read_views"].values():
            view_rows.append(
                (
                    generation,
                    view["view_id"],
                    int(view["txn_id"]),
                    view["isolation"],
                    int(view["view_clock_ms"]),
                    int(view["snapshot_watermark_ms"]),
                    int(view["watermark_txn_id"]),
                    canonical_json(view["active_txns"]),
                    int(view["pinned_generation"]),
                    view["status"],
                    int(view["created_clock_ms"]),
                    view["digest"],
                )
            )
        connection.executemany(
            """
            INSERT INTO snap_views(
                generation, view_id, txn_id, isolation, view_clock_ms,
                snapshot_watermark_ms, watermark_txn_id, active_txns_json,
                pinned_generation, status, created_clock_ms, view_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            view_rows,
        )
        applied_rows = []
        for applied in state["applied_ops"].values():
            applied_rows.append(
                (
                    generation,
                    applied["op_key"],
                    int(applied["txn_id"]),
                    applied["phase"],
                    int(applied["seq"]),
                    applied["fingerprint"],
                    canonical_json(applied["result"]),
                    int(applied["applied_clock_ms"]),
                )
            )
        connection.executemany(
            """
            INSERT INTO snap_applied_ops(
                generation, op_key, txn_id, phase, seq, fingerprint,
                result_json, applied_clock_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            applied_rows,
        )
        gc_run_rows = []
        retained_rows = []
        for run in state["gc_runs"].values():
            gc_run_rows.append(
                (
                    generation,
                    run["run_id"],
                    int(run["clock_ms"]),
                    int(run["collected_count"]),
                    int(run["retained_count"]),
                    canonical_json(run["details"]),
                )
            )
            for retained in run.get("retained", []):
                retained_rows.append(
                    (
                        generation,
                        run["run_id"],
                        int(retained["version_id"]),
                        retained["reason"],
                        canonical_json(retained.get("view_ids", [])),
                    )
                )
        connection.executemany(
            """
            INSERT INTO snap_gc_runs(
                generation, run_id, clock_ms, collected_count,
                retained_count, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            gc_run_rows,
        )
        connection.executemany(
            """
            INSERT INTO snap_gc_retained(
                generation, run_id, version_id, reason, view_ids_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            retained_rows,
        )
        return digest

    def _cleanup_generation_rows(self, generation: int) -> None:
        connection = self._connect()
        try:
            self._delete_generation_rows_conn(connection, generation)
        finally:
            connection.close()

    def _delete_generation_rows_conn(
        self,
        connection: sqlite3.Connection,
        generation: int,
    ) -> None:
        in_transaction = connection.in_transaction
        if not in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            for table in GENERATION_TABLES:
                connection.execute(
                    f"DELETE FROM {table} WHERE generation = ?",
                    (generation,),
                )
            connection.execute(
                "DELETE FROM snapshot_generations WHERE generation = ?",
                (generation,),
            )
            if not in_transaction:
                connection.execute("COMMIT")
        except Exception:
            if not in_transaction and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _json_load(value: str) -> Any:
        import json

        return json.loads(value)

