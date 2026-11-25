from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json_text, sha256_bytes
from .domain import Manifest
from .errors import fail
from .loaders import Catalog, RegistryBundle


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wt_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_operation_steps (
    op_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (op_id, step),
    FOREIGN KEY (op_id) REFERENCES wt_operations(op_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS wt_catalog_versions (
    version TEXT NOT NULL,
    source_format TEXT NOT NULL,
    source_path TEXT NOT NULL,
    revision INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    absolute_updated_ms INTEGER NOT NULL,
    tombstone INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    PRIMARY KEY (version, source_format)
);
CREATE TABLE IF NOT EXISTS wt_registries (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS wt_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    parent_snapshot_id TEXT,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    directory_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_replicas (
    replica_id TEXT PRIMARY KEY,
    baseline_version TEXT NOT NULL,
    target_version TEXT,
    stage TEXT NOT NULL,
    version_binding TEXT NOT NULL,
    shard_chain_digest TEXT NOT NULL,
    config_binding TEXT NOT NULL,
    health_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_caches (
    cache_id TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    shard_chain_json TEXT NOT NULL,
    shard_chain_digest TEXT NOT NULL,
    config_binding TEXT NOT NULL,
    valid INTEGER NOT NULL,
    invalidation_event_id TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_release_runs (
    run_id TEXT PRIMARY KEY,
    operation_id TEXT,
    target_version TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    baseline_snapshot_id TEXT NOT NULL,
    target_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage INTEGER NOT NULL,
    current_ratio INTEGER NOT NULL,
    stages_json TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_gray_stages (
    run_id TEXT NOT NULL,
    stage_index INTEGER NOT NULL,
    ratio INTEGER NOT NULL,
    status TEXT NOT NULL,
    replicas_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    committed_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, stage_index),
    FOREIGN KEY (run_id) REFERENCES wt_release_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS wt_rollback_steps (
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    from_ratio INTEGER NOT NULL,
    to_ratio INTEGER NOT NULL,
    replicas_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    committed_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, step_index),
    FOREIGN KEY (run_id) REFERENCES wt_release_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS wt_invalidation_events (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    roots_json TEXT NOT NULL,
    affected_replicas_json TEXT NOT NULL,
    invalidated_caches_json TEXT NOT NULL,
    reusable_caches_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_health_checks (
    version TEXT NOT NULL,
    directory_digest TEXT NOT NULL,
    healthy INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    PRIMARY KEY (version, directory_digest)
);
CREATE TABLE IF NOT EXISTS wt_requests (
    request_id TEXT PRIMARY KEY,
    run_id TEXT,
    phase TEXT NOT NULL,
    model_version TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    ratio_at_start INTEGER NOT NULL,
    binding_json TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ops_status ON wt_operations(status);
CREATE INDEX IF NOT EXISTS idx_caches_replica ON wt_caches(replica_id);
CREATE INDEX IF NOT EXISTS idx_requests_run ON wt_requests(run_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _json_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Storage:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(str(self.path) + ".lock")
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO wt_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        finally:
            connection.close()

    @contextlib.contextmanager
    def operation_lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.reader() as connection:
            row = connection.execute("SELECT value FROM wt_meta WHERE key=?", (key,)).fetchone()
        return _json_load(row["value"], default) if row else default

    def set_meta(self, key: str, value: Any, *, conn: sqlite3.Connection | None = None) -> None:
        text = canonical_json_text(value)
        if conn is not None:
            conn.execute(
                "INSERT INTO wt_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, text),
            )
            return
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO wt_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, text),
            )

    def begin_operation(
        self, op_id: str, kind: str, request: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        request_json = canonical_json_text(request)
        request_sha256 = sha256_bytes(request_json.encode("utf-8"))
        timestamp = now_ms()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM wt_operations WHERE op_id=?", (op_id,)
            ).fetchone()
            if existing:
                if existing["kind"] != kind or existing["request_sha256"] != request_sha256:
                    fail(
                        "CONFLICT",
                        "operation id is already bound to a different request",
                        op_id=op_id,
                        existing_kind=existing["kind"],
                        requested_kind=kind,
                    )
                return _operation_row(existing), False
            connection.execute(
                "INSERT INTO wt_operations("
                "op_id, kind, status, phase, request_json, request_sha256, created_at, updated_at"
                ") VALUES(?, ?, 'running', 'prepared', ?, ?, ?, ?)",
                (op_id, kind, request_json, request_sha256, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO wt_operation_steps(op_id, step, status, detail_json, updated_at)"
                " VALUES(?, 'prepare', 'committed', ?, ?)",
                (op_id, canonical_json_text({"kind": kind}), timestamp),
            )
        return self.get_operation(op_id), True

    def get_operation(self, op_id: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_operations WHERE op_id=?", (op_id,)
            ).fetchone()
            if row is None:
                fail("NOT_FOUND", "operation does not exist", op_id=op_id)
            steps = connection.execute(
                "SELECT * FROM wt_operation_steps WHERE op_id=? ORDER BY updated_at, step",
                (op_id,),
            ).fetchall()
        result = _operation_row(row)
        result["steps"] = [
            {
                "step": step["step"],
                "status": step["status"],
                "detail": _json_load(step["detail_json"], {}),
                "updated_at": step["updated_at"],
            }
            for step in steps
        ]
        return result

    def operation_status(self, op_id: str) -> str | None:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT status FROM wt_operations WHERE op_id=?", (op_id,)
            ).fetchone()
        return row["status"] if row else None

    def set_step(
        self,
        op_id: str,
        step: str,
        status: str,
        detail: dict[str, Any] | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        timestamp = now_ms()
        sql = (
            "INSERT INTO wt_operation_steps(op_id, step, status, detail_json, updated_at)"
            " VALUES(?, ?, ?, ?, ?) ON CONFLICT(op_id, step) DO UPDATE SET "
            "status=excluded.status, detail_json=excluded.detail_json, updated_at=excluded.updated_at"
        )
        parameters = (op_id, step, status, canonical_json_text(detail or {}), timestamp)
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def complete_operation(
        self,
        op_id: str,
        result: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
        phase: str = "committed",
    ) -> None:
        timestamp = now_ms()
        result_json = canonical_json_text(result)
        sql = (
            "UPDATE wt_operations SET status='committed', phase=?, result_json=?, "
            "error_json=NULL, updated_at=? WHERE op_id=?"
        )
        params = (phase, result_json, timestamp, op_id)
        step_sql = (
            "INSERT INTO wt_operation_steps(op_id, step, status, detail_json, updated_at)"
            " VALUES(?, 'commit', 'committed', ?, ?) "
            "ON CONFLICT(op_id, step) DO UPDATE SET status='committed', detail_json=excluded.detail_json,"
            " updated_at=excluded.updated_at"
        )
        if conn is not None:
            conn.execute(sql, params)
            conn.execute(step_sql, (op_id, canonical_json_text({"phase": phase}), timestamp))
            return
        with self.transaction() as connection:
            connection.execute(sql, params)
            connection.execute(step_sql, (op_id, canonical_json_text({"phase": phase}), timestamp))

    def fail_operation(self, op_id: str, error: dict[str, Any], *, phase: str = "failed") -> None:
        timestamp = now_ms()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE wt_operations SET status='failed', phase=?, error_json=?, updated_at=?"
                " WHERE op_id=?",
                (phase, canonical_json_text(error), timestamp, op_id),
            )
            connection.execute(
                "INSERT INTO wt_operation_steps(op_id, step, status, detail_json, updated_at)"
                " VALUES(?, 'commit', 'failed', ?, ?) "
                "ON CONFLICT(op_id, step) DO UPDATE SET status='failed', detail_json=excluded.detail_json,"
                " updated_at=excluded.updated_at",
                (op_id, canonical_json_text(error), timestamp),
            )

    def incomplete_operations(self) -> list[dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT op_id FROM wt_operations WHERE status IN ('prepared', 'running')"
                " ORDER BY created_at, op_id"
            ).fetchall()
        return [self.get_operation(row["op_id"]) for row in rows]

    def replace_catalog(
        self, catalog: Catalog, *, conn: sqlite3.Connection | None = None
    ) -> None:
        if conn is None:
            with self.transaction() as connection:
                self.replace_catalog(catalog, conn=connection)
            return
        conn.execute("DELETE FROM wt_catalog_versions")
        conn.execute("DELETE FROM wt_registries")
        for manifest in catalog.all_versions:
            conn.execute(
                "INSERT INTO wt_catalog_versions("
                "version, source_format, source_path, revision, logical_clock, generation,"
                " absolute_updated_ms, tombstone, manifest_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.version,
                    manifest.source_format,
                    manifest.source_path,
                    manifest.revision,
                    manifest.logical_clock,
                    manifest.generation,
                    manifest.absolute_updated_ms,
                    1 if manifest.tombstone else 0,
                    canonical_json_text(manifest.storage_dict()),
                ),
            )
        for kind, definitions in (
            ("config", catalog.registries.configs),
            ("feature_table", catalog.registries.feature_tables),
            ("upstream_model", catalog.registries.upstream_models),
        ):
            for item_id, definition in definitions.items():
                conn.execute(
                    "INSERT INTO wt_registries(kind, id, definition_json) VALUES(?, ?, ?)",
                    (kind, item_id, canonical_json_text(definition)),
                )
        self.set_meta(
            "catalog",
            {
                "input_digest": catalog.input_digest,
                "sources": catalog.sources,
                "v1_count": len(catalog.v1_versions),
                "v2_count": len(catalog.v2_versions),
            },
            conn=conn,
        )

    def catalog(self) -> Catalog:
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM wt_catalog_versions ORDER BY source_format, revision, version"
            ).fetchall()
            registry_rows = connection.execute(
                "SELECT * FROM wt_registries ORDER BY kind, id"
            ).fetchall()
        manifests = [Manifest.from_storage_dict(_json_load(row["manifest_json"])) for row in rows]
        v1 = tuple(item for item in manifests if item.source_format == "v1")
        v2 = tuple(item for item in manifests if item.source_format == "v2")
        registries = RegistryBundle({}, {}, {})
        for row in registry_rows:
            definition = _json_load(row["definition_json"], {})
            if row["kind"] == "config":
                registries.configs[row["id"]] = definition
            elif row["kind"] == "feature_table":
                registries.feature_tables[row["id"]] = definition
            elif row["kind"] == "upstream_model":
                registries.upstream_models[row["id"]] = definition
        meta = self.get_meta("catalog", {})
        return Catalog(
            v1_versions=v1,
            v2_versions=v2,
            registries=registries,
            input_digest=meta.get("input_digest", ""),
            sources=meta.get("sources", {}),
        )

    def insert_snapshot(
        self,
        *,
        snapshot_id: str,
        parent_snapshot_id: str | None,
        kind: str,
        version: str,
        logical_clock: int,
        generation: int,
        directory_digest: str,
        payload: dict[str, Any],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        payload_json = canonical_json_text(payload)
        parameters = (
            snapshot_id,
            parent_snapshot_id,
            kind,
            version,
            logical_clock,
            generation,
            directory_digest,
            payload_json,
            sha256_bytes(payload_json.encode("utf-8")),
            now_ms(),
        )
        sql = (
            "INSERT OR IGNORE INTO wt_snapshots("
            "snapshot_id, parent_snapshot_id, kind, version, logical_clock, generation,"
            " directory_digest, payload_json, payload_sha256, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            fail("NOT_FOUND", "snapshot does not exist", snapshot_id=snapshot_id)
        return _snapshot_row(row)

    def snapshot_exists(self, snapshot_id: str) -> bool:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT 1 FROM wt_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return row is not None

    def current_snapshot(self) -> dict[str, Any] | None:
        snapshot_id = self.get_meta("current_snapshot")
        return self.get_snapshot(snapshot_id) if snapshot_id else None

    def baseline_snapshot(self) -> dict[str, Any] | None:
        snapshot_id = self.get_meta("baseline_snapshot")
        return self.get_snapshot(snapshot_id) if snapshot_id else None

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM wt_snapshots ORDER BY created_at, snapshot_id"
            ).fetchall()
        return [_snapshot_row(row) for row in rows]

    def replace_replicas(self, replicas: list[dict[str, Any]], *, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.transaction() as connection:
                self.replace_replicas(replicas, conn=connection)
            return
        conn.execute("DELETE FROM wt_replicas")
        for replica in replicas:
            conn.execute(
                "INSERT INTO wt_replicas("
                "replica_id, baseline_version, target_version, stage, version_binding,"
                " shard_chain_digest, config_binding, health_json, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    replica["replica_id"],
                    replica["baseline_version"],
                    replica.get("target_version"),
                    replica.get("stage", "warmed"),
                    replica["version_binding"],
                    replica["shard_chain_digest"],
                    replica["config_binding"],
                    canonical_json_text(replica.get("health", {})),
                    now_ms(),
                ),
            )

    def list_replicas(self) -> list[dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute("SELECT * FROM wt_replicas ORDER BY replica_id").fetchall()
        return [_replica_row(row) for row in rows]

    def get_replica(self, replica_id: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_replicas WHERE replica_id=?", (replica_id,)
            ).fetchone()
        if row is None:
            fail("NOT_FOUND", "replica does not exist", replica_id=replica_id)
        return _replica_row(row)

    def update_replica(
        self,
        replica_id: str,
        *,
        stage: str,
        version_binding: str,
        shard_chain_digest: str,
        config_binding: str,
        target_version: str | None,
        health: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "UPDATE wt_replicas SET target_version=?, stage=?, version_binding=?,"
            " shard_chain_digest=?, config_binding=?, health_json=?, updated_at=? WHERE replica_id=?"
        )
        parameters = (
            target_version,
            stage,
            version_binding,
            shard_chain_digest,
            config_binding,
            canonical_json_text(health or {}),
            now_ms(),
            replica_id,
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def replace_caches(self, caches: list[dict[str, Any]], *, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.transaction() as connection:
                self.replace_caches(caches, conn=connection)
            return
        conn.execute("DELETE FROM wt_caches")
        for cache in caches:
            conn.execute(
                "INSERT INTO wt_caches("
                "cache_id, replica_id, kind, version, shard_chain_json, shard_chain_digest,"
                " config_binding, valid, invalidation_event_id, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cache["cache_id"],
                    cache["replica_id"],
                    cache["kind"],
                    cache["version"],
                    canonical_json_text(cache["shard_chain"]),
                    cache["shard_chain_digest"],
                    cache["config_binding"],
                    1 if cache.get("valid", True) else 0,
                    cache.get("invalidation_event_id"),
                    now_ms(),
                ),
            )

    def list_caches(self, *, valid: bool | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wt_caches"
        params: tuple[Any, ...] = ()
        if valid is not None:
            sql += " WHERE valid=?"
            params = (1 if valid else 0,)
        sql += " ORDER BY cache_id"
        with self.reader() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_cache_row(row) for row in rows]

    def invalidate_caches(
        self,
        cache_ids: list[str],
        event_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if not cache_ids:
            return
        sql = (
            "UPDATE wt_caches SET valid=0, invalidation_event_id=?, updated_at=?"
            " WHERE cache_id=?"
        )
        timestamp = now_ms()
        if conn is not None:
            for cache_id in cache_ids:
                conn.execute(sql, (event_id, timestamp, cache_id))
            return
        with self.transaction() as connection:
            for cache_id in cache_ids:
                connection.execute(sql, (event_id, timestamp, cache_id))

    def record_invalidation(
        self,
        *,
        event_id: str,
        operation_id: str,
        reason: str,
        roots: list[str],
        affected_replicas: list[str],
        invalidated_caches: list[str],
        reusable_caches: list[str],
        details: list[dict[str, Any]],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        parameters = (
            event_id,
            operation_id,
            reason,
            canonical_json_text(roots),
            canonical_json_text(affected_replicas),
            canonical_json_text(invalidated_caches),
            canonical_json_text(reusable_caches),
            canonical_json_text(details),
            now_ms(),
        )
        sql = (
            "INSERT OR IGNORE INTO wt_invalidation_events("
            "event_id, operation_id, reason, roots_json, affected_replicas_json,"
            " invalidated_caches_json, reusable_caches_json, details_json, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def invalidation_events(self) -> list[dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM wt_invalidation_events ORDER BY created_at, event_id"
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "operation_id": row["operation_id"],
                "reason": row["reason"],
                "roots": _json_load(row["roots_json"], []),
                "affected_replicas": _json_load(row["affected_replicas_json"], []),
                "invalidated_caches": _json_load(row["invalidated_caches_json"], []),
                "reusable_caches": _json_load(row["reusable_caches_json"], []),
                "details": _json_load(row["details_json"], []),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_release(
        self,
        *,
        run_id: str,
        operation_id: str,
        target_version: str,
        baseline_version: str,
        baseline_snapshot_id: str,
        target_snapshot_id: str,
        stages: list[int],
        status: str = "pending_warmup",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        timestamp = now_ms()
        sql = (
            "INSERT OR IGNORE INTO wt_release_runs("
            "run_id, operation_id, target_version, baseline_version, baseline_snapshot_id,"
            " target_snapshot_id, status, current_stage, current_ratio, stages_json,"
            " trigger_json, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, -1, 0, ?, '{}', ?, ?)"
        )
        parameters = (
            run_id,
            operation_id,
            target_version,
            baseline_version,
            baseline_snapshot_id,
            target_snapshot_id,
            status,
            canonical_json_text(stages),
            timestamp,
            timestamp,
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def latest_release(self, *, target_version: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM wt_release_runs"
        params: tuple[Any, ...] = ()
        if target_version is not None:
            sql += " WHERE target_version=?"
            params = (target_version,)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT 1"
        with self.reader() as connection:
            row = connection.execute(sql, params).fetchone()
        return _release_row(row) if row else None

    def get_release(self, run_id: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_release_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                fail("NOT_FOUND", "release run does not exist", run_id=run_id)
            stages = connection.execute(
                "SELECT * FROM wt_gray_stages WHERE run_id=? ORDER BY stage_index", (run_id,)
            ).fetchall()
            rollbacks = connection.execute(
                "SELECT * FROM wt_rollback_steps WHERE run_id=? ORDER BY step_index", (run_id,)
            ).fetchall()
        result = _release_row(row)
        result["gray_stages"] = [
            {
                "stage_index": stage["stage_index"],
                "ratio": stage["ratio"],
                "status": stage["status"],
                "replicas": _json_load(stage["replicas_json"], []),
                "decision": _json_load(stage["decision_json"], {}),
                "committed_at": stage["committed_at"],
            }
            for stage in stages
        ]
        result["rollback_steps"] = [
            {
                "step_index": step["step_index"],
                "from_ratio": step["from_ratio"],
                "to_ratio": step["to_ratio"],
                "replicas": _json_load(step["replicas_json"], []),
                "decision": _json_load(step["decision_json"], {}),
                "committed_at": step["committed_at"],
            }
            for step in rollbacks
        ]
        return result

    def update_release(
        self,
        run_id: str,
        *,
        status: str,
        current_stage: int,
        current_ratio: int,
        trigger: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "UPDATE wt_release_runs SET status=?, current_stage=?, current_ratio=?,"
            " trigger_json=COALESCE(?, trigger_json), updated_at=? WHERE run_id=?"
        )
        parameters = (
            status,
            current_stage,
            current_ratio,
            canonical_json_text(trigger) if trigger is not None else None,
            now_ms(),
            run_id,
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def set_release_stages(
        self,
        run_id: str,
        stages: list[int],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = "UPDATE wt_release_runs SET stages_json=?, updated_at=? WHERE run_id=?"
        parameters = (canonical_json_text(stages), now_ms(), run_id)
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def stage_exists(self, run_id: str, stage_index: int) -> bool:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT 1 FROM wt_gray_stages WHERE run_id=? AND stage_index=?",
                (run_id, stage_index),
            ).fetchone()
        return row is not None

    def record_gray_stage(
        self,
        *,
        run_id: str,
        stage_index: int,
        ratio: int,
        replicas: list[str],
        decision: dict[str, Any],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "INSERT OR IGNORE INTO wt_gray_stages("
            "run_id, stage_index, ratio, status, replicas_json, decision_json, committed_at"
            ") VALUES(?, ?, ?, 'committed', ?, ?, ?)"
        )
        parameters = (
            run_id,
            stage_index,
            ratio,
            canonical_json_text(replicas),
            canonical_json_text(decision),
            now_ms(),
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def rollback_step_exists(self, run_id: str, step_index: int) -> bool:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT 1 FROM wt_rollback_steps WHERE run_id=? AND step_index=?",
                (run_id, step_index),
            ).fetchone()
        return row is not None

    def record_rollback_step(
        self,
        *,
        run_id: str,
        step_index: int,
        from_ratio: int,
        to_ratio: int,
        replicas: list[str],
        decision: dict[str, Any],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "INSERT OR IGNORE INTO wt_rollback_steps("
            "run_id, step_index, from_ratio, to_ratio, replicas_json, decision_json, committed_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)"
        )
        parameters = (
            run_id,
            step_index,
            from_ratio,
            to_ratio,
            canonical_json_text(replicas),
            canonical_json_text(decision),
            now_ms(),
        )
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def record_health(
        self,
        version: str,
        directory_digest: str,
        healthy: bool,
        details: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "INSERT INTO wt_health_checks(version, directory_digest, healthy, details_json, checked_at)"
            " VALUES(?, ?, ?, ?, ?) ON CONFLICT(version, directory_digest) DO UPDATE SET"
            " healthy=excluded.healthy, details_json=excluded.details_json, checked_at=excluded.checked_at"
        )
        parameters = (version, directory_digest, 1 if healthy else 0, canonical_json_text(details), now_ms())
        if conn is not None:
            conn.execute(sql, parameters)
            return
        with self.transaction() as connection:
            connection.execute(sql, parameters)

    def latest_health(self, version: str) -> dict[str, Any] | None:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_health_checks WHERE version=?"
                " ORDER BY checked_at DESC, directory_digest DESC LIMIT 1",
                (version,),
            ).fetchone()
        if row is None:
            return None
        return {
            "version": row["version"],
            "directory_digest": row["directory_digest"],
            "healthy": bool(row["healthy"]),
            "details": _json_load(row["details_json"], {}),
            "checked_at": row["checked_at"],
        }

    def insert_request(
        self,
        *,
        request_id: str,
        run_id: str | None,
        model_version: str,
        snapshot_id: str,
        ratio_at_start: int,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_ms()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO wt_requests("
                "request_id, run_id, phase, model_version, snapshot_id, ratio_at_start,"
                " binding_json, started_at, finished_at"
                ") VALUES(?, ?, 'in_flight', ?, ?, ?, ?, ?, NULL)",
                (
                    request_id,
                    run_id,
                    model_version,
                    snapshot_id,
                    ratio_at_start,
                    canonical_json_text(binding),
                    timestamp,
                ),
            )
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM wt_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            fail("NOT_FOUND", "request binding does not exist", request_id=request_id)
        return {
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "phase": row["phase"],
            "model_version": row["model_version"],
            "snapshot_id": row["snapshot_id"],
            "ratio_at_start": row["ratio_at_start"],
            "binding": _json_load(row["binding_json"], {}),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def finish_request(self, request_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM wt_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                fail("NOT_FOUND", "request binding does not exist", request_id=request_id)
            connection.execute(
                "UPDATE wt_requests SET phase='finished', finished_at=? WHERE request_id=?",
                (now_ms(), request_id),
            )
        return self.get_request(request_id)

    def list_requests(self) -> list[dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT request_id FROM wt_requests ORDER BY started_at, request_id"
            ).fetchall()
        return [self.get_request(row["request_id"]) for row in rows]

    def status(self) -> dict[str, Any]:
        current = self.current_snapshot()
        baseline = self.baseline_snapshot()
        release = self.latest_release()
        if release is not None:
            release = self.get_release(release["run_id"])
        replicas = self.list_replicas()
        caches = self.list_caches()
        operations = []
        with self.reader() as connection:
            operation_rows = connection.execute(
                "SELECT op_id FROM wt_operations ORDER BY created_at, op_id"
            ).fetchall()
        for row in operation_rows:
            item = self.get_operation(row["op_id"])
            operations.append(
                {
                    "op_id": item["op_id"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "phase": item["phase"],
                    "steps": item["steps"],
                    "error": item["error"],
                }
            )
        return {
            "schema_version": self.get_meta("schema_version"),
            "db": str(self.path),
            "clock_watermark": self.get_meta("clock_watermark", 0),
            "update_round_watermark": self.get_meta("update_round", 0),
            "current_snapshot": current,
            "baseline_snapshot": baseline,
            "release": release,
            "replicas": replicas,
            "caches": {
                "valid": [cache for cache in caches if cache["valid"]],
                "invalidated": [cache for cache in caches if not cache["valid"]],
            },
            "invalidation_events": self.invalidation_events(),
            "operations": operations,
            "catalog": self.get_meta("catalog", {}),
            "requests": self.list_requests(),
            "health": {
                snapshot["version"]: self.latest_health(snapshot["version"])
                for snapshot in (current, baseline)
                if snapshot is not None
            },
        }


def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "op_id": row["op_id"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "request": _json_load(row["request_json"], {}),
        "request_sha256": row["request_sha256"],
        "result": _json_load(row["result_json"], None),
        "error": _json_load(row["error_json"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_id": row["snapshot_id"],
        "parent_snapshot_id": row["parent_snapshot_id"],
        "kind": row["kind"],
        "version": row["version"],
        "logical_clock": row["logical_clock"],
        "generation": row["generation"],
        "directory_digest": row["directory_digest"],
        "payload": _json_load(row["payload_json"], {}),
        "payload_sha256": row["payload_sha256"],
        "created_at": row["created_at"],
    }


def _replica_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "replica_id": row["replica_id"],
        "baseline_version": row["baseline_version"],
        "target_version": row["target_version"],
        "stage": row["stage"],
        "version_binding": row["version_binding"],
        "shard_chain_digest": row["shard_chain_digest"],
        "config_binding": row["config_binding"],
        "health": _json_load(row["health_json"], {}),
        "updated_at": row["updated_at"],
    }


def _cache_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cache_id": row["cache_id"],
        "replica_id": row["replica_id"],
        "kind": row["kind"],
        "version": row["version"],
        "shard_chain": _json_load(row["shard_chain_json"], []),
        "shard_chain_digest": row["shard_chain_digest"],
        "config_binding": row["config_binding"],
        "valid": bool(row["valid"]),
        "invalidation_event_id": row["invalidation_event_id"],
        "updated_at": row["updated_at"],
    }


def _release_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "operation_id": row["operation_id"],
        "target_version": row["target_version"],
        "baseline_version": row["baseline_version"],
        "baseline_snapshot_id": row["baseline_snapshot_id"],
        "target_snapshot_id": row["target_snapshot_id"],
        "status": row["status"],
        "current_stage": row["current_stage"],
        "current_ratio": row["current_ratio"],
        "stages": _json_load(row["stages_json"], []),
        "trigger": _json_load(row["trigger_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
