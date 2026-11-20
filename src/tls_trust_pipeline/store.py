from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import sha256_json
from .closure import apply_incremental_closure
from .errors import (
    E_BAD_REQUEST,
    E_BAD_SNAPSHOT,
    E_CLOCK_ROLLBACK,
    E_PATCH_CONFLICT,
    E_SNAPSHOT_CONFLICT,
    E_STATE,
    E_VERSION_ROLLBACK,
    PipelineError,
)
from .locks import FileLock
from .models import Anchor, Snapshot

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_version TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    source_digest TEXT NOT NULL UNIQUE,
    parent_version TEXT,
    patch_digest TEXT UNIQUE,
    patch_id TEXT,
    effective_clock_ms INTEGER,
    anchor_count INTEGER NOT NULL,
    job_id TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_anchors (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    cert_file TEXT NOT NULL,
    not_before_ms INTEGER NOT NULL,
    not_after_ms INTEGER NOT NULL,
    revoked INTEGER NOT NULL CHECK (revoked IN (0, 1)),
    can_issue_intermediates INTEGER NOT NULL CHECK (can_issue_intermediates IN (0, 1)),
    trust_generation INTEGER NOT NULL,
    conversion_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, alias),
    UNIQUE (snapshot_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS chains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    generation INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    purpose TEXT NOT NULL,
    leaf_file TEXT NOT NULL,
    leaf_fingerprint TEXT NOT NULL,
    request_clock_ms INTEGER NOT NULL,
    require_revocation INTEGER NOT NULL CHECK (require_revocation IN (0, 1)),
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    anchor_alias TEXT,
    anchor_fingerprint TEXT,
    chain_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'stale')),
    reuse_source_id INTEGER,
    cache_origin TEXT NOT NULL,
    job_id TEXT,
    UNIQUE (hostname, purpose, generation)
);
CREATE TABLE IF NOT EXISTS revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    generation INTEGER NOT NULL,
    chain_id INTEGER,
    cert_fp TEXT NOT NULL,
    serial_hex TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT,
    evidence_sha256 TEXT,
    record_sha256 TEXT,
    checked_clock_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'stale')),
    reuse_source_id INTEGER,
    job_id TEXT,
    UNIQUE (cert_fp, generation)
);
CREATE TABLE IF NOT EXISTS hostname_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leaf_fingerprint TEXT NOT NULL,
    hostname TEXT NOT NULL,
    purpose TEXT NOT NULL,
    matched INTEGER NOT NULL CHECK (matched IN (0, 1)),
    rule TEXT NOT NULL,
    matched_value TEXT,
    san_json TEXT NOT NULL,
    reason TEXT,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'stale')),
    job_id TEXT,
    UNIQUE (leaf_fingerprint, hostname, purpose)
);
CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    to_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    from_generation INTEGER NOT NULL,
    to_generation INTEGER NOT NULL,
    conclusion_kind TEXT NOT NULL,
    conclusion_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('invalidate', 'reuse')),
    reason TEXT NOT NULL,
    source_row_id INTEGER,
    target_row_id INTEGER,
    source_payload_sha256 TEXT,
    target_payload_sha256 TEXT,
    job_id TEXT,
    UNIQUE (to_snapshot_id, conclusion_kind, conclusion_key, action)
);
CREATE TABLE IF NOT EXISTS patches (
    patch_digest TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL UNIQUE,
    base_version TEXT NOT NULL,
    target_version TEXT NOT NULL UNIQUE,
    target_generation INTEGER NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    operations_json TEXT NOT NULL,
    job_id TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'aborted')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    created_seq INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL
);
"""


class Store:
    def __init__(
        self,
        database: Path,
        lock_path: Path | None = None,
        lock_timeout_seconds: float = 20.0,
    ) -> None:
        self.database = database
        self.lock_path = lock_path or database.with_suffix(database.suffix + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def _connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            connection = self._connect()
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                        (SCHEMA_VERSION,),
                    )
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES ('clock_precision_ms', '1')"
                    )
                elif current["value"] != SCHEMA_VERSION:
                    raise PipelineError(
                        E_STATE,
                        "state schema version is unsupported",
                        {"found": current["value"], "expected": SCHEMA_VERSION},
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = True,
        require_existing: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        if require_existing and not self.database.exists():
            raise PipelineError(E_STATE, "state database has not been initialized", {"path": str(self.database)})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def read(self) -> Iterator[sqlite3.Connection]:
        return self.transaction(immediate=False)

    def _next_seq(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'sequence'"
        ).fetchone()
        next_value = 1 if row is None else int(row["value"]) + 1
        if row is None:
            connection.execute("INSERT INTO meta(key, value) VALUES ('sequence', ?)", (str(next_value),))
        else:
            connection.execute("UPDATE meta SET value = ? WHERE key = 'sequence'", (str(next_value),))
        return next_value

    def _meta_int(self, connection: sqlite3.Connection, key: str) -> int | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else int(row["value"])

    def check_clock(
        self,
        connection: sqlite3.Connection,
        logical_clock_ms: int,
        tolerance_ms: int,
    ) -> None:
        if not isinstance(logical_clock_ms, int) or isinstance(logical_clock_ms, bool):
            raise PipelineError(
                E_CLOCK_ROLLBACK, "logical clock must be an integer millisecond value"
            )
        if not isinstance(tolerance_ms, int) or isinstance(tolerance_ms, bool) or tolerance_ms < 0:
            raise PipelineError(E_BAD_REQUEST, "clock tolerance must be a non-negative integer")
        watermark = self._meta_int(connection, "logical_clock_watermark_ms")
        if watermark is not None and logical_clock_ms < watermark - tolerance_ms:
            raise PipelineError(
                E_CLOCK_ROLLBACK,
                "logical clock moved backwards beyond tolerance",
                {
                    "watermark_ms": watermark,
                    "requested_ms": logical_clock_ms,
                    "tolerance_ms": tolerance_ms,
                },
            )

    def advance_clock(
        self,
        connection: sqlite3.Connection,
        logical_clock_ms: int,
        tolerance_ms: int,
    ) -> None:
        self.check_clock(connection, logical_clock_ms, tolerance_ms)
        watermark = self._meta_int(connection, "logical_clock_watermark_ms")
        if watermark is None:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('logical_clock_watermark_ms', ?)",
                (str(logical_clock_ms),),
            )
        elif logical_clock_ms > watermark:
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'logical_clock_watermark_ms'",
                (str(logical_clock_ms),),
            )

    def current_snapshot(self, connection: sqlite3.Connection) -> Snapshot | None:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'current_snapshot_version'"
        ).fetchone()
        if row is None:
            return None
        return self.snapshot_by_version(connection, row["value"])

    def snapshot_by_version(
        self,
        connection: sqlite3.Connection,
        version: str,
    ) -> Snapshot | None:
        row = connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        anchors = []
        for anchor_row in connection.execute(
            "SELECT * FROM snapshot_anchors WHERE snapshot_id = ? ORDER BY alias",
            (row["id"],),
        ).fetchall():
            anchors.append(
                Anchor(
                    alias=anchor_row["alias"],
                    fingerprint=anchor_row["fingerprint"],
                    cert_file=anchor_row["cert_file"],
                    not_before_ms=anchor_row["not_before_ms"],
                    not_after_ms=anchor_row["not_after_ms"],
                    revoked=bool(anchor_row["revoked"]),
                    can_issue_intermediates=bool(anchor_row["can_issue_intermediates"]),
                    trust_generation=anchor_row["trust_generation"],
                    conversion=json.loads(anchor_row["conversion_json"]),
                )
            )
        return Snapshot(
            snapshot_version=row["snapshot_version"],
            generation=row["generation"],
            created_clock_ms=row["created_clock_ms"],
            source_digest=row["source_digest"],
            kind=row["kind"],
            anchors=tuple(anchors),
        )

    def snapshot_id(self, connection: sqlite3.Connection, version: str) -> int:
        row = connection.execute(
            "SELECT id FROM snapshots WHERE snapshot_version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise PipelineError(E_STATE, "snapshot version does not exist", {"version": version})
        return int(row["id"])

    def _stage_anchors(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        anchors: tuple[Anchor, ...],
    ) -> None:
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS snapshot_anchors_next (
                alias TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                cert_file TEXT NOT NULL,
                not_before_ms INTEGER NOT NULL,
                not_after_ms INTEGER NOT NULL,
                revoked INTEGER NOT NULL,
                can_issue_intermediates INTEGER NOT NULL,
                trust_generation INTEGER NOT NULL,
                conversion_json TEXT NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM temp.snapshot_anchors_next")
        for anchor in anchors:
            connection.execute(
                """
                INSERT INTO temp.snapshot_anchors_next VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor.alias,
                    anchor.fingerprint,
                    anchor.cert_file,
                    anchor.not_before_ms,
                    anchor.not_after_ms,
                    int(anchor.revoked),
                    int(anchor.can_issue_intermediates),
                    anchor.trust_generation,
                    json.dumps(anchor.conversion, sort_keys=True, separators=(",", ":")),
                ),
            )
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM temp.snapshot_anchors_next"
        ).fetchone()["count"]
        if count != len(anchors):
            raise PipelineError(E_STATE, "temporary anchor staging count mismatch")
        connection.execute(
            """
            INSERT INTO snapshot_anchors(
                snapshot_id, alias, fingerprint, cert_file, not_before_ms, not_after_ms,
                revoked, can_issue_intermediates, trust_generation, conversion_json
            )
            SELECT ?, alias, fingerprint, cert_file, not_before_ms, not_after_ms,
                   revoked, can_issue_intermediates, trust_generation, conversion_json
            FROM temp.snapshot_anchors_next ORDER BY alias
            """,
            (snapshot_id,),
        )
        connection.execute("DROP TABLE temp.snapshot_anchors_next")

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        *,
        parent_version: str | None,
        patch_digest: str | None,
        patch_id: str | None,
        effective_clock_ms: int | None,
        job_id: str | None,
    ) -> int:
        try:
            cursor = connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_version, generation, kind, created_clock_ms, source_digest,
                    parent_version, patch_digest, patch_id, effective_clock_ms,
                    anchor_count, job_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_version,
                    snapshot.generation,
                    snapshot.kind,
                    snapshot.created_clock_ms,
                    snapshot.source_digest,
                    parent_version,
                    patch_digest,
                    patch_id,
                    effective_clock_ms,
                    len(snapshot.anchors),
                    job_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise PipelineError(
                E_SNAPSHOT_CONFLICT,
                "snapshot version, generation, or digest already exists",
                {"version": snapshot.snapshot_version},
            ) from exc
        snapshot_id = int(cursor.lastrowid)
        self._stage_anchors(connection, snapshot_id, snapshot.anchors)
        return snapshot_id

    def load_snapshots(
        self,
        snapshots: list[Snapshot],
        *,
        logical_clock_ms: int,
        tolerance_ms: int,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                self.check_clock(connection, logical_clock_ms, tolerance_ms)
                current = self.current_snapshot(connection)
                loaded: list[str] = []
                unchanged: list[str] = []
                highest = current
                for snapshot in sorted(snapshots, key=lambda item: item.generation):
                    existing_row = connection.execute(
                        "SELECT * FROM snapshots WHERE snapshot_version = ?",
                        (snapshot.snapshot_version,),
                    ).fetchone()
                    if existing_row is not None:
                        if existing_row["source_digest"] != snapshot.source_digest:
                            raise PipelineError(
                                E_SNAPSHOT_CONFLICT,
                                "snapshot version was previously loaded with different content",
                                {"version": snapshot.snapshot_version},
                            )
                        unchanged.append(snapshot.snapshot_version)
                        existing_snapshot = self.snapshot_by_version(
                            connection, snapshot.snapshot_version
                        )
                        if highest is None or (
                            existing_snapshot is not None
                            and existing_snapshot.generation > highest.generation
                        ):
                            highest = existing_snapshot
                        continue
                    digest_row = connection.execute(
                        "SELECT snapshot_version FROM snapshots WHERE source_digest = ?",
                        (snapshot.source_digest,),
                    ).fetchone()
                    if digest_row is not None:
                        raise PipelineError(
                            E_SNAPSHOT_CONFLICT,
                            "snapshot content already exists under another version",
                            {
                                "version": snapshot.snapshot_version,
                                "existing_version": digest_row["snapshot_version"],
                            },
                        )
                    if highest is not None:
                        if snapshot.generation <= highest.generation:
                            raise PipelineError(
                                E_VERSION_ROLLBACK,
                                "snapshot generation would move backwards or repeat",
                                {
                                    "current_generation": highest.generation,
                                    "incoming_generation": snapshot.generation,
                                },
                            )
                        if snapshot.generation != highest.generation + 1:
                            raise PipelineError(
                                E_BAD_SNAPSHOT,
                                "snapshot generation must increase by exactly one",
                                {
                                    "current_generation": highest.generation,
                                    "incoming_generation": snapshot.generation,
                                },
                            )
                    self._insert_snapshot(
                        connection,
                        snapshot,
                        parent_version=highest.snapshot_version if highest else None,
                        patch_digest=None,
                        patch_id=None,
                        effective_clock_ms=None,
                        job_id=None,
                    )
                    highest = snapshot
                    loaded.append(snapshot.snapshot_version)
                if highest is not None:
                    self._set_current(connection, highest)
                self.advance_clock(connection, logical_clock_ms, tolerance_ms)
                return {
                    "loaded": loaded,
                    "unchanged": unchanged,
                    "current_version": highest.snapshot_version if highest else None,
                    "current_generation": highest.generation if highest else None,
                }

    def _set_current(self, connection: sqlite3.Connection, snapshot: Snapshot) -> None:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'current_snapshot_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('current_snapshot_version', ?)",
                (snapshot.snapshot_version,),
            )
        else:
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'current_snapshot_version'",
                (snapshot.snapshot_version,),
            )

    def start_job(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        digest = sha256_json(payload)
        job_id = f"job-{digest[:32]}"
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is not None:
            if row["input_digest"] != digest:
                raise PipelineError(E_STATE, "job digest collision", {"job_id": job_id})
            if row["status"] == "aborted":
                seq = self._next_seq(connection)
                connection.execute(
                    """
                    UPDATE jobs SET status = 'running', result_json = NULL,
                        updated_seq = ? WHERE job_id = ?
                    """,
                    (seq, job_id),
                )
                return job_id, "running"
            return job_id, row["status"]
        seq = self._next_seq(connection)
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, kind, input_digest, status, payload_json, result_json,
                created_seq, updated_seq
            ) VALUES (?, ?, ?, 'running', ?, NULL, ?, ?)
            """,
            (
                job_id,
                kind,
                digest,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                seq,
                seq,
            ),
        )
        return job_id, "running"

    def complete_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            return
        seq = self._next_seq(connection)
        connection.execute(
            """
            UPDATE jobs
            SET status = 'completed', result_json = ?, updated_seq = ?
            WHERE job_id = ? AND status = 'running'
            """,
            (
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                seq,
                job_id,
            ),
        )

    def finish_job(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        logical_clock_ms: int,
        tolerance_ms: int,
    ) -> None:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                self.check_clock(connection, logical_clock_ms, tolerance_ms)
                self.complete_job(connection, job_id, result)

    def abort_job(self, job_id: str, reason: str) -> None:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None or row["status"] != "running":
                    return
                seq = self._next_seq(connection)
                connection.execute(
                    """
                    UPDATE jobs SET status = 'aborted',
                        result_json = ?, updated_seq = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (json.dumps({"reason": reason}, sort_keys=True), seq, job_id),
                )

    def begin_job(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        logical_clock_ms: int | None = None,
        tolerance_ms: int = 0,
    ) -> tuple[str, str]:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                if logical_clock_ms is not None:
                    self.check_clock(connection, logical_clock_ms, tolerance_ms)
                return self.start_job(connection, kind=kind, payload=payload)

    def apply_patch(
        self,
        *,
        patch: dict[str, Any],
        new_snapshot: Snapshot,
        patch_digest: str,
        logical_clock_ms: int,
        tolerance_ms: int,
        job_id: str,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                self.check_clock(connection, logical_clock_ms, tolerance_ms)
                existing_patch = connection.execute(
                    "SELECT * FROM patches WHERE patch_digest = ?",
                    (patch_digest,),
                ).fetchone()
                if existing_patch is not None:
                    existing_snapshot = self.snapshot_by_version(
                        connection, existing_patch["target_version"]
                    )
                    if existing_snapshot is None:
                        raise PipelineError(E_STATE, "applied patch has no target snapshot")
                    self.complete_job(
                        connection,
                        job_id,
                        {"status": "idempotent", "version": existing_snapshot.snapshot_version},
                    )
                    return {
                        "status": "idempotent",
                        "version": existing_snapshot.snapshot_version,
                        "generation": existing_snapshot.generation,
                    }
                patch_id_row = connection.execute(
                    "SELECT patch_digest FROM patches WHERE patch_id = ?",
                    (patch["patch_id"],),
                ).fetchone()
                if patch_id_row is not None:
                    raise PipelineError(
                        E_PATCH_CONFLICT,
                        "patch_id was already applied with different content",
                        {"patch_id": patch["patch_id"]},
                    )
                old_snapshot = self.current_snapshot(connection)
                if old_snapshot is None:
                    raise PipelineError(E_STATE, "cannot apply a patch without a current snapshot")
                if patch["base_version"] != old_snapshot.snapshot_version:
                    raise PipelineError(
                        E_PATCH_CONFLICT,
                        "patch base_version is not the current snapshot",
                        {
                            "base_version": patch["base_version"],
                            "current_version": old_snapshot.snapshot_version,
                        },
                    )
                if new_snapshot.generation <= old_snapshot.generation:
                    raise PipelineError(
                        E_VERSION_ROLLBACK,
                        "patch target generation would move backwards or repeat",
                        {
                            "current_generation": old_snapshot.generation,
                            "target_generation": new_snapshot.generation,
                        },
                    )
                if new_snapshot.generation != old_snapshot.generation + 1:
                    raise PipelineError(
                        E_VERSION_ROLLBACK,
                        "patch target generation must be current_generation + 1",
                        {
                            "current_generation": old_snapshot.generation,
                            "target_generation": new_snapshot.generation,
                        },
                    )
                if new_snapshot.snapshot_version == old_snapshot.snapshot_version:
                    raise PipelineError(E_PATCH_CONFLICT, "patch target version must be new")
                old_snapshot_id = self.snapshot_id(connection, old_snapshot.snapshot_version)
                connection.execute(
                    """
                    INSERT INTO patches(
                        patch_digest, patch_id, base_version, target_version,
                        target_generation, effective_clock_ms, operations_json, job_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        patch_digest,
                        patch["patch_id"],
                        patch["base_version"],
                        new_snapshot.snapshot_version,
                        new_snapshot.generation,
                        patch["effective_clock_ms"],
                        json.dumps(
                            patch["operations"], sort_keys=True, separators=(",", ":")
                        ),
                        job_id,
                    ),
                )
                new_snapshot_id = self._insert_snapshot(
                    connection,
                    new_snapshot,
                    parent_version=old_snapshot.snapshot_version,
                    patch_digest=patch_digest,
                    patch_id=patch["patch_id"],
                    effective_clock_ms=patch["effective_clock_ms"],
                    job_id=job_id,
                )
                closure = apply_incremental_closure(
                    connection,
                    old_snapshot=old_snapshot,
                    new_snapshot=new_snapshot,
                    old_snapshot_id=old_snapshot_id,
                    new_snapshot_id=new_snapshot_id,
                    job_id=job_id,
                )
                self._set_current(connection, new_snapshot)
                self.advance_clock(connection, logical_clock_ms, tolerance_ms)
                result = {
                    "status": "applied",
                    "version": new_snapshot.snapshot_version,
                    "generation": new_snapshot.generation,
                    "anchor_count": len(new_snapshot.anchors),
                    "closure": closure,
                }
                self.complete_job(connection, job_id, result)
                return result

    def chain_cache(
        self,
        connection: sqlite3.Connection,
        *,
        hostname: str,
        purpose: str,
        generation: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM chains
            WHERE hostname = ? AND purpose = ? AND generation = ? AND state = 'active'
            """,
            (hostname, purpose, generation),
        ).fetchone()

    def revocation_cache(
        self,
        connection: sqlite3.Connection,
        *,
        cert_fp: str,
        generation: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM revocations
            WHERE cert_fp = ? AND generation = ? AND state = 'active'
            """,
            (cert_fp, generation),
        ).fetchone()

    def hostname_cache(
        self,
        connection: sqlite3.Connection,
        *,
        leaf_fp: str,
        hostname: str,
        purpose: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM hostname_conclusions
            WHERE leaf_fingerprint = ? AND hostname = ? AND purpose = ? AND state = 'active'
            """,
            (leaf_fp, hostname, purpose),
        ).fetchone()

    def persist_chain_conclusion(
        self,
        *,
        job_id: str,
        expected_version: str,
        chain: dict[str, Any],
        hostname: dict[str, Any],
    ) -> int:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                current = self.current_snapshot(connection)
                if current is None or current.snapshot_version != expected_version:
                    raise PipelineError(
                        E_STATE,
                        "current snapshot changed while chain was being evaluated",
                        {
                            "expected_version": expected_version,
                            "current_version": current.snapshot_version if current else None,
                        },
                    )
                snapshot_id = self.snapshot_id(connection, current.snapshot_version)
                payload = {"chain": chain}
                payload_sha = sha256_json(payload)
                row = self.chain_cache(
                    connection,
                    hostname=chain["hostname"],
                    purpose=chain["purpose"],
                    generation=current.generation,
                )
                existing_payload = None if row is None else row["payload_sha256"]
                if row is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO chains(
                            snapshot_id, generation, hostname, purpose, leaf_file,
                            leaf_fingerprint, request_clock_ms, require_revocation,
                            status, reason_code, reason, anchor_alias, anchor_fingerprint,
                            chain_json, evidence_json, payload_sha256, state,
                            reuse_source_id, cache_origin, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'active', NULL, 'computed', ?)
                        """,
                        (
                            snapshot_id,
                            current.generation,
                            chain["hostname"],
                            chain["purpose"],
                            chain["leaf_file"],
                            chain["leaf_fingerprint"],
                            chain["logical_clock_ms"],
                            int(chain["require_revocation"]),
                            chain["status"],
                            chain["reason_code"],
                            chain["reason"],
                            chain["anchor_alias"],
                            chain["anchor_fingerprint"],
                            json.dumps(chain["chain"], sort_keys=True, separators=(",", ":")),
                            json.dumps(chain["evidence"], sort_keys=True, separators=(",", ":")),
                            payload_sha,
                            job_id,
                        ),
                    )
                    chain_id = int(cursor.lastrowid)
                else:
                    chain_id = int(row["id"])
                    if existing_payload != payload_sha:
                        connection.execute(
                            """
                            UPDATE chains SET
                                snapshot_id = ?, leaf_file = ?, leaf_fingerprint = ?,
                                request_clock_ms = ?, require_revocation = ?, status = ?,
                                reason_code = ?, reason = ?, anchor_alias = ?,
                                anchor_fingerprint = ?, chain_json = ?, evidence_json = ?,
                                payload_sha256 = ?, state = 'active', cache_origin = 'computed',
                                job_id = ?
                            WHERE id = ?
                            """,
                            (
                                snapshot_id,
                                chain["leaf_file"],
                                chain["leaf_fingerprint"],
                                chain["logical_clock_ms"],
                                int(chain["require_revocation"]),
                                chain["status"],
                                chain["reason_code"],
                                chain["reason"],
                                chain["anchor_alias"],
                                chain["anchor_fingerprint"],
                                json.dumps(chain["chain"], sort_keys=True, separators=(",", ":")),
                                json.dumps(chain["evidence"], sort_keys=True, separators=(",", ":")),
                                payload_sha,
                                job_id,
                                chain_id,
                            ),
                        )
                hostname_payload = {"hostname": hostname}
                hostname_payload_sha = sha256_json(hostname_payload)
                hostname_row = self.hostname_cache(
                    connection,
                    leaf_fp=chain["leaf_fingerprint"],
                    hostname=chain["hostname"],
                    purpose=chain["purpose"],
                )
                if hostname_row is None:
                    connection.execute(
                        """
                        INSERT INTO hostname_conclusions(
                            leaf_fingerprint, hostname, purpose, matched, rule,
                            matched_value, san_json, reason, payload_sha256, state, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            chain["leaf_fingerprint"],
                            chain["hostname"],
                            chain["purpose"],
                            int(bool(hostname.get("matched"))),
                            hostname.get("rule", "none"),
                            hostname.get("matched_value"),
                            json.dumps(
                                {"san_dns": hostname.get("san_dns", []), "san_ip": hostname.get("san_ip", [])},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            hostname.get("reason"),
                            hostname_payload_sha,
                            job_id,
                        ),
                    )
                result = {
                    "chain_id": chain_id,
                    "chain_payload_sha256": payload_sha,
                    "cache_action": "hit" if existing_payload == payload_sha else "computed",
                }
                self.complete_job(connection, job_id, result)
                return chain_id

    def persist_revocation(
        self,
        *,
        job_id: str,
        snapshot_version: str,
        chain_id: int,
        conclusions: list[dict[str, Any]],
    ) -> None:
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                current = self.current_snapshot(connection)
                if current is None or current.snapshot_version != snapshot_version:
                    raise PipelineError(E_STATE, "snapshot changed while revocation was evaluated")
                snapshot_id = self.snapshot_id(connection, snapshot_version)
                for conclusion in conclusions:
                    payload_sha = sha256_json(conclusion)
                    connection.execute(
                        """
                        INSERT INTO revocations(
                            snapshot_id, generation, chain_id, cert_fp, serial_hex,
                            status, reason, source, evidence_sha256, record_sha256,
                            checked_clock_ms, revoked_at_ms, payload_sha256, state,
                            reuse_source_id, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
                        ON CONFLICT(cert_fp, generation) DO UPDATE SET
                            snapshot_id = excluded.snapshot_id,
                            chain_id = excluded.chain_id,
                            serial_hex = excluded.serial_hex,
                            status = excluded.status,
                            reason = excluded.reason,
                            source = excluded.source,
                            evidence_sha256 = excluded.evidence_sha256,
                            record_sha256 = excluded.record_sha256,
                            checked_clock_ms = excluded.checked_clock_ms,
                            revoked_at_ms = excluded.revoked_at_ms,
                            payload_sha256 = excluded.payload_sha256,
                            state = 'active',
                            job_id = excluded.job_id
                        """,
                        (
                            snapshot_id,
                            current.generation,
                            chain_id,
                            conclusion["certificate_fingerprint"],
                            conclusion["serial_hex"],
                            conclusion["status"],
                            conclusion["reason"],
                            conclusion.get("source"),
                            conclusion.get("evidence_sha256"),
                            conclusion.get("record_sha256"),
                            conclusion["checked_clock_ms"],
                            conclusion.get("revoked_at_ms"),
                            payload_sha,
                            job_id,
                        ),
                    )
                self.complete_job(
                    connection,
                    job_id,
                    {"status": "completed", "revocations": len(conclusions)},
                )

    def persist_verification(
        self,
        *,
        job_id: str,
        expected_version: str,
        logical_clock_ms: int,
        tolerance_ms: int,
        chain: dict[str, Any],
        hostname: dict[str, Any],
        revocations: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit all conclusions and the job completion as one atomic unit."""

        with FileLock(self.lock_path, self.lock_timeout_seconds):
            with self.transaction() as connection:
                current = self.current_snapshot(connection)
                if current is None or current.snapshot_version != expected_version:
                    raise PipelineError(
                        E_STATE,
                        "current snapshot changed while verification was running",
                        {
                            "expected_version": expected_version,
                            "current_version": current.snapshot_version if current else None,
                        },
                    )
                self.check_clock(connection, logical_clock_ms, tolerance_ms)
                snapshot_id = self.snapshot_id(connection, current.snapshot_version)
                payload_sha = sha256_json(chain)
                existing = self.chain_cache(
                    connection,
                    hostname=chain["hostname"],
                    purpose=chain["purpose"],
                    generation=current.generation,
                )
                cache_action = "hit"
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO chains(
                            snapshot_id, generation, hostname, purpose, leaf_file,
                            leaf_fingerprint, request_clock_ms, require_revocation,
                            status, reason_code, reason, anchor_alias, anchor_fingerprint,
                            chain_json, evidence_json, payload_sha256, state,
                            reuse_source_id, cache_origin, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'active', NULL, 'computed', ?)
                        """,
                        (
                            snapshot_id,
                            current.generation,
                            chain["hostname"],
                            chain["purpose"],
                            chain["leaf_file"],
                            chain["leaf_fingerprint"],
                            chain["logical_clock_ms"],
                            int(chain["require_revocation"]),
                            chain["status"],
                            chain["reason_code"],
                            chain["reason"],
                            chain["anchor_alias"],
                            chain["anchor_fingerprint"],
                            json.dumps(
                                chain["chain"], sort_keys=True, separators=(",", ":")
                            ),
                            json.dumps(chain, sort_keys=True, separators=(",", ":")),
                            payload_sha,
                            job_id,
                        ),
                    )
                    chain_id = int(cursor.lastrowid)
                    cache_action = "computed"
                else:
                    chain_id = int(existing["id"])
                    if existing["payload_sha256"] != payload_sha:
                        connection.execute(
                            """
                            UPDATE chains SET
                                snapshot_id = ?, leaf_file = ?, leaf_fingerprint = ?,
                                request_clock_ms = ?, require_revocation = ?, status = ?,
                                reason_code = ?, reason = ?, anchor_alias = ?,
                                anchor_fingerprint = ?, chain_json = ?, evidence_json = ?,
                                payload_sha256 = ?, state = 'active', cache_origin = 'computed',
                                job_id = ?
                            WHERE id = ?
                            """,
                            (
                                snapshot_id,
                                chain["leaf_file"],
                                chain["leaf_fingerprint"],
                                chain["logical_clock_ms"],
                                int(chain["require_revocation"]),
                                chain["status"],
                                chain["reason_code"],
                                chain["reason"],
                                chain["anchor_alias"],
                                chain["anchor_fingerprint"],
                                json.dumps(
                                    chain["chain"], sort_keys=True, separators=(",", ":")
                                ),
                                json.dumps(chain, sort_keys=True, separators=(",", ":")),
                                payload_sha,
                                job_id,
                                chain_id,
                            ),
                        )
                        cache_action = "computed"
                hostname_sha = sha256_json(hostname)
                hostname_row = connection.execute(
                    """
                    SELECT * FROM hostname_conclusions
                    WHERE leaf_fingerprint = ? AND hostname = ? AND purpose = ?
                    """,
                    (chain["leaf_fingerprint"], chain["hostname"], chain["purpose"]),
                ).fetchone()
                if hostname_row is None:
                    connection.execute(
                        """
                        INSERT INTO hostname_conclusions(
                            leaf_fingerprint, hostname, purpose, matched, rule,
                            matched_value, san_json, reason, payload_sha256, state, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            chain["leaf_fingerprint"],
                            chain["hostname"],
                            chain["purpose"],
                            int(bool(hostname.get("matched"))),
                            hostname.get("rule", "none"),
                            hostname.get("matched_value"),
                            json.dumps(
                                hostname, sort_keys=True, separators=(",", ":")
                            ),
                            hostname.get("reason"),
                            hostname_sha,
                            job_id,
                        ),
                    )
                elif hostname_row["payload_sha256"] != hostname_sha:
                    connection.execute(
                        """
                        UPDATE hostname_conclusions SET
                            matched = ?, rule = ?, matched_value = ?, san_json = ?,
                            reason = ?, payload_sha256 = ?, state = 'active', job_id = ?
                        WHERE id = ?
                        """,
                        (
                            int(bool(hostname.get("matched"))),
                            hostname.get("rule", "none"),
                            hostname.get("matched_value"),
                            json.dumps(
                                hostname, sort_keys=True, separators=(",", ":")
                            ),
                            hostname.get("reason"),
                            hostname_sha,
                            job_id,
                            int(hostname_row["id"]),
                        ),
                    )
                cache_actions = {}
                for conclusion in revocations:
                    revocation_payload_sha = sha256_json(conclusion)
                    existing_revocation = self.revocation_cache(
                        connection,
                        cert_fp=conclusion["certificate_fingerprint"],
                        generation=current.generation,
                    )
                    if (
                        existing_revocation is not None
                        and existing_revocation["payload_sha256"] == revocation_payload_sha
                    ):
                        cache_actions[conclusion["certificate_fingerprint"]] = "hit"
                    else:
                        cache_actions[conclusion["certificate_fingerprint"]] = "computed"
                    connection.execute(
                        """
                        INSERT INTO revocations(
                            snapshot_id, generation, chain_id, cert_fp, serial_hex,
                            status, reason, source, evidence_sha256, record_sha256,
                            checked_clock_ms, revoked_at_ms, payload_sha256, state,
                            reuse_source_id, job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
                        ON CONFLICT(cert_fp, generation) DO UPDATE SET
                            snapshot_id = excluded.snapshot_id,
                            chain_id = excluded.chain_id,
                            serial_hex = excluded.serial_hex,
                            status = excluded.status,
                            reason = excluded.reason,
                            source = excluded.source,
                            evidence_sha256 = excluded.evidence_sha256,
                            record_sha256 = excluded.record_sha256,
                            checked_clock_ms = excluded.checked_clock_ms,
                            revoked_at_ms = excluded.revoked_at_ms,
                            payload_sha256 = excluded.payload_sha256,
                            state = 'active',
                            job_id = excluded.job_id
                        """,
                        (
                            snapshot_id,
                            current.generation,
                            chain_id,
                            conclusion["certificate_fingerprint"],
                            conclusion["serial_hex"],
                            conclusion["status"],
                            conclusion["reason"],
                            conclusion.get("source"),
                            conclusion.get("evidence_sha256"),
                            conclusion.get("record_sha256"),
                            conclusion["checked_clock_ms"],
                            conclusion.get("revoked_at_ms"),
                            revocation_payload_sha,
                            job_id,
                        ),
                    )
                job_result = dict(result)
                job_result.update(
                    {
                        "chain_id": chain_id,
                        "chain_payload_sha256": payload_sha,
                        "cache_action": cache_action,
                        "revocation_cache_actions": cache_actions,
                    }
                )
                self.complete_job(connection, job_id, job_result)
                self.advance_clock(connection, logical_clock_ms, tolerance_ms)
                return job_result

    def status(self) -> dict[str, Any]:
        with self.read() as connection:
            current = self.current_snapshot(connection)
            if current is None:
                return {
                    "initialized": True,
                    "current_version": None,
                    "current_generation": None,
                    "anchor_count": 0,
                    "counts": {},
                }
            counts = {}
            for table in ("chains", "revocations", "hostname_conclusions", "invalidations", "jobs"):
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
            active = {
                "chains": connection.execute(
                    "SELECT COUNT(*) AS count FROM chains WHERE generation = ? AND state='active'",
                    (current.generation,),
                ).fetchone()["count"],
                "revocations": connection.execute(
                    "SELECT COUNT(*) AS count FROM revocations WHERE generation = ? AND state='active'",
                    (current.generation,),
                ).fetchone()["count"],
                "stale_chains": connection.execute(
                    "SELECT COUNT(*) AS count FROM chains WHERE state='stale'"
                ).fetchone()["count"],
                "running_jobs": connection.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE status='running'"
                ).fetchone()["count"],
            }
            invalidation_counts = {
                row["action"]: row["count"]
                for row in connection.execute(
                    "SELECT action, COUNT(*) AS count FROM invalidations GROUP BY action ORDER BY action"
                ).fetchall()
            }
            return {
                "initialized": True,
                "current_version": current.snapshot_version,
                "current_generation": current.generation,
                "source_digest": current.source_digest,
                "anchor_count": len(current.anchors),
                "logical_clock_watermark_ms": self._meta_int(
                    connection, "logical_clock_watermark_ms"
                ),
                "clock_precision_ms": self._meta_int(connection, "clock_precision_ms"),
                "anchor_time_basis": [
                    {
                        "alias": anchor.alias,
                        "trust_generation": anchor.trust_generation,
                        "not_before_ms": anchor.not_before_ms,
                        "not_after_ms": anchor.not_after_ms,
                        "revoked": anchor.revoked,
                        "can_issue_intermediates": anchor.can_issue_intermediates,
                        "conversion": anchor.conversion,
                    }
                    for anchor in current.anchors
                ],
                "counts": counts,
                "active": active,
                "invalidations": invalidation_counts,
                "job_states": {
                    row["status"]: row["count"]
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
                    ).fetchall()
                },
            }

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM jobs WHERE status='running' ORDER BY created_seq, job_id"
                ).fetchall()
            ]

