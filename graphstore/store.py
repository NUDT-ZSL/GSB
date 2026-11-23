from __future__ import annotations

import contextlib
import fcntl
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import ErrorCode, fail
from .index import rehydrate_index
from .models import (
    PackageManifest,
    TraversalRequest,
    UnifiedIndex,
)
from .util import canonical_json, digest_json


SCHEMA_VERSION = "1"
INITIAL_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_versions (
    index_version TEXT PRIMARY KEY,
    snapshot_identity TEXT NOT NULL UNIQUE,
    index_identity TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL,
    logical_clock_floor INTEGER NOT NULL,
    node_count INTEGER NOT NULL,
    visible_node_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    index_digest TEXT NOT NULL,
    merge_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('staging', 'committed', 'superseded', 'orphaned')),
    created_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS index_nodes (
    index_version TEXT NOT NULL REFERENCES index_versions(index_version) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    label TEXT NOT NULL,
    node_json TEXT NOT NULL,
    PRIMARY KEY (index_version, node_id)
);
CREATE TABLE IF NOT EXISTS index_edges (
    index_version TEXT NOT NULL REFERENCES index_versions(index_version) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    label TEXT NOT NULL,
    weight REAL NOT NULL,
    edge_json TEXT NOT NULL,
    PRIMARY KEY (index_version, source_id, target_id, label)
);
CREATE TABLE IF NOT EXISTS merge_decisions (
    index_version TEXT NOT NULL REFERENCES index_versions(index_version) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    decision_key TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (index_version, kind, decision_key)
);
CREATE TABLE IF NOT EXISTS migration_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_version TEXT NOT NULL REFERENCES index_versions(index_version) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traversal_runs (
    run_id TEXT PRIMARY KEY,
    index_version TEXT NOT NULL REFERENCES index_versions(index_version),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned', 'running', 'completed', 'completed_with_failures')),
    total_batches INTEGER NOT NULL,
    committed_batches INTEGER NOT NULL DEFAULT 0,
    truth_digest TEXT,
    result_digest TEXT,
    result_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (index_version, request_digest)
);
CREATE TABLE IF NOT EXISTS traversal_batches (
    run_id TEXT NOT NULL REFERENCES traversal_runs(run_id) ON DELETE CASCADE,
    batch_key TEXT NOT NULL,
    batch_index INTEGER NOT NULL,
    starts_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    estimate_entries INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('planned', 'running', 'succeeded', 'failed', 'abandoned')
    ),
    reason TEXT,
    result_json TEXT,
    result_digest TEXT,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, batch_key),
    UNIQUE (run_id, batch_index)
);
CREATE TABLE IF NOT EXISTS batch_commit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    result_digest TEXT,
    state TEXT NOT NULL,
    committed_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, batch_key)
);
CREATE INDEX IF NOT EXISTS idx_index_nodes_version ON index_nodes(index_version, node_id);
CREATE INDEX IF NOT EXISTS idx_index_edges_version ON index_edges(index_version, source_id);
CREATE INDEX IF NOT EXISTS idx_batches_run_state ON traversal_batches(run_id, state, batch_index);
"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class FileLock:
    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise fail(
                        ErrorCode.LOCK_TIMEOUT,
                        "timed out acquiring state file lock",
                        path=str(self.path),
                        timeout_seconds=self.timeout,
                    )
                time.sleep(0.01)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class StateStore:
    def __init__(self, path: str | Path, *, lock_timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = lock_timeout

    def lock(self) -> FileLock:
        return FileLock(self.lock_path, self.lock_timeout)

    def connect(self, *, initialize: bool = False) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.lock_timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.lock_timeout * 1000)}")
        if initialize:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(INITIAL_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        return connection

    @contextlib.contextmanager
    def transaction(
        self, connection: sqlite3.Connection, *, immediate: bool = True
    ) -> Iterator[sqlite3.Connection]:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def validate_environment(self) -> dict[str, Any]:
        with self.lock():
            connection = self.connect(initialize=True)
            try:
                with self.transaction(connection):
                    connection.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS _environment_probe(value TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO _environment_probe(value) VALUES ('ok')"
                    )
                    value = connection.execute(
                        "SELECT value FROM _environment_probe"
                    ).fetchone()["value"]
                    connection.execute("DROP TABLE _environment_probe")
                return {
                    "state_db": str(self.path),
                    "lock_file": str(self.lock_path),
                    "sqlite_version": sqlite3.sqlite_version,
                    "journal_mode": connection.execute(
                        "PRAGMA journal_mode"
                    ).fetchone()[0],
                    "temp_table": value,
                }
            finally:
                connection.close()

    def _cleanup_orphan_indexes(self, connection: sqlite3.Connection) -> None:
        pointer = connection.execute(
            "SELECT value FROM state WHERE key = 'current_index_version'"
        ).fetchone()
        current = pointer["value"] if pointer else None
        rows = connection.execute(
            "SELECT index_version FROM index_versions WHERE state IN ('staging', 'orphaned')"
        ).fetchall()
        for row in rows:
            if row["index_version"] == current:
                continue
            connection.execute(
                "DELETE FROM index_versions WHERE index_version = ?",
                (row["index_version"],),
            )

    def current_meta(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT iv.*
            FROM state s
            JOIN index_versions iv ON iv.index_version = s.value
            WHERE s.key = 'current_index_version'
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "index_version": row["index_version"],
            "snapshot_identity": row["snapshot_identity"],
            "index_identity": row["index_identity"],
            "snapshot_id": row["snapshot_id"],
            "snapshot_version": row["snapshot_version"],
            "logical_clock_floor": row["logical_clock_floor"],
            "node_count": row["node_count"],
            "visible_node_count": row["visible_node_count"],
            "edge_count": row["edge_count"],
            "manifest": PackageManifest.from_dict(
                __import__("json").loads(row["manifest_json"])
            ),
            "manifest_json": row["manifest_json"],
            "summary_json": row["summary_json"],
            "summary": __import__("json").loads(row["summary_json"]),
            "index_digest": row["index_digest"],
            "merge_digest": row["merge_digest"],
            "state": row["state"],
            "committed_at_ms": row["committed_at_ms"],
        }

    def classify_replacement(
        self,
        current: dict[str, Any] | None,
        new_manifest: PackageManifest,
        *,
        clock_rollback_tolerance_ms: int,
    ) -> dict[str, Any]:
        if current is None:
            return {
                "decision": "INITIAL_LOAD",
                "reason": "state database has no committed index pointer",
                "previous_index_version": None,
            }
        previous: PackageManifest = current["manifest"]
        if new_manifest.package_version < previous.package_version:
            raise fail(
                ErrorCode.VERSION_ROLLBACK,
                "new snapshot version is older than committed version",
                previous=previous.package_version,
                new=new_manifest.package_version,
            )
        if (
            new_manifest.logical_clock_floor
            < previous.logical_clock_floor - clock_rollback_tolerance_ms
        ):
            raise fail(
                ErrorCode.CLOCK_ROLLBACK_EXCEEDED,
                "logical clock floor rolled back beyond tolerance",
                previous=previous.logical_clock_floor,
                new=new_manifest.logical_clock_floor,
                tolerance=clock_rollback_tolerance_ms,
            )
        if new_manifest.snapshot_identity == previous.snapshot_identity:
            return {
                "decision": "NOOP",
                "reason": "snapshot identity is unchanged",
                "previous_index_version": current["index_version"],
            }

        old_hashes = previous.shard_hashes()
        new_hashes = new_manifest.shard_hashes()
        v1_base_unchanged = (
            previous.v1_snapshot_id == new_manifest.v1_snapshot_id
            and previous.v1_digest == new_manifest.v1_digest
        )
        old_is_subset = v1_base_unchanged and all(
            new_hashes.get(name) == digest for name, digest in old_hashes.items()
        )
        has_more_shards = len(new_hashes) > len(old_hashes)
        expands_bounds = (
            new_manifest.node_count_max > previous.node_count_max
            or new_manifest.node_count_min > previous.node_count_min
        )
        if (
            new_manifest.package_version >= previous.package_version
            and old_is_subset
            and (has_more_shards or expands_bounds)
        ):
            return {
                "decision": "INCREMENTAL_APPEND",
                "reason": "all previous shard hashes are retained and the package expands",
                "previous_index_version": current["index_version"],
                "added_shards": sorted(set(new_hashes) - set(old_hashes)),
            }
        if new_manifest.package_version > previous.package_version:
            return {
                "decision": "FULL_REPLACE",
                "reason": "snapshot version advanced with a changed shard set",
                "previous_index_version": current["index_version"],
                "removed_shards": sorted(set(old_hashes) - set(new_hashes)),
                "added_shards": sorted(set(new_hashes) - set(old_hashes)),
                "changed_shards": sorted(
                    name
                    for name in set(old_hashes) & set(new_hashes)
                    if old_hashes[name] != new_hashes[name]
                ),
            }
        raise fail(
            ErrorCode.SNAPSHOT_CONFLICT,
            "same snapshot version has different content",
            snapshot_version=new_manifest.package_version,
        )

    def commit_index(
        self,
        index: UnifiedIndex,
        *,
        node_decisions: tuple[dict[str, Any], ...],
        edge_decisions: tuple[dict[str, Any], ...],
        clock_rollback_tolerance_ms: int,
    ) -> dict[str, Any]:
        with self.lock():
            connection = self.connect(initialize=True)
            try:
                connection.execute("DROP TABLE IF EXISTS temp._stage_nodes")
                connection.execute("DROP TABLE IF EXISTS temp._stage_edges")
                connection.execute(
                    """
                    CREATE TEMP TABLE _stage_nodes (
                        node_id TEXT PRIMARY KEY,
                        label TEXT NOT NULL,
                        node_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TEMP TABLE _stage_edges (
                        source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        label TEXT NOT NULL,
                        weight REAL NOT NULL,
                        edge_json TEXT NOT NULL,
                        PRIMARY KEY (source_id, target_id, label)
                    )
                    """
                )
                for node_id in sorted(index.nodes):
                    node = index.nodes[node_id]
                    connection.execute(
                        "INSERT INTO _stage_nodes(node_id, label, node_json) VALUES (?, ?, ?)",
                        (
                            node_id,
                            node.label,
                            canonical_json(node.to_index_dict()),
                        ),
                    )
                    for edge in index.outgoing(node_id):
                        connection.execute(
                            """
                            INSERT INTO _stage_edges(
                                source_id, target_id, label, weight, edge_json
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                node_id,
                                edge.target,
                                edge.label,
                                edge.weight,
                                canonical_json(edge.to_dict()),
                            ),
                        )

                with self.transaction(connection):
                    self._cleanup_orphan_indexes(connection)
                    current = self.current_meta(connection)
                    migration = self.classify_replacement(
                        current,
                        index.manifest,
                        clock_rollback_tolerance_ms=clock_rollback_tolerance_ms,
                    )
                    existing = connection.execute(
                        "SELECT * FROM index_versions WHERE snapshot_identity = ?",
                        (index.manifest.snapshot_identity,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["index_version"] == index.index_version
                            and existing["state"] in {"committed", "superseded"}
                            and current is not None
                            and current["index_version"] == existing["index_version"]
                        ):
                            return {
                                "decision": "IDEMPOTENT_REPLAY",
                                "reason": "snapshot was already committed with identical build inputs",
                                "index_version": index.index_version,
                                "index_digest": existing["index_digest"],
                            }
                        raise fail(
                            ErrorCode.SNAPSHOT_CONFLICT,
                            "snapshot was already committed with different build inputs",
                            existing_index_version=existing["index_version"],
                            requested_index_version=index.index_version,
                        )
                    if migration["decision"] == "NOOP":
                        migration["index_version"] = current["index_version"]
                        return migration

                    now = _now_ms()
                    connection.execute(
                        """
                        INSERT INTO index_versions(
                            index_version, snapshot_identity, index_identity,
                            snapshot_id, snapshot_version, logical_clock_floor,
                            node_count, visible_node_count, edge_count,
                            manifest_json, summary_json, index_digest, merge_digest,
                            state, created_at_ms, committed_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                        """,
                        (
                            index.index_version,
                            index.manifest.snapshot_identity,
                            index.manifest.index_identity,
                            index.manifest.snapshot_id,
                            index.manifest.package_version,
                            index.manifest.logical_clock_floor,
                            index.summary["node_count"],
                            index.summary["visible_node_count"],
                            index.summary["edge_count"],
                            canonical_json(index.manifest.to_dict()),
                            canonical_json(index.summary),
                            index.digest,
                            index.merge_digest,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO index_nodes(index_version, node_id, label, node_json)
                        SELECT ?, node_id, label, node_json FROM _stage_nodes
                        ORDER BY node_id
                        """,
                        (index.index_version,),
                    )
                    connection.execute(
                        """
                        INSERT INTO index_edges(
                            index_version, source_id, target_id, label, weight, edge_json
                        )
                        SELECT ?, source_id, target_id, label, weight, edge_json
                        FROM _stage_edges
                        ORDER BY source_id, target_id, label
                        """,
                        (index.index_version,),
                    )
                    for decision in node_decisions:
                        connection.execute(
                            """
                            INSERT INTO merge_decisions(
                                index_version, kind, decision_key, decision_json
                            ) VALUES (?, 'node', ?, ?)
                            """,
                            (
                                index.index_version,
                                decision["node_id"],
                                canonical_json(decision),
                            ),
                        )
                    for ordinal, decision in enumerate(edge_decisions):
                        key = f"{decision['node_id']}:{decision['target']}:{decision['label']}:{ordinal}"
                        connection.execute(
                            """
                            INSERT INTO merge_decisions(
                                index_version, kind, decision_key, decision_json
                            ) VALUES (?, 'edge', ?, ?)
                            """,
                            (index.index_version, key, canonical_json(decision)),
                        )
                    connection.execute(
                        """
                        INSERT INTO migration_decisions(
                            index_version, decision, reason, details_json, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            index.index_version,
                            migration["decision"],
                            migration["reason"],
                            canonical_json(migration),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO state(key, value)
                        VALUES ('current_index_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (index.index_version,),
                    )
                    connection.execute(
                        """
                        UPDATE index_versions
                        SET state = 'superseded'
                        WHERE state = 'committed' AND index_version <> ?
                        """,
                        (index.index_version,),
                    )
                    migration["index_version"] = index.index_version
                    migration["index_digest"] = index.digest
                    migration["node_count"] = index.summary["node_count"]
                    migration["visible_node_count"] = index.summary["visible_node_count"]
                    migration["edge_count"] = index.summary["edge_count"]
                    return migration
            finally:
                connection.close()

    def load_index(
        self,
        index_version: str | None = None,
    ) -> tuple[UnifiedIndex, dict[str, Any]]:
        connection = self.connect(initialize=False)
        try:
            with self.transaction(connection, immediate=False):
                if index_version is None:
                    meta = self.current_meta(connection)
                    if meta is None:
                        raise fail(
                            ErrorCode.INDEX_NOT_FOUND,
                            "no committed index pointer exists",
                        )
                    version = meta["index_version"]
                else:
                    row = connection.execute(
                        "SELECT * FROM index_versions WHERE index_version = ?",
                        (index_version,),
                    ).fetchone()
                    if row is None:
                        raise fail(
                            ErrorCode.INDEX_NOT_FOUND,
                            "index version does not exist",
                            index_version=index_version,
                        )
                    meta = {
                        "index_version": row["index_version"],
                        "manifest": PackageManifest.from_dict(
                            __import__("json").loads(row["manifest_json"])
                        ),
                        "summary": __import__("json").loads(row["summary_json"]),
                        "index_digest": row["index_digest"],
                        "merge_digest": row["merge_digest"],
                        "state": row["state"],
                    }
                    version = index_version
                node_rows = [
                    {
                        "node_id": row["node_id"],
                        "node_json": __import__("json").loads(row["node_json"]),
                    }
                    for row in connection.execute(
                        "SELECT node_id, node_json FROM index_nodes WHERE index_version = ? ORDER BY node_id",
                        (version,),
                    )
                ]
                edge_rows = [
                    {
                        "node_id": row["source_id"],
                        "edge_json": __import__("json").loads(row["edge_json"]),
                    }
                    for row in connection.execute(
                        """
                        SELECT source_id, edge_json FROM index_edges
                        WHERE index_version = ?
                        ORDER BY source_id, target_id, label
                        """,
                        (version,),
                    )
                ]
                index = rehydrate_index(
                    index_version=version,
                    manifest=meta["manifest"],
                    node_rows=node_rows,
                    edge_rows=edge_rows,
                    persisted_summary=meta["summary"],
                    persisted_digest=meta["index_digest"],
                    merge_digest=meta["merge_digest"],
                )
                return index, meta
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise fail(
                    ErrorCode.INDEX_NOT_FOUND,
                    "state database is not initialized",
                    error=str(exc),
                )
            raise
        finally:
            connection.close()

    def ensure_run(
        self,
        *,
        run_id: str,
        index_version: str,
        request: TraversalRequest,
        plans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.lock():
            connection = self.connect(initialize=True)
            try:
                with self.transaction(connection):
                    existing = connection.execute(
                        "SELECT * FROM traversal_runs WHERE run_id = ? OR (index_version = ? AND request_digest = ?)",
                        (run_id, index_version, request.digest),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["run_id"] != run_id
                            or existing["index_version"] != index_version
                            or existing["request_digest"] != request.digest
                        ):
                            raise fail(
                                ErrorCode.IDEMPOTENCY_CONFLICT,
                                "run id/request conflicts with existing run",
                                existing_run_id=existing["run_id"],
                            )
                        return {"created": False, "run_id": run_id}
                    if not plans:
                        raise fail(
                            ErrorCode.INVALID_ARGUMENT,
                            "a traversal run requires at least one batch",
                        )
                    now = _now_ms()
                    connection.execute(
                        """
                        INSERT INTO traversal_runs(
                            run_id, index_version, request_digest, request_json,
                            state, total_batches, committed_batches,
                            created_at_ms, updated_at_ms
                        ) VALUES (?, ?, ?, ?, 'planned', ?, 0, ?, ?)
                        """,
                        (
                            run_id,
                            index_version,
                            request.digest,
                            canonical_json(request.to_dict()),
                            len(plans),
                            now,
                            now,
                        ),
                    )
                    for plan in plans:
                        connection.execute(
                            """
                            INSERT INTO traversal_batches(
                                run_id, batch_key, batch_index, starts_json,
                                content_digest, estimate_entries, state, updated_at_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)
                            """,
                            (
                                run_id,
                                plan["batch_key"],
                                plan["batch_index"],
                                canonical_json(plan["starts"]),
                                plan["content_digest"],
                                plan["estimate_entries"],
                                now,
                            ),
                        )
                    return {"created": True, "run_id": run_id}
            finally:
                connection.close()

    def get_run(self, run_id: str) -> dict[str, Any]:
        connection = self.connect(initialize=False)
        try:
            with self.transaction(connection, immediate=False):
                row = connection.execute(
                    "SELECT * FROM traversal_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise fail(
                        ErrorCode.RUN_NOT_FOUND,
                        "traversal run does not exist",
                        run_id=run_id,
                    )
                return _run_row_to_dict(row)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise fail(
                    ErrorCode.RUN_NOT_FOUND,
                    "traversal run does not exist",
                    run_id=run_id,
                )
            raise
        finally:
            connection.close()

    def get_latest_run(self, index_version: str | None = None) -> dict[str, Any] | None:
        connection = self.connect(initialize=False)
        try:
            with self.transaction(connection, immediate=False):
                if index_version is None:
                    meta = self.current_meta(connection)
                    if meta is None:
                        return None
                    index_version = meta["index_version"]
                row = connection.execute(
                    """
                    SELECT * FROM traversal_runs
                    WHERE index_version = ?
                    ORDER BY created_at_ms DESC, run_id DESC
                    LIMIT 1
                    """,
                    (index_version,),
                ).fetchone()
                if row is None:
                    return None
                return _run_row_to_dict(row)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        finally:
            connection.close()

    def list_batches(self, run_id: str) -> list[dict[str, Any]]:
        connection = self.connect(initialize=False)
        try:
            rows = connection.execute(
                """
                SELECT * FROM traversal_batches
                WHERE run_id = ?
                ORDER BY batch_index
                """,
                (run_id,),
            ).fetchall()
            return [_batch_row_to_dict(row) for row in rows]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        finally:
            connection.close()

    def validate_batch_plans(
        self,
        run_id: str,
        plans: list[dict[str, Any]],
    ) -> None:
        expected = {plan["batch_key"]: plan for plan in plans}
        connection = self.connect(initialize=False)
        try:
            with self.transaction(connection, immediate=False):
                rows = connection.execute(
                    """
                    SELECT batch_key, batch_index, starts_json, content_digest,
                           estimate_entries
                    FROM traversal_batches
                    WHERE run_id = ?
                    ORDER BY batch_index
                    """,
                    (run_id,),
                ).fetchall()
                if len(rows) != len(plans):
                    raise fail(
                        ErrorCode.CONTENT_DIGEST_MISMATCH,
                        "persisted batch count does not match rebuilt plan",
                        run_id=run_id,
                        expected=len(plans),
                        actual=len(rows),
                    )
                for row in rows:
                    plan = expected.get(row["batch_key"])
                    starts = __import__("json").loads(row["starts_json"])
                    if (
                        plan is None
                        or row["batch_index"] != plan["batch_index"]
                        or starts != plan["starts"]
                        or row["content_digest"] != plan["content_digest"]
                        or row["estimate_entries"] != plan["estimate_entries"]
                    ):
                        raise fail(
                            ErrorCode.CONTENT_DIGEST_MISMATCH,
                            "persisted batch content does not match rebuilt plan",
                            run_id=run_id,
                            batch_key=row["batch_key"],
                            expected_digest=(
                                plan["content_digest"] if plan else None
                            ),
                            actual_digest=row["content_digest"],
                            expected_starts=plan["starts"] if plan else None,
                            actual_starts=starts,
                        )
        finally:
            connection.close()

    def reserve_wave(
        self,
        *,
        run_id: str,
        request: TraversalRequest,
        expected_plans: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Atomically reserve a budget/task-capped wave.

        The reservation, budget accounting and abandonment decision happen in
        one SQLite transaction under the process lock, so concurrent workers
        cannot oversubscribe the run's memory budget.
        """

        with self.lock():
            connection = self.connect(initialize=False)
            try:
                with self.transaction(connection):
                    batch_rows = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM traversal_batches WHERE run_id = ? ORDER BY batch_index",
                            (run_id,),
                        )
                    ]
                    for row in batch_rows:
                        plan = expected_plans.get(row["batch_key"])
                        starts = __import__("json").loads(row["starts_json"])
                        if (
                            plan is None
                            or row["content_digest"] != plan["content_digest"]
                            or row["batch_index"] != plan["batch_index"]
                            or starts != plan["starts"]
                            or row["estimate_entries"] != plan["estimate_entries"]
                        ):
                            raise fail(
                                ErrorCode.CONTENT_DIGEST_MISMATCH,
                                "batch content mismatch before reservation",
                                run_id=run_id,
                                batch_key=row["batch_key"],
                            )
                    consumed = sum(
                        row["estimate_entries"]
                        for row in batch_rows
                        if row["state"] in {"succeeded", "failed", "running"}
                    )
                    remaining_budget = request.memory_budget_entries - consumed
                    selected: list[dict[str, Any]] = []
                    abandoned: list[dict[str, Any]] = []
                    abandon_rest = False
                    for row in batch_rows:
                        if row["state"] not in {"planned", "running"}:
                            continue
                        if len(selected) >= request.max_parallel_tasks:
                            break
                        if row["state"] == "running":
                            selected.append(row)
                            continue
                        if abandon_rest:
                            abandoned.append(row)
                            continue
                        if row["estimate_entries"] > remaining_budget:
                            abandon_rest = True
                            abandoned.append(row)
                            continue
                        selected.append(row)
                        remaining_budget -= row["estimate_entries"]

                    now = _now_ms()
                    for row in abandoned:
                        reason = (
                            "memory_budget_exhausted: "
                            f"need={row['estimate_entries']} "
                            f"remaining={max(remaining_budget, 0)}"
                        )
                        result_digest = digest_json(
                            {
                                "state": "abandoned",
                                "batch_key": row["batch_key"],
                                "reason": reason,
                            }
                        )
                        connection.execute(
                            """
                            UPDATE traversal_batches
                            SET state = 'abandoned', reason = ?, result_digest = ?,
                                updated_at_ms = ?
                            WHERE run_id = ? AND batch_key = ? AND state = 'planned'
                            """,
                            (
                                reason,
                                result_digest,
                                now,
                                run_id,
                                row["batch_key"],
                            ),
                        )
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO batch_commit_log(
                                run_id, batch_key, content_digest, result_digest,
                                state, committed_at_ms
                            ) VALUES (?, ?, ?, ?, 'abandoned', ?)
                            """,
                            (
                                run_id,
                                row["batch_key"],
                                row["content_digest"],
                                result_digest,
                                now,
                            ),
                        )
                    if abandoned:
                        connection.execute(
                            """
                            UPDATE traversal_runs
                            SET committed_batches = committed_batches + ?,
                                state = 'running', updated_at_ms = ?
                            WHERE run_id = ?
                            """,
                            (len(abandoned), now, run_id),
                        )
                    if selected:
                        connection.executemany(
                            """
                            UPDATE traversal_batches
                            SET state = 'running', updated_at_ms = ?
                            WHERE run_id = ? AND batch_key = ?
                            """,
                            [
                                (now, run_id, row["batch_key"])
                                for row in selected
                            ],
                        )
                        connection.execute(
                            """
                            UPDATE traversal_runs
                            SET state = 'running', updated_at_ms = ?
                            WHERE run_id = ?
                            """,
                            (now, run_id),
                        )
                    return [
                        {
                            "batch_key": row["batch_key"],
                            "batch_index": row["batch_index"],
                            "starts": __import__("json").loads(row["starts_json"]),
                            "content_digest": row["content_digest"],
                            "estimate_entries": row["estimate_entries"],
                            "state": "running",
                        }
                        for row in selected
                    ], len(abandoned)
            finally:
                connection.close()

    def commit_batch(
        self,
        *,
        run_id: str,
        batch_key: str,
        content_digest: str,
        state: str,
        result: dict[str, Any] | None,
        reason: str | None,
    ) -> dict[str, Any]:
        if state not in {"succeeded", "failed"}:
            raise fail(
                ErrorCode.INVALID_ARGUMENT,
                "commit_batch state must be succeeded or failed",
            )
        if result is not None and "result_digest" in result:
            payload = dict(result)
            claimed_digest = payload.pop("result_digest")
            actual_digest = digest_json(payload)
            if claimed_digest != actual_digest:
                raise fail(
                    ErrorCode.CONTENT_DIGEST_MISMATCH,
                    "batch result payload does not match its digest",
                    batch_key=batch_key,
                    expected=actual_digest,
                    actual=claimed_digest,
                )
            result_digest = actual_digest
        else:
            result_digest = (
                digest_json(result)
                if result is not None
                else digest_json({"state": state, "reason": reason})
            )
        with self.lock():
            connection = self.connect(initialize=False)
            try:
                with self.transaction(connection):
                    row = connection.execute(
                        "SELECT * FROM traversal_batches WHERE run_id = ? AND batch_key = ?",
                        (run_id, batch_key),
                    ).fetchone()
                    if row is None:
                        raise fail(
                            ErrorCode.CONTENT_DIGEST_MISMATCH,
                            "batch does not exist",
                            run_id=run_id,
                            batch_key=batch_key,
                        )
                    if row["content_digest"] != content_digest:
                        raise fail(
                            ErrorCode.CONTENT_DIGEST_MISMATCH,
                            "batch content digest mismatch before commit",
                            run_id=run_id,
                            batch_key=batch_key,
                            expected=row["content_digest"],
                            actual=content_digest,
                        )
                    if row["state"] in {"succeeded", "failed", "abandoned"}:
                        if row["result_digest"] == result_digest:
                            return {
                                "idempotent": True,
                                "batch_key": batch_key,
                                "state": row["state"],
                                "result_digest": result_digest,
                            }
                        raise fail(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "batch was already committed with a different result",
                            run_id=run_id,
                            batch_key=batch_key,
                            existing_state=row["state"],
                            existing_digest=row["result_digest"],
                            incoming_digest=result_digest,
                        )
                    now = _now_ms()
                    connection.execute(
                        """
                        UPDATE traversal_batches
                        SET state = ?, reason = ?, result_json = ?, result_digest = ?,
                            updated_at_ms = ?
                        WHERE run_id = ? AND batch_key = ?
                        """,
                        (
                            state,
                            reason,
                            canonical_json(result) if result is not None else None,
                            result_digest,
                            now,
                            run_id,
                            batch_key,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO batch_commit_log(
                            run_id, batch_key, content_digest, result_digest,
                            state, committed_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, batch_key, content_digest, result_digest, state, now),
                    )
                    connection.execute(
                        """
                        UPDATE traversal_runs
                        SET committed_batches = committed_batches + 1,
                            state = 'running', updated_at_ms = ?
                        WHERE run_id = ?
                        """,
                        (now, run_id),
                    )
                    return {
                        "idempotent": False,
                        "batch_key": batch_key,
                        "state": state,
                        "result_digest": result_digest,
                    }
            finally:
                connection.close()

    def finalize_run(self, run_id: str) -> dict[str, Any]:
        with self.lock():
            connection = self.connect(initialize=False)
            try:
                with self.transaction(connection):
                    run = connection.execute(
                        "SELECT * FROM traversal_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    if run is None:
                        raise fail(
                            ErrorCode.RUN_NOT_FOUND,
                            "traversal run does not exist",
                            run_id=run_id,
                        )
                    batches = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM traversal_batches WHERE run_id = ? ORDER BY batch_index",
                            (run_id,),
                        )
                    ]
                    terminal = {"succeeded", "failed", "abandoned"}
                    if any(batch["state"] not in terminal for batch in batches):
                        return {"finalized": False, "run_id": run_id}
                    outcomes = {
                        "started_succeeded": 0,
                        "started_failed": 0,
                        "abandoned": 0,
                        "not_started": 0,
                        "started_in_progress": 0,
                    }
                    results: list[dict[str, Any]] = []
                    failures: list[dict[str, Any]] = []
                    abandoned: list[dict[str, Any]] = []
                    result_digests: list[dict[str, Any]] = []
                    for batch in batches:
                        if batch["state"] == "succeeded":
                            outcomes["started_succeeded"] += 1
                            result = (
                                __import__("json").loads(batch["result_json"])
                                if batch["result_json"]
                                else None
                            )
                            results.append(result)
                        elif batch["state"] == "failed":
                            outcomes["started_failed"] += 1
                            failure_payload = (
                                __import__("json").loads(batch["result_json"])
                                if batch["result_json"]
                                else None
                            )
                            failures.append(
                                {
                                    "batch_key": batch["batch_key"],
                                    "reason": batch["reason"],
                                    "result": failure_payload,
                                }
                            )
                        elif batch["state"] == "abandoned":
                            outcomes["abandoned"] += 1
                            abandoned.append(
                                {
                                    "batch_key": batch["batch_key"],
                                    "reason": batch["reason"],
                                }
                            )
                        else:
                            outcomes["not_started"] += 1
                        result_digests.append(
                            {
                                "batch_key": batch["batch_key"],
                                "content_digest": batch["content_digest"],
                                "result_digest": batch["result_digest"],
                                "state": batch["state"],
                            }
                        )
                    final_result = {
                        "run_id": run_id,
                        "index_version": run["index_version"],
                        "request_digest": run["request_digest"],
                        "outcomes": outcomes,
                        "failures": failures,
                        "abandoned": abandoned,
                        "batch_results": results,
                        "batch_digests": result_digests,
                    }
                    truth_digest = digest_json(
                        {
                            "run_id": run_id,
                            "index_version": run["index_version"],
                            "request_digest": run["request_digest"],
                            "batch_digests": result_digests,
                        }
                    )
                    final_digest = digest_json(final_result)
                    run_state = (
                        "completed_with_failures"
                        if failures or abandoned
                        else "completed"
                    )
                    now = _now_ms()
                    connection.execute(
                        """
                        UPDATE traversal_runs
                        SET state = ?, committed_batches = ?, truth_digest = ?,
                            result_digest = ?, result_json = ?, updated_at_ms = ?
                        WHERE run_id = ?
                        """,
                        (
                            run_state,
                            len(batches),
                            truth_digest,
                            final_digest,
                            canonical_json(final_result),
                            now,
                            run_id,
                        ),
                    )
                    return {
                        "finalized": True,
                        "run_id": run_id,
                        "state": run_state,
                        "truth_digest": truth_digest,
                        "result_digest": final_digest,
                        "outcomes": outcomes,
                    }
            finally:
                connection.close()

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        connection = self.connect(initialize=False)
        try:
            with self.transaction(connection, immediate=False):
                current = self.current_meta(connection)
                if current is None:
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "state": "empty",
                        "index": None,
                        "traversal": None,
                    }
                if run_id is None:
                    row = connection.execute(
                        """
                        SELECT * FROM traversal_runs
                        WHERE index_version = ?
                        ORDER BY created_at_ms DESC, run_id DESC
                        LIMIT 1
                        """,
                        (current["index_version"],),
                    ).fetchone()
                    view_meta = current
                else:
                    row = connection.execute(
                        "SELECT * FROM traversal_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise fail(
                            ErrorCode.RUN_NOT_FOUND,
                            "traversal run does not exist",
                            run_id=run_id,
                        )
                    if row["index_version"] == current["index_version"]:
                        view_meta = current
                    else:
                        version_row = connection.execute(
                            "SELECT * FROM index_versions WHERE index_version = ?",
                            (row["index_version"],),
                        ).fetchone()
                        if version_row is None:
                            raise fail(
                                ErrorCode.INDEX_NOT_FOUND,
                                "run index version no longer exists",
                                index_version=row["index_version"],
                            )
                        view_meta = {
                            "index_version": version_row["index_version"],
                            "snapshot_id": version_row["snapshot_id"],
                            "snapshot_version": version_row["snapshot_version"],
                            "node_count": version_row["node_count"],
                            "visible_node_count": version_row["visible_node_count"],
                            "edge_count": version_row["edge_count"],
                            "index_digest": version_row["index_digest"],
                            "merge_digest": version_row["merge_digest"],
                            "committed_at_ms": version_row["committed_at_ms"],
                        }
                traversal = None
                if row is not None:
                    batches = [
                        dict(item)
                        for item in connection.execute(
                            "SELECT * FROM traversal_batches WHERE run_id = ? ORDER BY batch_index",
                            (row["run_id"],),
                        )
                    ]
                    counts = {
                        "not_started": 0,
                        "started_in_progress": 0,
                        "started_succeeded": 0,
                        "started_failed": 0,
                        "abandoned": 0,
                    }
                    abandoned: list[dict[str, Any]] = []
                    failed: list[dict[str, Any]] = []
                    for batch in batches:
                        if batch["state"] == "succeeded":
                            counts["started_succeeded"] += 1
                        elif batch["state"] == "failed":
                            counts["started_failed"] += 1
                            failed.append(
                                {
                                    "batch_key": batch["batch_key"],
                                    "reason": batch["reason"],
                                }
                            )
                        elif batch["state"] == "running":
                            counts["started_in_progress"] += 1
                        elif batch["state"] == "abandoned":
                            counts["abandoned"] += 1
                            abandoned.append(
                                {
                                    "batch_key": batch["batch_key"],
                                    "reason": batch["reason"],
                                }
                            )
                        else:
                            counts["not_started"] += 1
                    committed = sum(
                        1
                        for batch in batches
                        if batch["state"] in {"succeeded", "failed", "abandoned"}
                    )
                    traversal = {
                        "run_id": row["run_id"],
                        "index_version": row["index_version"],
                        "request_digest": row["request_digest"],
                        "state": row["state"],
                        "watermark": committed,
                        "total_batches": row["total_batches"],
                        "outcomes": counts,
                        "abandoned": sorted(abandoned, key=lambda item: item["batch_key"]),
                        "failed": sorted(failed, key=lambda item: item["batch_key"]),
                        "truth_digest": row["truth_digest"],
                        "result_digest": row["result_digest"],
                    }
                index_summary = {
                    "version": view_meta["index_version"],
                    "snapshot_id": view_meta["snapshot_id"],
                    "snapshot_version": view_meta["snapshot_version"],
                    "node_count": view_meta["node_count"],
                    "visible_node_count": view_meta["visible_node_count"],
                    "edge_count": view_meta["edge_count"],
                    "index_digest": view_meta["index_digest"],
                    "merge_digest": view_meta["merge_digest"],
                    "committed_at_ms": view_meta["committed_at_ms"],
                }
                return {
                    "schema_version": SCHEMA_VERSION,
                    "state": "ready",
                    "index": index_summary,
                    "current_index_version": current["index_version"],
                    "traversal": traversal,
                }
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {
                    "schema_version": SCHEMA_VERSION,
                    "state": "empty",
                    "index": None,
                    "traversal": None,
                }
            raise
        finally:
            connection.close()

    def list_merge_decisions(self, index_version: str) -> list[dict[str, Any]]:
        connection = self.connect(initialize=False)
        try:
            rows = connection.execute(
                """
                SELECT kind, decision_key, decision_json
                FROM merge_decisions
                WHERE index_version = ?
                ORDER BY kind, decision_key
                """,
                (index_version,),
            ).fetchall()
            return [
                {
                    "kind": row["kind"],
                    "key": row["decision_key"],
                    "decision": __import__("json").loads(row["decision_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def integrity_check(self) -> dict[str, Any]:
        connection = self.connect(initialize=False)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            return {
                "integrity_check": result,
                "foreign_key_violations": len(foreign),
            }
        finally:
            connection.close()


def _run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["request"] = __import__("json").loads(result.pop("request_json"))
    if result["result_json"] is not None:
        result["result"] = __import__("json").loads(result.pop("result_json"))
    else:
        result.pop("result_json")
        result["result"] = None
    return result


def _batch_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["starts"] = __import__("json").loads(result.pop("starts_json"))
    if result["result_json"] is not None:
        result["result"] = __import__("json").loads(result.pop("result_json"))
    else:
        result.pop("result_json")
        result["result"] = None
    return result

