"""SQLite state store with staged, versioned snapshot publication."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .errors import (
    CACHE_CLOCK_ROLLBACK,
    CACHE_GENERATION_REGRESSION,
    CACHE_MANIFEST_VERSION_ROLLBACK,
    CACHE_STATE_CORRUPT,
    CacheError,
)
from .indexer import recompute_data_digest
from .locking import FileLock
from .util import canonical_json, sha256_text


class ConcurrentStateChange(Exception):
    """Internal retry signal; never exposed as a business error."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    index_version INTEGER PRIMARY KEY,
    input_hash TEXT NOT NULL,
    data_digest TEXT NOT NULL,
    manifest_version INTEGER NOT NULL,
    cache_generation_floor INTEGER NOT NULL,
    cache_generation_ceiling INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    previous_index_version INTEGER,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_sources (
    index_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    manifest_version INTEGER NOT NULL,
    generation_min INTEGER,
    generation_max INTEGER,
    source_json TEXT NOT NULL,
    PRIMARY KEY (index_version, source_id)
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
    index_version INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    artifact TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tombstoned INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (index_version, task_key, artifact)
);

CREATE TABLE IF NOT EXISTS snapshot_tombstones (
    index_version INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    artifact TEXT NOT NULL,
    digest TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tombstone_json TEXT NOT NULL,
    PRIMARY KEY (index_version, task_key, artifact, digest, source_id)
);

CREATE TABLE IF NOT EXISTS snapshot_decisions (
    index_version INTEGER NOT NULL,
    decision_type TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (index_version, decision_type, decision_id)
);

CREATE TABLE IF NOT EXISTS pending_operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    base_index_version INTEGER NOT NULL,
    target_index_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    snapshot_json TEXT,
    data_digest TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    error_json TEXT
);

CREATE TABLE IF NOT EXISTS stage_snapshots (
    op_id TEXT PRIMARY KEY,
    index_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    data_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_sources (
    op_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY (op_id, source_id)
);

CREATE TABLE IF NOT EXISTS stage_entries (
    op_id TEXT NOT NULL,
    task_key TEXT NOT NULL,
    artifact TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (op_id, task_key, artifact)
);

CREATE TABLE IF NOT EXISTS stage_tombstones (
    op_id TEXT NOT NULL,
    task_key TEXT NOT NULL,
    artifact TEXT NOT NULL,
    digest TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tombstone_json TEXT NOT NULL,
    PRIMARY KEY (op_id, task_key, artifact, digest, source_id)
);

CREATE TABLE IF NOT EXISTS stage_decisions (
    op_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (op_id, decision_type, decision_id)
);

CREATE TABLE IF NOT EXISTS judgement_cache (
    pipeline_id TEXT NOT NULL,
    pipeline_version INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    source_index_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (pipeline_id, pipeline_version, task_key, input_hash)
);

CREATE TABLE IF NOT EXISTS judgement_reports (
    report_key TEXT PRIMARY KEY,
    index_version INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS judgement_invalidations (
    index_version INTEGER NOT NULL,
    pipeline_id TEXT NOT NULL,
    pipeline_version INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (index_version, pipeline_id, pipeline_version, task_key)
);
"""


def _loads(text: str) -> Any:
    return json.loads(text)


class StateStore:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        lock_timeout_seconds: float = 15.0,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "index.sqlite3"
        self.lock_path = self.state_dir / "cacheman.lock"
        self.lock_timeout_seconds = lock_timeout_seconds
        self._initialize()

    def _initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=15.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with FileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO state_meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        raw = StateStore._get_meta(conn, "current_index_version")
        return 0 if raw is None else int(raw)

    @staticmethod
    def _watermark(conn: sqlite3.Connection) -> int:
        raw = StateStore._get_meta(conn, "logical_clock_watermark_ms")
        return 0 if raw is None else int(raw)

    def current_version(self) -> int:
        with self.read_connection() as conn:
            return self._current_version(conn)

    def logical_clock_watermark(self) -> int:
        with self.read_connection() as conn:
            return self._watermark(conn)

    def check_clock(
        self,
        now_ms: int,
        tolerance_ms: int,
    ) -> int:
        with self.read_connection() as conn:
            return self._check_clock(conn, now_ms, tolerance_ms)

    @staticmethod
    def _check_clock(
        conn: sqlite3.Connection,
        now_ms: int,
        tolerance_ms: int,
    ) -> int:
        watermark = StateStore._watermark(conn)
        if now_ms < watermark - tolerance_ms:
            raise CacheError(
                CACHE_CLOCK_ROLLBACK,
                "logical clock rollback exceeds the configured tolerance",
                fields={
                    "now_ms": now_ms,
                    "logical_clock_watermark_ms": watermark,
                    "tolerance_ms": tolerance_ms,
                    "rollback_ms": watermark - now_ms,
                },
            )
        return max(watermark, now_ms)

    @staticmethod
    def _raise_if_manifest_regression(
        conn: sqlite3.Connection,
        *,
        new_manifest_version: int,
        new_generation_floor: int,
    ) -> None:
        current_version = StateStore._current_version(conn)
        if current_version == 0:
            return
        snapshot = StateStore._load_snapshot_row(conn, current_version)
        if new_manifest_version < int(snapshot["manifest_version"]):
            raise CacheError(
                CACHE_MANIFEST_VERSION_ROLLBACK,
                "incoming manifests are older than the committed index",
                fields={
                    "incoming_manifest_version": new_manifest_version,
                    "committed_manifest_version": int(snapshot["manifest_version"]),
                    "committed_index_version": current_version,
                },
            )
        if new_generation_floor < int(snapshot["cache_generation_floor"]):
            raise CacheError(
                CACHE_GENERATION_REGRESSION,
                "incoming cache generation floor is older than the committed index",
                fields={
                    "incoming_generation_floor": new_generation_floor,
                    "committed_generation_floor": int(snapshot["cache_generation_floor"]),
                    "committed_index_version": current_version,
                },
            )

    @staticmethod
    def _load_snapshot_row(
        conn: sqlite3.Connection,
        index_version: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE index_version = ?",
            (index_version,),
        ).fetchone()
        if row is None:
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "current snapshot pointer references a missing snapshot",
                fields={"index_version": index_version},
            )
        try:
            snapshot = _loads(str(row["snapshot_json"]))
        except json.JSONDecodeError as exc:
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "snapshot JSON cannot be decoded",
                fields={"index_version": index_version, "reason": str(exc)},
            )
        expected_digest = recompute_data_digest(snapshot)
        if expected_digest != snapshot.get("data_digest"):
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "snapshot data digest is invalid",
                fields={
                    "index_version": index_version,
                    "stored_digest": snapshot.get("data_digest"),
                    "recomputed_digest": expected_digest,
                },
            )
        return snapshot

    def current_snapshot(self) -> dict[str, Any] | None:
        with self.read_connection() as conn:
            version = self._current_version(conn)
            if version == 0:
                return None
            return self._load_snapshot_row(conn, version)

    def get_snapshot(self, index_version: int) -> dict[str, Any]:
        with self.read_connection() as conn:
            return self._load_snapshot_row(conn, index_version)

    @staticmethod
    def _insert_snapshot_children(
        conn: sqlite3.Connection,
        *,
        table_prefix: str,
        key_column: str,
        key_value: Any,
        snapshot: dict[str, Any],
    ) -> None:
        for source in snapshot["sources"]:
            if table_prefix == "snapshot":
                conn.execute(
                    """
                    INSERT INTO snapshot_sources(
                        index_version, source_id, source_version, manifest_version,
                        generation_min, generation_max, source_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_value,
                        source["source_id"],
                        source["source_version"],
                        source["manifest_version"],
                        source["generation_min"],
                        source["generation_max"],
                        canonical_json(source),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stage_sources(op_id, source_id, source_json)
                    VALUES(?, ?, ?)
                    """,
                    (key_value, source["source_id"], canonical_json(source)),
                )
        for entry in snapshot["entries"]:
            if table_prefix == "snapshot":
                conn.execute(
                    """
                    INSERT INTO snapshot_entries(
                        index_version, task_key, artifact, source_id, tombstoned, entry_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_value,
                        entry["task_key"],
                        entry["artifact"],
                        entry["source_id"],
                        1 if entry["tombstoned"] else 0,
                        canonical_json(entry),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stage_entries(op_id, task_key, artifact, entry_json)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        key_value,
                        entry["task_key"],
                        entry["artifact"],
                        canonical_json(entry),
                    ),
                )
        for tombstone in snapshot["tombstones"]:
            if table_prefix == "snapshot":
                conn.execute(
                    """
                    INSERT INTO snapshot_tombstones(
                        index_version, task_key, artifact, digest, source_id, tombstone_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_value,
                        tombstone["task_key"],
                        tombstone["artifact"],
                        tombstone["digest"],
                        tombstone["source_id"],
                        canonical_json(tombstone),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stage_tombstones(
                        op_id, task_key, artifact, digest, source_id, tombstone_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_value,
                        tombstone["task_key"],
                        tombstone["artifact"],
                        tombstone["digest"],
                        tombstone["source_id"],
                        canonical_json(tombstone),
                    ),
                )
        decision_groups = (
            ("merge", snapshot["merge_decisions"]),
            ("migration", snapshot["migration_decisions"]),
        )
        for decision_type, decisions in decision_groups:
            for decision in decisions:
                if table_prefix == "snapshot":
                    conn.execute(
                        """
                        INSERT INTO snapshot_decisions(
                            index_version, decision_type, decision_id, decision_json
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (
                            key_value,
                            decision_type,
                            decision["decision_id"],
                            canonical_json(decision),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO stage_decisions(
                            op_id, decision_type, decision_id, decision_json
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (
                            key_value,
                            decision_type,
                            decision["decision_id"],
                            canonical_json(decision),
                        ),
                    )

    def prepare_operation(
        self,
        *,
        op_id: str,
        kind: str,
        base_index_version: int,
        target_index_version: int,
        input_hash: str,
        payload: dict[str, Any],
        now_ms: int,
        clock_tolerance_ms: int,
        manifest_version: int,
        generation_floor: int,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            watermark = self._check_clock(conn, now_ms, clock_tolerance_ms)
            current = self._current_version(conn)
            if current != base_index_version:
                return {
                    "status": "concurrent_change",
                    "base_index_version": base_index_version,
                    "current_index_version": current,
                }
            if current:
                current_snapshot = self._load_snapshot_row(conn, current)
                if current_snapshot["input_hash"] == input_hash:
                    self._set_meta(conn, "logical_clock_watermark_ms", str(watermark))
                    return {
                        "status": "noop",
                        "index_version": current,
                        "data_digest": current_snapshot["data_digest"],
                    }
            self._raise_if_manifest_regression(
                conn,
                new_manifest_version=manifest_version,
                new_generation_floor=generation_floor,
            )
            if target_index_version <= current:
                return {
                    "status": "concurrent_change",
                    "base_index_version": base_index_version,
                    "current_index_version": current,
                }
            existing = conn.execute(
                "SELECT status FROM pending_operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO pending_operations(
                        op_id, kind, status, base_index_version, target_index_version,
                        input_hash, payload_json, created_at_ms, updated_at_ms
                    ) VALUES(?, ?, 'prepared', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        op_id,
                        kind,
                        base_index_version,
                        target_index_version,
                        input_hash,
                        canonical_json(payload),
                        now_ms,
                        now_ms,
                    ),
                )
            elif existing["status"] == "completed":
                return {
                    "status": "noop",
                    "index_version": current,
                }
            else:
                conn.execute(
                    """
                    UPDATE pending_operations
                    SET payload_json = ?, updated_at_ms = ?
                    WHERE op_id = ? AND status = 'prepared'
                    """,
                    (canonical_json(payload), now_ms, op_id),
                )
            self._set_meta(conn, "logical_clock_watermark_ms", str(watermark))
            return {
                "status": "prepared",
                "op_id": op_id,
                "base_index_version": base_index_version,
                "target_index_version": target_index_version,
            }

    def stage_snapshot(self, op_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as conn:
            operation = conn.execute(
                "SELECT * FROM pending_operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if operation is None:
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "cannot stage a snapshot for an unknown operation",
                    fields={"op_id": op_id},
                )
            if operation["status"] == "completed":
                return {"status": "completed", "op_id": op_id}
            if operation["status"] == "superseded":
                return {"status": "superseded", "op_id": op_id}
            if int(operation["target_index_version"]) != int(snapshot["index_version"]):
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "snapshot index version differs from the pending operation",
                    fields={
                        "op_id": op_id,
                        "snapshot_index_version": snapshot["index_version"],
                        "operation_target_index_version": int(
                            operation["target_index_version"]
                        ),
                    },
                )
            if recompute_data_digest(snapshot) != snapshot.get("data_digest"):
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "snapshot data digest is invalid before staging",
                    fields={"op_id": op_id},
                )
            for table in (
                "stage_sources",
                "stage_entries",
                "stage_tombstones",
                "stage_decisions",
            ):
                conn.execute(f"DELETE FROM {table} WHERE op_id = ?", (op_id,))
            conn.execute("DELETE FROM stage_snapshots WHERE op_id = ?", (op_id,))
            conn.execute(
                """
                INSERT INTO stage_snapshots(
                    op_id, index_version, input_hash, data_digest, snapshot_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    op_id,
                    int(snapshot["index_version"]),
                    snapshot["input_hash"],
                    snapshot["data_digest"],
                    canonical_json(snapshot),
                ),
            )
            self._insert_snapshot_children(
                conn,
                table_prefix="stage",
                key_column="op_id",
                key_value=op_id,
                snapshot=snapshot,
            )
            self._verify_staged_snapshot(conn, op_id, snapshot)
            conn.execute(
                """
                UPDATE pending_operations
                SET status = 'staged', snapshot_json = ?, data_digest = ?,
                    updated_at_ms = ?
                WHERE op_id = ?
                """,
                (
                    canonical_json(snapshot),
                    snapshot["data_digest"],
                    int(snapshot["created_at_ms"]),
                    op_id,
                ),
            )
            return {
                "status": "staged",
                "op_id": op_id,
                "index_version": int(snapshot["index_version"]),
                "data_digest": snapshot["data_digest"],
            }

    @staticmethod
    def _stage_snapshot_json(
        conn: sqlite3.Connection,
        op_id: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM stage_snapshots WHERE op_id = ?",
            (op_id,),
        ).fetchone()
        if row is None:
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "staged snapshot is missing",
                fields={"op_id": op_id},
            )
        try:
            snapshot = _loads(str(row["snapshot_json"]))
        except json.JSONDecodeError as exc:
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "staged snapshot JSON cannot be decoded",
                fields={"op_id": op_id, "reason": str(exc)},
            )
        if row["data_digest"] != snapshot.get("data_digest"):
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "staged snapshot digest column differs from its JSON",
                fields={"op_id": op_id},
            )
        if recompute_data_digest(snapshot) != snapshot.get("data_digest"):
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "staged snapshot data digest is invalid",
                fields={"op_id": op_id},
            )
        return snapshot

    @staticmethod
    def _verify_staged_snapshot(
        conn: sqlite3.Connection,
        op_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        checks = (
            ("stage_sources", "sources"),
            ("stage_entries", "entries"),
            ("stage_tombstones", "tombstones"),
        )
        for table, key in checks:
            actual = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE op_id = ?",
                (op_id,),
            ).fetchone()["count"]
            if actual != len(snapshot[key]):
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "staged table row count is incomplete",
                    fields={
                        "op_id": op_id,
                        "table": table,
                        "actual": actual,
                        "expected": len(snapshot[key]),
                    },
                )
        expected_decisions = len(snapshot["merge_decisions"]) + len(
            snapshot["migration_decisions"]
        )
        actual_decisions = conn.execute(
            "SELECT COUNT(*) AS count FROM stage_decisions WHERE op_id = ?",
            (op_id,),
        ).fetchone()["count"]
        if actual_decisions != expected_decisions:
            raise CacheError(
                CACHE_STATE_CORRUPT,
                "staged decision row count is incomplete",
                fields={
                    "op_id": op_id,
                    "actual": actual_decisions,
                    "expected": expected_decisions,
                },
            )

    def publish_staged(self, op_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            operation = conn.execute(
                "SELECT * FROM pending_operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if operation is None:
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "unknown pending operation",
                    fields={"op_id": op_id},
                )
            if operation["status"] == "completed":
                return {
                    "status": "completed",
                    "op_id": op_id,
                    "index_version": int(operation["target_index_version"]),
                }
            if operation["status"] == "superseded":
                return {"status": "superseded", "op_id": op_id}
            if operation["status"] != "staged":
                raise CacheError(
                    CACHE_STATE_CORRUPT,
                    "only a fully staged operation can be published",
                    fields={"op_id": op_id, "status": operation["status"]},
                )
            snapshot = self._stage_snapshot_json(conn, op_id)
            self._verify_staged_snapshot(conn, op_id, snapshot)
            current = self._current_version(conn)
            if current:
                current_snapshot = self._load_snapshot_row(conn, current)
                if current_snapshot["input_hash"] == snapshot["input_hash"]:
                    conn.execute(
                        """
                        UPDATE pending_operations
                        SET status = 'completed', updated_at_ms = ?
                        WHERE op_id = ?
                        """,
                        (int(snapshot["created_at_ms"]), op_id),
                    )
                    self._cleanup_stage(conn, op_id)
                    return {
                        "status": "idempotent",
                        "op_id": op_id,
                        "index_version": current,
                    }
            if current != int(operation["base_index_version"]):
                conn.execute(
                    """
                    UPDATE pending_operations
                    SET status = 'superseded', updated_at_ms = ?
                    WHERE op_id = ?
                    """,
                    (int(snapshot["created_at_ms"]), op_id),
                )
                self._cleanup_stage(conn, op_id)
                return {
                    "status": "superseded",
                    "op_id": op_id,
                    "base_index_version": int(operation["base_index_version"]),
                    "current_index_version": current,
                }
            target = int(operation["target_index_version"])
            if target <= current:
                conn.execute(
                    """
                    UPDATE pending_operations
                    SET status = 'superseded', updated_at_ms = ?
                    WHERE op_id = ?
                    """,
                    (int(snapshot["created_at_ms"]), op_id),
                )
                self._cleanup_stage(conn, op_id)
                return {"status": "superseded", "op_id": op_id}
            self._raise_if_manifest_regression(
                conn,
                new_manifest_version=int(snapshot["manifest_version"]),
                new_generation_floor=int(snapshot["cache_generation_floor"]),
            )
            conn.execute(
                """
                INSERT INTO snapshots(
                    index_version, input_hash, data_digest, manifest_version,
                    cache_generation_floor, cache_generation_ceiling,
                    logical_clock_ms, created_at_ms, previous_index_version,
                    snapshot_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target,
                    snapshot["input_hash"],
                    snapshot["data_digest"],
                    int(snapshot["manifest_version"]),
                    int(snapshot["cache_generation_floor"]),
                    int(snapshot["cache_generation_ceiling"]),
                    int(snapshot["logical_clock_ms"]),
                    int(snapshot["created_at_ms"]),
                    snapshot["previous_index_version"],
                    canonical_json(snapshot),
                ),
            )
            self._insert_snapshot_children(
                conn,
                table_prefix="snapshot",
                key_column="index_version",
                key_value=target,
                snapshot=snapshot,
            )
            self._set_meta(conn, "current_index_version", str(target))
            watermark = max(self._watermark(conn), int(snapshot["logical_clock_ms"]))
            self._set_meta(conn, "logical_clock_watermark_ms", str(watermark))
            conn.execute(
                """
                UPDATE pending_operations
                SET status = 'completed', updated_at_ms = ?
                WHERE op_id = ?
                """,
                (int(snapshot["created_at_ms"]), op_id),
            )
            self._cleanup_stage(conn, op_id)
            return {
                "status": "published",
                "op_id": op_id,
                "index_version": target,
                "data_digest": snapshot["data_digest"],
            }

    @staticmethod
    def _cleanup_stage(conn: sqlite3.Connection, op_id: str) -> None:
        for table in (
            "stage_sources",
            "stage_entries",
            "stage_tombstones",
            "stage_decisions",
            "stage_snapshots",
        ):
            conn.execute(f"DELETE FROM {table} WHERE op_id = ?", (op_id,))

    def list_pending_operations(self, include_completed: bool = False) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            predicate = (
                ""
                if include_completed
                else "WHERE status IN ('prepared', 'staged')"
            )
            rows = conn.execute(
                f"""
                SELECT op_id, kind, status, base_index_version, target_index_version,
                       input_hash, payload_json, data_digest, created_at_ms,
                       updated_at_ms, error_json
                FROM pending_operations
                {predicate}
                ORDER BY base_index_version, target_index_version, op_id
                """
            ).fetchall()
            result = []
            for row in rows:
                result.append(
                    {
                        "op_id": row["op_id"],
                        "kind": row["kind"],
                        "status": row["status"],
                        "base_index_version": int(row["base_index_version"]),
                        "target_index_version": int(row["target_index_version"]),
                        "input_hash": row["input_hash"],
                        "payload": _loads(str(row["payload_json"])),
                        "data_digest": row["data_digest"],
                        "created_at_ms": int(row["created_at_ms"]),
                        "updated_at_ms": int(row["updated_at_ms"]),
                        "error": None if row["error_json"] is None else _loads(row["error_json"]),
                    }
                )
            return result

    def mark_operation_superseded(self, op_id: str, now_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE pending_operations
                SET status = 'superseded', updated_at_ms = ?
                WHERE op_id = ? AND status IN ('prepared', 'staged')
                """,
                (now_ms, op_id),
            )
            self._cleanup_stage(conn, op_id)

    def mark_operation_completed(self, op_id: str, now_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE pending_operations
                SET status = 'completed', updated_at_ms = ?
                WHERE op_id = ? AND status NOT IN ('completed')
                """,
                (now_ms, op_id),
            )
            self._cleanup_stage(conn, op_id)

    def mark_operation_failed(self, op_id: str, error: dict[str, Any], now_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE pending_operations
                SET status = 'failed', error_json = ?, updated_at_ms = ?
                WHERE op_id = ? AND status NOT IN ('completed', 'superseded')
                """,
                (canonical_json(error), now_ms, op_id),
            )
            self._cleanup_stage(conn, op_id)

    def lookup_judgement(
        self,
        pipeline_id: str,
        pipeline_version: int,
        task_key: str,
        input_hash: str,
    ) -> dict[str, Any] | None:
        with self.read_connection() as conn:
            row = conn.execute(
                """
                SELECT result_json, source_index_version
                FROM judgement_cache
                WHERE pipeline_id = ? AND pipeline_version = ?
                  AND task_key = ? AND input_hash = ?
                """,
                (pipeline_id, pipeline_version, task_key, input_hash),
            ).fetchone()
            if row is None:
                return None
            result = _loads(str(row["result_json"]))
            result["_cached_from_index_version"] = int(row["source_index_version"])
            return result

    def get_judgement_report(self, report_key: str) -> str | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT report_json FROM judgement_reports WHERE report_key = ?",
                (report_key,),
            ).fetchone()
            return None if row is None else str(row["report_json"])

    def commit_judgements(
        self,
        *,
        expected_index_version: int,
        logical_clock_ms: int,
        clock_tolerance_ms: int,
        report_key: str,
        report: dict[str, Any],
        cache_records: Sequence[dict[str, Any]],
        invalidations: Sequence[dict[str, Any]],
    ) -> str:
        with self.transaction() as conn:
            current = self._current_version(conn)
            if current != expected_index_version:
                raise ConcurrentStateChange(
                    f"index changed from {expected_index_version} to {current}"
                )
            watermark = self._check_clock(conn, logical_clock_ms, clock_tolerance_ms)
            existing = conn.execute(
                "SELECT report_json FROM judgement_reports WHERE report_key = ?",
                (report_key,),
            ).fetchone()
            if existing is not None:
                self._set_meta(conn, "logical_clock_watermark_ms", str(watermark))
                return str(existing["report_json"])
            for record in cache_records:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO judgement_cache(
                        pipeline_id, pipeline_version, task_key, input_hash,
                        source_index_version, result_json, created_at_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["pipeline_id"],
                        record["pipeline_version"],
                        record["task_key"],
                        record["input_hash"],
                        expected_index_version,
                        canonical_json(record["result"]),
                        logical_clock_ms,
                    ),
                )
            report_json = canonical_json(report)
            conn.execute(
                """
                INSERT INTO judgement_reports(
                    report_key, index_version, logical_clock_ms,
                    report_json, created_at_ms
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    report_key,
                    expected_index_version,
                    logical_clock_ms,
                    report_json,
                    logical_clock_ms,
                ),
            )
            for invalidation in invalidations:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO judgement_invalidations(
                        index_version, pipeline_id, pipeline_version, task_key, reason
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        expected_index_version,
                        invalidation["pipeline_id"],
                        invalidation["pipeline_version"],
                        invalidation["task_key"],
                        invalidation["reason"],
                    ),
                )
            self._set_meta(conn, "logical_clock_watermark_ms", str(watermark))
            return report_json

    def status(self) -> dict[str, Any]:
        with self.read_connection() as conn:
            version = self._current_version(conn)
            watermark = self._watermark(conn)
            pending = conn.execute(
                "SELECT COUNT(*) AS count FROM pending_operations WHERE status NOT IN ('completed', 'superseded', 'failed')"
            ).fetchone()["count"]
            if version == 0:
                return {
                    "has_snapshot": False,
                    "index_version": 0,
                    "logical_clock_watermark_ms": watermark,
                    "sources": [],
                    "invalid_artifact_count": 0,
                    "artifact_count": 0,
                    "pending_operation_count": pending,
                }
            snapshot = self._load_snapshot_row(conn, version)
            return {
                "has_snapshot": True,
                "index_version": version,
                "data_digest": snapshot["data_digest"],
                "created_at_ms": snapshot["created_at_ms"],
                "logical_clock_ms": snapshot["logical_clock_ms"],
                "logical_clock_watermark_ms": watermark,
                "manifest_version": snapshot["manifest_version"],
                "cache_generation_floor": snapshot["cache_generation_floor"],
                "cache_generation_ceiling": snapshot["cache_generation_ceiling"],
                "sources": [
                    {
                        "source_id": source["source_id"],
                        "source_version": source["source_version"],
                        "manifest_id": source["manifest_id"],
                        "manifest_version": source["manifest_version"],
                        "generation_min": source["generation_min"],
                        "generation_max": source["generation_max"],
                    }
                    for source in snapshot["sources"]
                ],
                "artifact_count": len(snapshot["entries"]),
                "invalid_artifact_count": sum(
                    1 for entry in snapshot["entries"] if entry["tombstoned"]
                ),
                "pending_operation_count": pending,
            }

    def _verify_children(
        self,
        conn: sqlite3.Connection,
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        version = int(snapshot["index_version"])
        issues: list[dict[str, Any]] = []
        checks = (
            ("snapshot_sources", "sources"),
            ("snapshot_entries", "entries"),
            ("snapshot_tombstones", "tombstones"),
        )
        for table, key in checks:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE index_version = ?",
                (version,),
            ).fetchall()
            if len(rows) != len(snapshot[key]):
                issues.append(
                    {
                        "type": "row_count_mismatch",
                        "table": table,
                        "actual": len(rows),
                        "expected": len(snapshot[key]),
                    }
                )
        source_rows = conn.execute(
            "SELECT source_json FROM snapshot_sources WHERE index_version = ?",
            (version,),
        ).fetchall()
        if sorted(str(row["source_json"]) for row in source_rows) != sorted(
            canonical_json(value) for value in snapshot["sources"]
        ):
            issues.append({"type": "source_rows_differ"})
        entry_rows = conn.execute(
            "SELECT entry_json FROM snapshot_entries WHERE index_version = ?",
            (version,),
        ).fetchall()
        if sorted(str(row["entry_json"]) for row in entry_rows) != sorted(
            canonical_json(value) for value in snapshot["entries"]
        ):
            issues.append({"type": "entry_rows_differ"})
        tombstone_rows = conn.execute(
            "SELECT tombstone_json FROM snapshot_tombstones WHERE index_version = ?",
            (version,),
        ).fetchall()
        if sorted(str(row["tombstone_json"]) for row in tombstone_rows) != sorted(
            canonical_json(value) for value in snapshot["tombstones"]
        ):
            issues.append({"type": "tombstone_rows_differ"})
        expected_decisions = [
            canonical_json(value) for value in snapshot["merge_decisions"]
        ] + [canonical_json(value) for value in snapshot["migration_decisions"]]
        decision_rows = conn.execute(
            "SELECT decision_json FROM snapshot_decisions WHERE index_version = ?",
            (version,),
        ).fetchall()
        if sorted(str(row["decision_json"]) for row in decision_rows) != sorted(
            expected_decisions
        ):
            issues.append({"type": "decision_rows_differ"})
        return issues

    def verify_state(self) -> dict[str, Any]:
        with self.read_connection() as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
            integrity = [str(row[0]) for row in integrity_rows]
            version = self._current_version(conn)
            issues: list[dict[str, Any]] = []
            if integrity != ["ok"]:
                issues.append({"type": "sqlite_integrity_check", "details": integrity})
            snapshot = None
            if version == 0:
                snapshot_count = conn.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()["count"]
                if snapshot_count:
                    issues.append(
                        {
                            "type": "snapshots_without_pointer",
                            "snapshot_count": snapshot_count,
                        }
                    )
            else:
                snapshot = self._load_snapshot_row(conn, version)
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE index_version = ?",
                    (version,),
                ).fetchone()
                if row is None:
                    issues.append({"type": "missing_pointer_snapshot"})
                elif row["data_digest"] != snapshot["data_digest"]:
                    issues.append({"type": "snapshot_digest_mismatch"})
                issues.extend(self._verify_children(conn, snapshot))
            orphan_stages = 0
            for table in (
                "stage_snapshots",
                "stage_sources",
                "stage_entries",
                "stage_tombstones",
                "stage_decisions",
            ):
                orphan_stages += conn.execute(
                    f"""
                    SELECT COUNT(*) AS count FROM {table}
                    WHERE op_id NOT IN (
                        SELECT op_id FROM pending_operations
                        WHERE status IN ('prepared', 'staged')
                    )
                    """
                ).fetchone()["count"]
            pending = [
                {
                    "op_id": row["op_id"],
                    "kind": row["kind"],
                    "status": row["status"],
                }
                for row in conn.execute(
                    """
                    SELECT op_id, kind, status FROM pending_operations
                    WHERE status NOT IN ('completed', 'superseded')
                    ORDER BY op_id
                    """
                ).fetchall()
            ]
            return {
                "ok": not issues,
                "sqlite_integrity_check": integrity,
                "index_version": version,
                "snapshot_present": snapshot is not None,
                "data_digest": None if snapshot is None else snapshot["data_digest"],
                "issues": issues,
                "orphan_stage_rows": orphan_stages,
                "pending_operations": pending,
            }

    def orphan_stage_count(self) -> int:
        with self.read_connection() as conn:
            count = 0
            for table in (
                "stage_snapshots",
                "stage_sources",
                "stage_entries",
                "stage_tombstones",
                "stage_decisions",
            ):
                count += conn.execute(
                    f"""
                    SELECT COUNT(*) AS count FROM {table}
                    WHERE op_id NOT IN (
                        SELECT op_id FROM pending_operations
                        WHERE status IN ('prepared', 'staged')
                    )
                    """
                ).fetchone()["count"]
            return count

    def cleanup_orphan_stages(self) -> int:
        removed = 0
        with self.transaction() as conn:
            orphan_ids = {
                str(row["op_id"])
                for table in (
                    "stage_snapshots",
                    "stage_sources",
                    "stage_entries",
                    "stage_tombstones",
                    "stage_decisions",
                )
                for row in conn.execute(
                    f"""
                    SELECT DISTINCT op_id FROM {table}
                    WHERE op_id NOT IN (
                        SELECT op_id FROM pending_operations
                        WHERE status IN ('prepared', 'staged')
                    )
                    """
                ).fetchall()
            }
            for op_id in sorted(orphan_ids):
                for table in (
                    "stage_snapshots",
                    "stage_sources",
                    "stage_entries",
                    "stage_tombstones",
                    "stage_decisions",
                ):
                    removed += conn.execute(
                        f"DELETE FROM {table} WHERE op_id = ?",
                        (op_id,),
                    ).rowcount
        return removed

    @staticmethod
    def make_operation_id(
        kind: str,
        input_hash: str,
        base_index_version: int,
    ) -> str:
        return "op:" + sha256_text(
            canonical_json(
                {
                    "kind": kind,
                    "input_hash": input_hash,
                    "base_index_version": base_index_version,
                }
            )
        )[:32]
