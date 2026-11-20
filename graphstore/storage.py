from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .checksums import (
    canonical_json,
    digest_edges,
    sha256_object,
)
from .errors import (
    E_BATCH_DIGEST_MISMATCH,
    E_BUILD_IN_PROGRESS,
    E_CLOCK_ROLLBACK,
    E_INDEX_NOT_FOUND,
    E_STATE_CORRUPT,
    E_VERSION_CONFLICT,
    E_VERSION_ROLLBACK,
    GraphStoreError,
    require,
)
from .model import (
    ClockConfig,
    GraphIndex,
    MergeAudit,
    SourceVersion,
    UnifiedEdge,
    UnifiedNode,
    build_label_index,
)
from .package import LoadedPackage


SCHEMA_VERSION = "1"
NODE_BATCH_SIZE = 128
EDGE_BATCH_SIZE = 256


class FileLock:
    """Cross-platform exclusive file lock used around state transitions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            if self.handle.read(1) == b"":
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def maybe_crash(point: str) -> None:
    if os.environ.get("GRAPHSTORE_ENABLE_TEST_FAILPOINTS") != "1":
        return
    configured = os.environ.get("GRAPHSTORE_CRASH_AT", "")
    if configured == point:
        os.kill(os.getpid(), 9 if os.name != "nt" else 15)


def _parse_json_field(
    raw: str,
    *,
    code: str,
    message: str,
    **details: Any,
) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GraphStoreError(code, message, **details) from exc


@dataclass(frozen=True)
class ReplaceOutcome:
    classification: str
    idempotent: bool
    index_version: int
    package_id: str
    package_version: int
    node_count: int
    edge_count: int
    tombstone_count: int
    adjacency_digest: str
    decision_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjacency_digest": self.adjacency_digest,
            "classification": self.classification,
            "decision_reason": self.decision_reason,
            "edge_count": self.edge_count,
            "idempotent": self.idempotent,
            "index_version": self.index_version,
            "node_count": self.node_count,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "tombstone_count": self.tombstone_count,
        }


def _schema_sql() -> str:
    return """
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS clock_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        now_ms INTEGER NOT NULL,
        anchor_ms INTEGER NOT NULL,
        step_ms INTEGER NOT NULL,
        max_rollback_ms INTEGER NOT NULL,
        scale_numerator INTEGER NOT NULL,
        scale_denominator INTEGER NOT NULL,
        rounding TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS index_versions (
        version INTEGER PRIMARY KEY AUTOINCREMENT,
        package_id TEXT NOT NULL,
        package_version INTEGER NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        logical_clock_ms INTEGER NOT NULL,
        change_kind TEXT NOT NULL,
        classification TEXT NOT NULL,
        decision_reason TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('building', 'committed', 'aborted')),
        stage_phase TEXT NOT NULL,
        stage_cursor TEXT,
        node_count INTEGER NOT NULL,
        edge_count INTEGER NOT NULL,
        tombstone_count INTEGER NOT NULL,
        adjacency_digest TEXT NOT NULL,
        package_summary_json TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        migration_json TEXT NOT NULL,
        committed_clock_ms INTEGER,
        UNIQUE (package_id, package_version)
    );

    CREATE TABLE IF NOT EXISTS current_index (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL REFERENCES index_versions(version),
        updated_clock_ms INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS index_nodes (
        version INTEGER NOT NULL REFERENCES index_versions(version) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        label TEXT NOT NULL,
        weight REAL NOT NULL,
        written_at_ms INTEGER NOT NULL,
        tombstone INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        source_format TEXT NOT NULL,
        source_snapshot_version INTEGER NOT NULL,
        source_logical_clock_ms INTEGER NOT NULL,
        source_shard_generation INTEGER NOT NULL,
        source_priority INTEGER NOT NULL,
        decision_reason TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        PRIMARY KEY (version, node_id)
    );

    CREATE INDEX IF NOT EXISTS idx_index_nodes_label
        ON index_nodes(version, label, node_id);

    CREATE TABLE IF NOT EXISTS index_edges (
        version INTEGER NOT NULL REFERENCES index_versions(version) ON DELETE CASCADE,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        label TEXT NOT NULL,
        weight REAL NOT NULL,
        source_format TEXT NOT NULL,
        source_snapshot_version INTEGER NOT NULL,
        source_logical_clock_ms INTEGER NOT NULL,
        source_shard_generation INTEGER NOT NULL,
        source_priority INTEGER NOT NULL,
        decision_reason TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        PRIMARY KEY (version, from_id, to_id, label)
    );

    CREATE INDEX IF NOT EXISTS idx_index_edges_from
        ON index_edges(version, from_id, to_id, label);

    CREATE TABLE IF NOT EXISTS merge_decisions (
        version INTEGER NOT NULL REFERENCES index_versions(version) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        target_key TEXT NOT NULL,
        reason TEXT NOT NULL,
        winner_json TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        PRIMARY KEY (version, seq)
    );

    CREATE TABLE IF NOT EXISTS migration_decisions (
        version INTEGER NOT NULL REFERENCES index_versions(version) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        PRIMARY KEY (version, node_id)
    );

    CREATE TABLE IF NOT EXISTS traverse_batches (
        batch_id TEXT PRIMARY KEY,
        index_version INTEGER NOT NULL REFERENCES index_versions(version),
        query_digest TEXT NOT NULL,
        batch_content_digest TEXT NOT NULL,
        query_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('prepared', 'running', 'committed')),
        next_depth INTEGER NOT NULL,
        progress_json TEXT NOT NULL,
        result_json TEXT,
        result_digest TEXT,
        stats_json TEXT,
        created_clock_ms INTEGER NOT NULL,
        updated_clock_ms INTEGER NOT NULL,
        committed_clock_ms INTEGER
    );

    CREATE TABLE IF NOT EXISTS traverse_watermarks (
        batch_id TEXT PRIMARY KEY REFERENCES traverse_batches(batch_id),
        index_version INTEGER NOT NULL,
        batch_content_digest TEXT NOT NULL,
        result_digest TEXT NOT NULL,
        committed_clock_ms INTEGER NOT NULL
    );
    """


class GraphStore:
    def __init__(self, db_path: Path, clock: ClockConfig) -> None:
        self.db_path = db_path.resolve()
        self.clock = clock
        self.lock_path = self.db_path.with_name(self.db_path.name + ".lock")

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=10.0)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.isolation_level = None
        return connection

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect(read_only=read_only)
        try:
            yield connection
        finally:
            connection.close()

    def _init_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(_schema_sql())
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        else:
            require(
                row["value"] == SCHEMA_VERSION,
                E_STATE_CORRUPT,
                "unsupported persistent schema version",
                expected=SCHEMA_VERSION,
                observed=row["value"],
            )

    def assert_clock_compatible(self) -> None:
        if not self.exists:
            return
        with self.connection(read_only=True) as connection:
            try:
                row = connection.execute("SELECT * FROM clock_state WHERE id = 1").fetchone()
            except sqlite3.Error:
                return
        if row is None:
            return
        expected = (
            self.clock.anchor_ms,
            self.clock.step_ms,
            self.clock.max_rollback_ms,
            self.clock.v1_scale_numerator,
            self.clock.v1_scale_denominator,
            self.clock.v1_rounding,
        )
        observed = (
            row["anchor_ms"],
            row["step_ms"],
            row["max_rollback_ms"],
            row["scale_numerator"],
            row["scale_denominator"],
            row["rounding"],
        )
        if expected != observed:
            if self.clock.anchor_ms < row["anchor_ms"] - row["max_rollback_ms"]:
                raise GraphStoreError(
                    E_CLOCK_ROLLBACK,
                    "injected clock anchor moved backwards beyond tolerance",
                    persisted_anchor_ms=row["anchor_ms"],
                    injected_anchor_ms=self.clock.anchor_ms,
                    max_rollback_ms=row["max_rollback_ms"],
                )
            raise GraphStoreError(
                E_STATE_CORRUPT,
                "injected clock configuration differs from the committed store",
                expected=list(observed),
                injected=list(expected),
            )

    def _ensure_clock(self, connection: sqlite3.Connection) -> sqlite3.Row:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM clock_state WHERE id = 1").fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO clock_state(
                        id, now_ms, anchor_ms, step_ms, max_rollback_ms,
                        scale_numerator, scale_denominator, rounding
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.clock.anchor_ms,
                        self.clock.anchor_ms,
                        self.clock.step_ms,
                        self.clock.max_rollback_ms,
                        self.clock.v1_scale_numerator,
                        self.clock.v1_scale_denominator,
                        self.clock.v1_rounding,
                    ),
                )
            else:
                expected = (
                    self.clock.anchor_ms,
                    self.clock.step_ms,
                    self.clock.max_rollback_ms,
                    self.clock.v1_scale_numerator,
                    self.clock.v1_scale_denominator,
                    self.clock.v1_rounding,
                )
                observed = (
                    row["anchor_ms"],
                    row["step_ms"],
                    row["max_rollback_ms"],
                    row["scale_numerator"],
                    row["scale_denominator"],
                    row["rounding"],
                )
                require(
                    expected == observed,
                    E_CLOCK_ROLLBACK
                    if expected[0] < observed[0]
                    else E_STATE_CORRUPT,
                    "injected clock configuration does not match the store",
                    expected=list(observed),
                    injected=list(expected),
                )
            row = connection.execute("SELECT * FROM clock_state WHERE id = 1").fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return row

    def _advance_clock(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT * FROM clock_state WHERE id = 1").fetchone()
        require(row is not None, E_STATE_CORRUPT, "clock state is missing")
        next_ms = int(row["now_ms"]) + int(row["step_ms"])
        connection.execute("UPDATE clock_state SET now_ms = ? WHERE id = 1", (next_ms,))
        return next_ms

    def _current_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT iv.*
            FROM current_index ci
            JOIN index_versions iv ON iv.version = ci.version
            WHERE ci.id = 1
            """
        ).fetchone()
        if row is not None:
            require(
                row["status"] == "committed",
                E_STATE_CORRUPT,
                "current index pointer references a non-committed version",
                version=row["version"],
                status=row["status"],
            )
        return row

    def current_index_version(self) -> int | None:
        if not self.exists:
            return None
        with self.connection(read_only=True) as connection:
            row = self._current_row(connection)
            return None if row is None else int(row["version"])

    @staticmethod
    def _package_summary(package: LoadedPackage, classification: str) -> dict[str, Any]:
        semantic_digest = sha256_object(
            {
                "adjacency_digest": package.index.adjacency_digest(),
                "change_kind": package.change_kind,
                "edge_count": package.index.edge_count,
                "logical_clock_ms": package.logical_clock_ms,
                "migration_count": len(package.index.audit.migrations),
                "node_count": package.index.node_count,
                "node_count_bounds": package.node_count_bounds,
                "package_id": package.package_id,
                "package_version": package.package_version,
                "shard_hashes": package.shard_hashes,
                "tombstone_count": package.index.tombstone_count,
                "v1_sha256": package.v1.sha256,
                "v1_snapshot_version": package.v1.snapshot_version,
                "v2_manifest_sha256": package.v2.manifest_sha256,
                "v2_metadata_sha256": package.v2.metadata_sha256,
                "v2_snapshot_version": package.v2.snapshot_version,
            }
        )
        return {
            "adjacency_digest": package.index.adjacency_digest(),
            "change_kind": package.change_kind,
            "classification": classification,
            "edge_count": package.index.edge_count,
            "logical_clock_ms": package.logical_clock_ms,
            "manifest_sha256": package.manifest_sha256,
            "migration_count": len(package.index.audit.migrations),
            "node_count": package.index.node_count,
            "node_count_bounds": package.node_count_bounds,
            "package_id": package.package_id,
            "package_version": package.package_version,
            "semantic_digest": semantic_digest,
            "shard_hashes": package.shard_hashes,
            "tombstone_count": package.index.tombstone_count,
            "v1_sha256": package.v1.sha256,
            "v1_snapshot_version": package.v1.snapshot_version,
            "v2_manifest_sha256": package.v2.manifest_sha256,
            "v2_metadata_sha256": package.v2.metadata_sha256,
            "v2_snapshot_version": package.v2.snapshot_version,
        }

    @staticmethod
    def _existing_content_matches(
        existing: sqlite3.Row, package: LoadedPackage
    ) -> bool:
        try:
            summary = json.loads(existing["package_summary_json"])
            semantic_digest = GraphStore._package_summary(
                package, existing["classification"]
            )["semantic_digest"]
            return summary.get("semantic_digest") == semantic_digest
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False

    def _classify(
        self,
        package: LoadedPackage,
        current: sqlite3.Row | None,
        downgrade_policy: str,
    ) -> tuple[str, str]:
        if current is None:
            return "initial_load", "no_current_index"
        if current["manifest_sha256"] == package.manifest_sha256:
            return "no_op", "manifest_and_source_digests_identical"
        if package.package_version < int(current["package_version"]):
            if downgrade_policy == "reject":
                raise GraphStoreError(
                    E_VERSION_ROLLBACK,
                    "snapshot package version would move backwards",
                    current_package_version=int(current["package_version"]),
                    incoming_package_version=package.package_version,
                    policy=downgrade_policy,
                )
            return "full_replace", "explicit_downgrade_version_rollback_accepted"
        if package.package_version == int(current["package_version"]):
            raise GraphStoreError(
                E_VERSION_CONFLICT,
                "same package version has different snapshot content",
                package_id=package.package_id,
                package_version=package.package_version,
                current_manifest_sha256=current["manifest_sha256"],
                incoming_manifest_sha256=package.manifest_sha256,
            )
        clock_floor = int(current["logical_clock_ms"]) - self.clock.max_rollback_ms
        if package.logical_clock_ms < clock_floor:
            if downgrade_policy == "reject":
                raise GraphStoreError(
                    E_CLOCK_ROLLBACK,
                    "snapshot logical clock moved backwards beyond tolerance",
                    current_logical_clock_ms=int(current["logical_clock_ms"]),
                    incoming_logical_clock_ms=package.logical_clock_ms,
                    max_rollback_ms=self.clock.max_rollback_ms,
                    policy=downgrade_policy,
                )
            return "full_replace", "explicit_downgrade_clock_rollback_accepted"
        current_count = int(current["node_count"])
        bounds = package.node_count_bounds
        if package.change_kind == "append":
            try:
                previous_summary = json.loads(current["package_summary_json"])
                previous_shards = previous_summary.get("shard_hashes", {})
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_shards = {}
            inherited_hashes = all(
                package.shard_hashes.get(shard_id) == digest
                for shard_id, digest in previous_shards.items()
            )
            if (
                bounds["min"] <= current_count <= bounds["max"]
                and package.index.node_count >= current_count
                and inherited_hashes
            ):
                return (
                    "incremental_append",
                    "bounded_append_with_inherited_shards_and_complete_materialization",
                )
            return (
                "full_replace",
                "append_declaration_failed_bounds_or_shard_inheritance",
            )
        return "full_replace", "higher_version_complete_snapshot"

    def replace(
        self,
        package: LoadedPackage,
        *,
        downgrade_policy: str = "reject",
    ) -> ReplaceOutcome:
        require(
            downgrade_policy in {"reject", "allow"},
            "GS_INVALID_QUERY",
            "downgrade policy must be reject or allow",
            policy=downgrade_policy,
        )
        self.assert_clock_compatible()
        with FileLock(self.lock_path):
            with self.connection() as connection:
                self._init_schema(connection)
                self._ensure_clock(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = self._current_row(connection)
                    existing = connection.execute(
                        """
                        SELECT * FROM index_versions
                        WHERE package_id = ? AND package_version = ?
                        """,
                        (package.package_id, package.package_version),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["manifest_sha256"] != package.manifest_sha256
                            and not self._existing_content_matches(existing, package)
                        ):
                            raise GraphStoreError(
                                E_VERSION_CONFLICT,
                                "same package version was already committed with different content",
                                package_id=package.package_id,
                                package_version=package.package_version,
                                committed_manifest_sha256=existing["manifest_sha256"],
                                incoming_manifest_sha256=package.manifest_sha256,
                            )
                        if existing["status"] == "committed":
                            require(
                                current is not None,
                                E_STATE_CORRUPT,
                                "committed versions exist but current index is missing",
                            )
                            if int(existing["version"]) == int(current["version"]):
                                connection.commit()
                                return ReplaceOutcome(
                                    classification="no_op",
                                    idempotent=True,
                                    index_version=int(existing["version"]),
                                    package_id=package.package_id,
                                    package_version=package.package_version,
                                    node_count=int(existing["node_count"]),
                                    edge_count=int(existing["edge_count"]),
                                    tombstone_count=int(existing["tombstone_count"]),
                                    adjacency_digest=existing["adjacency_digest"],
                                    decision_reason="already_current_identical_manifest",
                                )
                            if package.package_version < int(
                                current["package_version"]
                            ) and downgrade_policy == "reject":
                                raise GraphStoreError(
                                    E_VERSION_ROLLBACK,
                                    "historical package version would move current pointer backwards",
                                    current_package_version=int(
                                        current["package_version"]
                                    ),
                                    incoming_package_version=package.package_version,
                                    current_index_version=int(current["version"]),
                                    historical_index_version=int(existing["version"]),
                                    policy=downgrade_policy,
                                )
                            if package.package_version == int(
                                current["package_version"]
                            ):
                                raise GraphStoreError(
                                    E_VERSION_CONFLICT,
                                    "same version belongs to a different committed snapshot",
                                    current_package_id=current["package_id"],
                                    incoming_package_id=package.package_id,
                                    package_version=package.package_version,
                                )
                            clock_floor = int(
                                current["logical_clock_ms"]
                            ) - self.clock.max_rollback_ms
                            if (
                                package.logical_clock_ms < clock_floor
                                and downgrade_policy == "reject"
                            ):
                                raise GraphStoreError(
                                    E_CLOCK_ROLLBACK,
                                    "historical package logical clock would move current pointer backwards",
                                    current_logical_clock_ms=int(
                                        current["logical_clock_ms"]
                                    ),
                                    incoming_logical_clock_ms=package.logical_clock_ms,
                                    max_rollback_ms=self.clock.max_rollback_ms,
                                    policy=downgrade_policy,
                                )
                            reason = (
                                "explicit_downgrade_restore_committed_version"
                                if package.package_version
                                < int(current["package_version"])
                                else "restore_newer_committed_version_after_downgrade"
                            )
                            now = self._advance_clock(connection)
                            connection.execute(
                                """
                                UPDATE index_versions
                                SET classification = 'downgrade_restore',
                                    decision_reason = ?,
                                    committed_clock_ms = ?
                                WHERE version = ?
                                """,
                                (reason, now, int(existing["version"])),
                            )
                            connection.execute(
                                """
                                UPDATE current_index
                                SET version = ?, updated_clock_ms = ?
                                WHERE id = 1
                                """,
                                (int(existing["version"]), now),
                            )
                            connection.commit()
                            maybe_crash("replace_after_pointer_restore")
                            return ReplaceOutcome(
                                classification="downgrade_restore",
                                idempotent=False,
                                index_version=int(existing["version"]),
                                package_id=package.package_id,
                                package_version=package.package_version,
                                node_count=int(existing["node_count"]),
                                edge_count=int(existing["edge_count"]),
                                tombstone_count=int(existing["tombstone_count"]),
                                adjacency_digest=existing["adjacency_digest"],
                                decision_reason=reason,
                            )
                        if existing["status"] != "building":
                            raise GraphStoreError(
                                E_STATE_CORRUPT,
                                "package version is in an unsupported state",
                                version=int(existing["version"]),
                                status=existing["status"],
                            )
                        if current is not None and int(existing["version"]) != int(
                            current["version"]
                        ):
                            if package.package_version < int(
                                current["package_version"]
                            ):
                                raise GraphStoreError(
                                    E_VERSION_ROLLBACK,
                                    "building version is now older than the current index",
                                    current_package_version=int(
                                        current["package_version"]
                                    ),
                                    incoming_package_version=package.package_version,
                                )
                            if package.package_version == int(
                                current["package_version"]
                            ):
                                raise GraphStoreError(
                                    E_VERSION_CONFLICT,
                                    "building snapshot version conflicts with current snapshot version",
                                    current_package_id=current["package_id"],
                                    incoming_package_id=package.package_id,
                                    package_version=package.package_version,
                                )
                            clock_floor = int(
                                current["logical_clock_ms"]
                            ) - self.clock.max_rollback_ms
                            if (
                                package.logical_clock_ms < clock_floor
                                and downgrade_policy == "reject"
                            ):
                                raise GraphStoreError(
                                    E_CLOCK_ROLLBACK,
                                    "building snapshot logical clock is now behind the current index",
                                    current_logical_clock_ms=int(
                                        current["logical_clock_ms"]
                                    ),
                                    incoming_logical_clock_ms=package.logical_clock_ms,
                                    max_rollback_ms=self.clock.max_rollback_ms,
                                )
                        classification = existing["classification"]
                        reason = existing["decision_reason"]
                        connection.commit()
                        version = int(existing["version"])
                    else:
                        classification, reason = self._classify(
                            package, current, downgrade_policy
                        )
                        if classification == "no_op":
                            connection.commit()
                            assert current is not None
                            return ReplaceOutcome(
                                classification=classification,
                                idempotent=True,
                                index_version=int(current["version"]),
                                package_id=package.package_id,
                                package_version=package.package_version,
                                node_count=int(current["node_count"]),
                                edge_count=int(current["edge_count"]),
                                tombstone_count=int(current["tombstone_count"]),
                                adjacency_digest=current["adjacency_digest"],
                                decision_reason=reason,
                            )
                        summary = self._package_summary(package, classification)
                        cursor = connection.execute(
                            """
                            INSERT INTO index_versions(
                                package_id, package_version, manifest_sha256,
                                logical_clock_ms, change_kind, classification,
                                decision_reason, status, stage_phase, stage_cursor,
                                node_count, edge_count, tombstone_count,
                                adjacency_digest, package_summary_json,
                                decision_json, migration_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', 'nodes', NULL,
                                      ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                package.package_id,
                                package.package_version,
                                package.manifest_sha256,
                                package.logical_clock_ms,
                                package.change_kind,
                                classification,
                                reason,
                                package.index.node_count,
                                package.index.edge_count,
                                package.index.tombstone_count,
                                package.index.adjacency_digest(),
                                canonical_json(summary),
                                canonical_json(
                                    list(package.index.audit.node_decisions)
                                    + list(package.index.audit.edge_decisions)
                                ),
                                canonical_json(package.index.audit.migrations),
                            ),
                        )
                        version = int(cursor.lastrowid)
                        connection.commit()
                    maybe_crash("replace_after_build_row")
                except Exception:
                    connection.rollback()
                    raise

                self._stage_nodes(connection, version, package.index)
                self._stage_edges(connection, version, package.index)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        UPDATE index_versions
                        SET stage_phase = 'audit', stage_cursor = NULL
                        WHERE version = ?
                        """,
                        (version,),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                maybe_crash("replace_before_finalize")
                outcome = self._finalize(connection, version, package)
                maybe_crash("replace_after_commit")
                return outcome

    def _stage_nodes(
        self, connection: sqlite3.Connection, version: int, index: GraphIndex
    ) -> None:
        row = connection.execute(
            "SELECT stage_phase, stage_cursor FROM index_versions WHERE version = ?",
            (version,),
        ).fetchone()
        require(row is not None, E_STATE_CORRUPT, "building version disappeared")
        if row["stage_phase"] not in {"nodes"}:
            return
        cursor = row["stage_cursor"]
        nodes = [
            index.nodes[node_id]
            for node_id in sorted(index.nodes)
            if cursor is None or node_id > cursor
        ]
        for batch_index, start in enumerate(range(0, len(nodes), NODE_BATCH_SIZE), 1):
            batch = nodes[start : start + NODE_BATCH_SIZE]
            connection.execute("BEGIN IMMEDIATE")
            try:
                for node in batch:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO index_nodes(
                            version, node_id, label, weight, written_at_ms,
                            tombstone, generation, source_format,
                            source_snapshot_version, source_logical_clock_ms,
                            source_shard_generation, source_priority,
                            decision_reason, candidates_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version,
                            node.node_id,
                            node.label,
                            node.weight,
                            node.written_at_ms,
                            1 if node.tombstone else 0,
                            node.generation,
                            node.source.format,
                            node.source.snapshot_version,
                            node.source.logical_clock_ms,
                            node.source.shard_generation,
                            node.source.format_priority,
                            node.decision_reason,
                            canonical_json(list(node.candidates)),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE index_versions SET stage_cursor = ?
                    WHERE version = ?
                    """,
                    (batch[-1].node_id, version),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            if batch_index == 1:
                maybe_crash("replace_after_stage_nodes")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE index_versions
                SET stage_phase = 'edges', stage_cursor = NULL
                WHERE version = ?
                """,
                (version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _stage_edges(
        self, connection: sqlite3.Connection, version: int, index: GraphIndex
    ) -> None:
        row = connection.execute(
            "SELECT stage_phase, stage_cursor FROM index_versions WHERE version = ?",
            (version,),
        ).fetchone()
        require(row is not None, E_STATE_CORRUPT, "building version disappeared")
        if row["stage_phase"] not in {"edges"}:
            return
        cursor_raw = row["stage_cursor"]
        cursor = tuple(json.loads(cursor_raw)) if cursor_raw else None
        edges: list[UnifiedEdge] = []
        for from_id in sorted(index.adjacency):
            for edge in index.adjacency[from_id]:
                key = (edge.from_id, edge.to_id, edge.label)
                if cursor is None or key > cursor:
                    edges.append(edge)
        edges.sort(key=lambda edge: (edge.from_id, edge.to_id, edge.label))
        for batch_index, start in enumerate(range(0, len(edges), EDGE_BATCH_SIZE), 1):
            batch = edges[start : start + EDGE_BATCH_SIZE]
            connection.execute("BEGIN IMMEDIATE")
            try:
                for edge in batch:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO index_edges(
                            version, from_id, to_id, label, weight,
                            source_format, source_snapshot_version,
                            source_logical_clock_ms, source_shard_generation,
                            source_priority, decision_reason, sources_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version,
                            edge.from_id,
                            edge.to_id,
                            edge.label,
                            edge.weight,
                            edge.source.format,
                            edge.source.snapshot_version,
                            edge.source.logical_clock_ms,
                            edge.source.shard_generation,
                            edge.source.format_priority,
                            edge.decision_reason,
                            canonical_json(list(edge.sources)),
                        ),
                    )
                last = batch[-1]
                connection.execute(
                    """
                    UPDATE index_versions SET stage_cursor = ?
                    WHERE version = ?
                    """,
                    (canonical_json([last.from_id, last.to_id, last.label]), version),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            if batch_index == 1:
                maybe_crash("replace_after_stage_edges")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE index_versions
                SET stage_phase = 'audit', stage_cursor = NULL
                WHERE version = ?
                """,
                (version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _finalize(
        self,
        connection: sqlite3.Connection,
        version: int,
        package: LoadedPackage,
    ) -> ReplaceOutcome:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM index_versions WHERE version = ?", (version,)
            ).fetchone()
            require(row is not None, E_STATE_CORRUPT, "building version disappeared")
            require(
                row["status"] == "building",
                E_STATE_CORRUPT,
                "version to finalize is not building",
                version=version,
                status=row["status"],
            )
            counts = connection.execute(
                """
                SELECT COUNT(*) AS nodes,
                       COALESCE(SUM(tombstone), 0) AS tombstones
                FROM index_nodes WHERE version = ?
                """,
                (version,),
            ).fetchone()
            edge_count = connection.execute(
                "SELECT COUNT(*) AS count FROM index_edges WHERE version = ?",
                (version,),
            ).fetchone()["count"]
            require(
                int(counts["nodes"]) == package.index.node_count,
                E_STATE_CORRUPT,
                "staged node count mismatch",
                staged=int(counts["nodes"]),
                expected=package.index.node_count,
            )
            require(
                int(counts["tombstones"]) == package.index.tombstone_count,
                E_STATE_CORRUPT,
                "staged tombstone count mismatch",
                staged=int(counts["tombstones"]),
                expected=package.index.tombstone_count,
            )
            require(
                int(edge_count) == package.index.edge_count,
                E_STATE_CORRUPT,
                "staged edge count mismatch",
                staged=int(edge_count),
                expected=package.index.edge_count,
            )
            observed_digest = digest_edges(
                (
                    row["from_id"],
                    row["to_id"],
                    row["label"],
                    float(row["weight"]),
                )
                for row in connection.execute(
                    """
                    SELECT from_id, to_id, label, weight
                    FROM index_edges WHERE version = ?
                    """,
                    (version,),
                )
            )
            require(
                observed_digest == package.index.adjacency_digest(),
                E_STATE_CORRUPT,
                "staged adjacency digest mismatch",
                staged=observed_digest,
                expected=package.index.adjacency_digest(),
            )

            connection.execute("DELETE FROM merge_decisions WHERE version = ?", (version,))
            connection.execute(
                "DELETE FROM migration_decisions WHERE version = ?", (version,)
            )
            decisions = list(package.index.audit.node_decisions) + list(
                package.index.audit.edge_decisions
            )
            for sequence, decision in enumerate(decisions):
                if decision["kind"] == "node":
                    target_key = decision["node_id"]
                    winner = decision.get("winner_source")
                else:
                    target_key = canonical_json(decision["edge"])
                    winner = decision.get("winner")
                connection.execute(
                    """
                    INSERT INTO merge_decisions(
                        version, seq, kind, target_key, reason,
                        winner_json, candidates_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        sequence,
                        decision["kind"],
                        target_key,
                        decision["reason"],
                        canonical_json(winner),
                        canonical_json(decision["candidates"]),
                    ),
                )
            for migration in sorted(
                package.index.audit.migrations, key=lambda item: item["node_id"]
            ):
                connection.execute(
                    """
                    INSERT INTO migration_decisions(version, node_id, decision_json)
                    VALUES (?, ?, ?)
                    """,
                    (version, migration["node_id"], canonical_json(migration)),
                )
            clock_now = self._advance_clock(connection)
            connection.execute(
                """
                UPDATE index_versions
                SET status = 'committed', stage_phase = 'done',
                    stage_cursor = NULL, node_count = ?,
                    edge_count = ?, tombstone_count = ?,
                    adjacency_digest = ?, committed_clock_ms = ?
                WHERE version = ?
                """,
                (
                    int(counts["nodes"]),
                    int(edge_count),
                    int(counts["tombstones"]),
                    observed_digest,
                    clock_now,
                    version,
                ),
            )
            connection.execute(
                """
                INSERT INTO current_index(id, version, updated_clock_ms)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    updated_clock_ms = excluded.updated_clock_ms
                """,
                (version, clock_now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return ReplaceOutcome(
            classification=row["classification"],
            idempotent=False,
            index_version=version,
            package_id=package.package_id,
            package_version=package.package_version,
            node_count=package.index.node_count,
            edge_count=package.index.edge_count,
            tombstone_count=package.index.tombstone_count,
            adjacency_digest=observed_digest,
            decision_reason=row["decision_reason"],
        )

    def classify_package(
        self,
        package: LoadedPackage,
        *,
        downgrade_policy: str = "reject",
    ) -> dict[str, Any]:
        """Read-only classification used by the merge preview command."""
        if not self.exists:
            return {
                "classification": "initial_load",
                "idempotent": False,
                "reason": "no_current_index",
            }
        with self.connection(read_only=True) as connection:
            try:
                current = self._current_row(connection)
            except sqlite3.Error:
                current = None
            existing = connection.execute(
                """
                SELECT * FROM index_versions
                WHERE package_id = ? AND package_version = ?
                """,
                (package.package_id, package.package_version),
            ).fetchone()
            if existing is not None:
                if (
                    existing["manifest_sha256"] != package.manifest_sha256
                    and not self._existing_content_matches(existing, package)
                ):
                    raise GraphStoreError(
                        E_VERSION_CONFLICT,
                        "same package version has different content",
                        package_id=package.package_id,
                        package_version=package.package_version,
                    )
                if existing["status"] != "committed":
                    return {
                        "classification": "resume_build",
                        "idempotent": False,
                        "index_version": int(existing["version"]),
                        "reason": "building_version_matches_manifest",
                    }
                require(
                    current is not None,
                    E_STATE_CORRUPT,
                    "committed versions exist but current index is missing",
                )
                if int(existing["version"]) == int(current["version"]):
                    return {
                        "classification": "no_op",
                        "idempotent": True,
                        "index_version": int(existing["version"]),
                        "reason": "already_current_identical_manifest",
                    }
                if package.package_version < int(
                    current["package_version"]
                ) and downgrade_policy == "reject":
                    raise GraphStoreError(
                        E_VERSION_ROLLBACK,
                        "historical package version would move current pointer backwards",
                        current_package_version=int(current["package_version"]),
                        incoming_package_version=package.package_version,
                        policy=downgrade_policy,
                    )
                if package.package_version == int(current["package_version"]):
                    raise GraphStoreError(
                        E_VERSION_CONFLICT,
                        "same version belongs to a different committed snapshot",
                        current_package_id=current["package_id"],
                        incoming_package_id=package.package_id,
                        package_version=package.package_version,
                    )
                clock_floor = int(
                    current["logical_clock_ms"]
                ) - self.clock.max_rollback_ms
                if (
                    package.logical_clock_ms < clock_floor
                    and downgrade_policy == "reject"
                ):
                    raise GraphStoreError(
                        E_CLOCK_ROLLBACK,
                        "historical package logical clock would move current pointer backwards",
                        current_logical_clock_ms=int(current["logical_clock_ms"]),
                        incoming_logical_clock_ms=package.logical_clock_ms,
                        max_rollback_ms=self.clock.max_rollback_ms,
                        policy=downgrade_policy,
                    )
                return {
                    "classification": "downgrade_restore",
                    "idempotent": False,
                    "index_version": int(existing["version"]),
                    "reason": "pointer_would_restore_previously_committed_version",
                }
            classification, reason = self._classify(
                package, current, downgrade_policy
            )
            return {
                "classification": classification,
                "idempotent": classification == "no_op",
                "reason": reason,
            }

    def load_index(self, version: int | None = None) -> GraphIndex:
        require(self.exists, E_INDEX_NOT_FOUND, "graph store does not exist")
        with self.connection(read_only=True) as connection:
            if version is None:
                current = self._current_row(connection)
                require(
                    current is not None,
                    E_INDEX_NOT_FOUND,
                    "no committed index version is available",
                )
                version = int(current["version"])
            version_row = connection.execute(
                "SELECT * FROM index_versions WHERE version = ?", (version,)
            ).fetchone()
            require(
                version_row is not None,
                E_INDEX_NOT_FOUND,
                "index version does not exist",
                version=version,
            )
            require(
                version_row["status"] == "committed",
                E_STATE_CORRUPT,
                "index version is not committed",
                version=version,
                status=version_row["status"],
            )
            nodes: dict[str, UnifiedNode] = {}
            for row in connection.execute(
                "SELECT * FROM index_nodes WHERE version = ? ORDER BY node_id",
                (version,),
            ):
                source = SourceVersion(
                    format=row["source_format"],
                    snapshot_version=int(row["source_snapshot_version"]),
                    logical_clock_ms=int(row["source_logical_clock_ms"]),
                    shard_generation=int(row["source_shard_generation"]),
                    format_priority=int(row["source_priority"]),
                )
                nodes[row["node_id"]] = UnifiedNode(
                    node_id=row["node_id"],
                    label=row["label"],
                    weight=float(row["weight"]),
                    written_at_ms=int(row["written_at_ms"]),
                    tombstone=bool(row["tombstone"]),
                    generation=int(row["generation"]),
                    source=source,
                    decision_reason=row["decision_reason"],
                    candidates=tuple(
                        _parse_json_field(
                            row["candidates_json"],
                            code=E_STATE_CORRUPT,
                            message="invalid persisted node candidate JSON",
                            node_id=row["node_id"],
                            version=version,
                        )
                    ),
                )
            adjacency_lists: dict[str, list[UnifiedEdge]] = {}
            for row in connection.execute(
                """
                SELECT * FROM index_edges
                WHERE version = ?
                ORDER BY from_id, to_id, label
                """,
                (version,),
            ):
                source = SourceVersion(
                    format=row["source_format"],
                    snapshot_version=int(row["source_snapshot_version"]),
                    logical_clock_ms=int(row["source_logical_clock_ms"]),
                    shard_generation=int(row["source_shard_generation"]),
                    format_priority=int(row["source_priority"]),
                )
                edge = UnifiedEdge(
                    from_id=row["from_id"],
                    to_id=row["to_id"],
                    label=row["label"],
                    weight=float(row["weight"]),
                    source=source,
                    decision_reason=row["decision_reason"],
                    sources=tuple(
                        _parse_json_field(
                            row["sources_json"],
                            code=E_STATE_CORRUPT,
                            message="invalid persisted edge source JSON",
                            from_id=row["from_id"],
                            to_id=row["to_id"],
                            label=row["label"],
                            version=version,
                        )
                    ),
                )
                adjacency_lists.setdefault(edge.from_id, []).append(edge)
            adjacency = {
                from_id: tuple(sorted(edges, key=lambda edge: edge.sort_key()))
                for from_id, edges in sorted(adjacency_lists.items())
            }
            labels, by_label = build_label_index(nodes)
            index = GraphIndex(
                nodes=nodes,
                adjacency=adjacency,
                labels=labels,
                nodes_by_label=by_label,
                audit=MergeAudit(),
                index_version=version,
            )
            require(
                index.node_count == int(version_row["node_count"]),
                E_STATE_CORRUPT,
                "persisted node count does not match index",
                persisted=int(version_row["node_count"]),
                observed=index.node_count,
            )
            require(
                index.edge_count == int(version_row["edge_count"]),
                E_STATE_CORRUPT,
                "persisted edge count does not match index",
                persisted=int(version_row["edge_count"]),
                observed=index.edge_count,
            )
            require(
                index.tombstone_count == int(version_row["tombstone_count"]),
                E_STATE_CORRUPT,
                "persisted tombstone count does not match index",
                persisted=int(version_row["tombstone_count"]),
                observed=index.tombstone_count,
            )
            observed_digest = index.adjacency_digest()
            require(
                observed_digest == version_row["adjacency_digest"],
                E_STATE_CORRUPT,
                "persisted adjacency digest does not match index",
                persisted=version_row["adjacency_digest"],
                observed=observed_digest,
            )
            return index

    @staticmethod
    def batch_content_digest(index_version: int, query_digest: str) -> str:
        return sha256_object(
            {"index_version": index_version, "query_digest": query_digest}
        )

    def status(self) -> dict[str, Any]:
        require(self.exists, E_INDEX_NOT_FOUND, "graph store does not exist")
        with self.connection(read_only=True) as connection:
            current = self._current_row(connection)
            clock = connection.execute(
                "SELECT now_ms FROM clock_state WHERE id = 1"
            ).fetchone()
            pending = [
                {
                    "classification": row["classification"],
                    "package_id": row["package_id"],
                    "package_version": int(row["package_version"]),
                    "stage_phase": row["stage_phase"],
                    "version": int(row["version"]),
                }
                for row in connection.execute(
                    """
                    SELECT version, package_id, package_version, classification,
                           stage_phase
                    FROM index_versions WHERE status = 'building'
                    ORDER BY version
                    """
                )
            ]
            watermark = connection.execute(
                """
                SELECT COUNT(*) AS batch_count,
                       MAX(committed_clock_ms) AS latest_clock,
                       MAX(batch_id) AS latest_batch_id
                FROM traverse_watermarks
                """
            ).fetchone()
            result: dict[str, Any] = {
                "committed_traversals": {
                    "batch_count": int(watermark["batch_count"]),
                    "latest_batch_id": watermark["latest_batch_id"],
                    "latest_committed_clock_ms": watermark["latest_clock"],
                },
                "current_index": None,
                "logical_clock_ms": None if clock is None else int(clock["now_ms"]),
                "pending_builds": pending,
                "schema_version": SCHEMA_VERSION,
            }
            if current is not None:
                result["current_index"] = {
                    "adjacency_digest": current["adjacency_digest"],
                    "classification": current["classification"],
                    "edge_count": int(current["edge_count"]),
                    "index_version": int(current["version"]),
                    "logical_clock_ms": int(current["logical_clock_ms"]),
                    "node_count": int(current["node_count"]),
                    "package_id": current["package_id"],
                    "package_version": int(current["package_version"]),
                    "tombstone_count": int(current["tombstone_count"]),
                }
            return result

    def verify(self, batch_id: str | None = None) -> dict[str, Any]:
        require(self.exists, E_INDEX_NOT_FOUND, "graph store does not exist")
        with self.connection(read_only=True) as connection:
            current = self._current_row(connection)
            require(
                current is not None,
                E_INDEX_NOT_FOUND,
                "no committed index is available for verification",
            )
            version = int(current["version"])
            nodes = connection.execute(
                "SELECT COUNT(*) AS count FROM index_nodes WHERE version = ?",
                (version,),
            ).fetchone()["count"]
            edges = connection.execute(
                "SELECT COUNT(*) AS count FROM index_edges WHERE version = ?",
                (version,),
            ).fetchone()["count"]
            tombstones = connection.execute(
                """
                SELECT COUNT(*) AS count FROM index_nodes
                WHERE version = ? AND tombstone = 1
                """,
                (version,),
            ).fetchone()["count"]
            observed_digest = digest_edges(
                (
                    row["from_id"],
                    row["to_id"],
                    row["label"],
                    float(row["weight"]),
                )
                for row in connection.execute(
                    """
                    SELECT from_id, to_id, label, weight
                    FROM index_edges WHERE version = ?
                    """,
                    (version,),
                )
            )
            require(
                int(nodes) == int(current["node_count"]),
                E_STATE_CORRUPT,
                "index node count mismatch",
            )
            require(
                int(edges) == int(current["edge_count"]),
                E_STATE_CORRUPT,
                "index edge count mismatch",
            )
            require(
                int(tombstones) == int(current["tombstone_count"]),
                E_STATE_CORRUPT,
                "index tombstone count mismatch",
            )
            require(
                observed_digest == current["adjacency_digest"],
                E_STATE_CORRUPT,
                "index adjacency digest mismatch",
                stored=current["adjacency_digest"],
                observed=observed_digest,
            )
            summary = _parse_json_field(
                current["package_summary_json"],
                code=E_STATE_CORRUPT,
                message="invalid package summary JSON",
                version=version,
            )
            merge_decision_count = connection.execute(
                "SELECT COUNT(*) AS count FROM merge_decisions WHERE version = ?",
                (version,),
            ).fetchone()["count"]
            require(
                int(merge_decision_count) == int(nodes) + int(edges),
                E_STATE_CORRUPT,
                "merge decision count mismatch",
                expected=int(nodes) + int(edges),
                observed=int(merge_decision_count),
            )
            migration_count = connection.execute(
                "SELECT COUNT(*) AS count FROM migration_decisions WHERE version = ?",
                (version,),
            ).fetchone()["count"]
            require(
                int(migration_count) == int(summary.get("migration_count", -1)),
                E_STATE_CORRUPT,
                "migration decision count mismatch",
                expected=summary.get("migration_count"),
                observed=int(migration_count),
            )
            require(
                len(
                    _parse_json_field(
                        current["decision_json"],
                        code=E_STATE_CORRUPT,
                        message="invalid package decision JSON",
                        version=version,
                    )
                )
                == int(nodes) + int(edges)
                and len(
                    _parse_json_field(
                        current["migration_json"],
                        code=E_STATE_CORRUPT,
                        message="invalid migration JSON",
                        version=version,
                    )
                )
                == int(migration_count),
                E_STATE_CORRUPT,
                "persisted audit JSON does not match decision tables",
            )
            pending_builds = [
                {
                    "classification": row["classification"],
                    "package_id": row["package_id"],
                    "package_version": int(row["package_version"]),
                    "stage_phase": row["stage_phase"],
                    "version": int(row["version"]),
                }
                for row in connection.execute(
                    """
                    SELECT version, package_id, package_version, classification,
                           stage_phase
                    FROM index_versions WHERE status = 'building'
                    ORDER BY version
                    """
                )
            ]

            if batch_id is not None:
                batch_rows = connection.execute(
                    "SELECT * FROM traverse_batches WHERE batch_id = ?", (batch_id,)
                ).fetchall()
                require(
                    bool(batch_rows),
                    E_INDEX_NOT_FOUND,
                    "traversal batch does not exist",
                    batch_id=batch_id,
                )
            else:
                batch_rows = connection.execute(
                    "SELECT * FROM traverse_batches ORDER BY batch_id"
                ).fetchall()
            checked_batches = []
            for batch in batch_rows:
                batch_index = connection.execute(
                    "SELECT status FROM index_versions WHERE version = ?",
                    (batch["index_version"],),
                ).fetchone()
                require(
                    batch_index is not None
                    and batch_index["status"] == "committed",
                    E_STATE_CORRUPT,
                    "traversal batch references a non-committed index version",
                    batch_id=batch["batch_id"],
                    index_version=int(batch["index_version"]),
                )
                content_digest = self.batch_content_digest(
                    int(batch["index_version"]), batch["query_digest"]
                )
                require(
                    content_digest == batch["batch_content_digest"],
                    E_BATCH_DIGEST_MISMATCH,
                    "traversal batch content digest mismatch",
                    batch_id=batch["batch_id"],
                    stored=batch["batch_content_digest"],
                    observed=content_digest,
                )
                query_digest = sha256_object(
                    _parse_json_field(
                        batch["query_json"],
                        code=E_BATCH_DIGEST_MISMATCH,
                        message="traversal query JSON is invalid",
                        batch_id=batch["batch_id"],
                    )
                )
                require(
                    query_digest == batch["query_digest"],
                    E_BATCH_DIGEST_MISMATCH,
                    "traversal query digest mismatch",
                    batch_id=batch["batch_id"],
                    stored=batch["query_digest"],
                    observed=query_digest,
                )
                if batch["status"] == "committed":
                    result = _parse_json_field(
                        batch["result_json"],
                        code=E_BATCH_DIGEST_MISMATCH,
                        message="traversal result JSON is invalid",
                        batch_id=batch["batch_id"],
                    )
                    result_digest = sha256_object(result)
                    require(
                        result_digest == batch["result_digest"],
                        E_BATCH_DIGEST_MISMATCH,
                        "traversal result digest mismatch",
                        batch_id=batch["batch_id"],
                        stored=batch["result_digest"],
                        observed=result_digest,
                    )
                    require(
                        _parse_json_field(
                            batch["stats_json"],
                            code=E_BATCH_DIGEST_MISMATCH,
                            message="traversal stats JSON is invalid",
                            batch_id=batch["batch_id"],
                        )
                        == result["stats"],
                        E_BATCH_DIGEST_MISMATCH,
                        "traversal stats digest mismatch",
                        batch_id=batch["batch_id"],
                    )
                    watermark = connection.execute(
                        """
                        SELECT * FROM traverse_watermarks WHERE batch_id = ?
                        """,
                        (batch["batch_id"],),
                    ).fetchone()
                    require(
                        watermark is not None,
                        E_STATE_CORRUPT,
                        "committed traversal has no watermark",
                        batch_id=batch["batch_id"],
                    )
                    require(
                        watermark["index_version"] == batch["index_version"]
                        and watermark["result_digest"] == batch["result_digest"]
                        and watermark["batch_content_digest"]
                        == batch["batch_content_digest"],
                        E_STATE_CORRUPT,
                        "traversal watermark does not match batch",
                        batch_id=batch["batch_id"],
                    )
                checked_batches.append(
                    {
                        "batch_id": batch["batch_id"],
                        "index_version": int(batch["index_version"]),
                        "status": batch["status"],
                    }
                )
            return {
                "batches": checked_batches,
                "current_index": {
                    "adjacency_digest": observed_digest,
                    "edge_count": int(edges),
                    "index_version": version,
                    "node_count": int(nodes),
                    "tombstone_count": int(tombstones),
                },
                "ok": True,
                "pending_builds": pending_builds,
            }

