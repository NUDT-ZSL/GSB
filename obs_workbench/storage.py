from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .errors import E_STATE_CORRUPT, WorkbenchError
from .topology import Topology, parse_topology_payload


SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topologies (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    effective_clock_ms INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS topology_services (
    version INTEGER NOT NULL,
    service_name TEXT NOT NULL,
    failure_domain TEXT NOT NULL,
    upstream_json TEXT NOT NULL,
    PRIMARY KEY (version, service_name)
);

CREATE TABLE IF NOT EXISTS topology_instances (
    version INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    failure_domain TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (version, instance_id)
);

CREATE TABLE IF NOT EXISTS topology_silences (
    version INTEGER NOT NULL,
    silence_id TEXT NOT NULL,
    instance_id TEXT,
    service_name TEXT,
    rule_name TEXT,
    start_clock_ms INTEGER NOT NULL,
    end_clock_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (version, silence_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
    instance_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    watermark_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (instance_id, rule_name)
);

CREATE TABLE IF NOT EXISTS events (
    event_seq TEXT PRIMARY KEY,
    seq_kind INTEGER NOT NULL,
    seq_num INTEGER,
    seq_text TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    occurred_clock_ms INTEGER NOT NULL,
    arrived_clock_ms INTEGER NOT NULL,
    metric_value REAL NOT NULL,
    silenced_maintenance INTEGER NOT NULL,
    silenced_standalone INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    ingested_topology_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS events_instance_rule_time
ON events(instance_id, rule_name, occurred_clock_ms);

CREATE TABLE IF NOT EXISTS windows (
    window_key TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    closed_at_watermark_ms INTEGER,
    closed_topology_version INTEGER,
    current_revision_id INTEGER,
    created_topology_version INTEGER NOT NULL,
    UNIQUE(instance_id, rule_name, window_start_ms, window_end_ms)
);

CREATE INDEX IF NOT EXISTS windows_instance_rule_state
ON windows(instance_id, rule_name, state, window_start_ms);

CREATE TABLE IF NOT EXISTS window_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_key TEXT NOT NULL,
    topology_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    input_mode TEXT NOT NULL,
    result_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    staged INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    UNIQUE(window_key, topology_version)
);

CREATE INDEX IF NOT EXISTS revisions_staged
ON window_revisions(staged, topology_version);

CREATE TABLE IF NOT EXISTS window_events (
    window_key TEXT NOT NULL,
    event_seq TEXT NOT NULL,
    membership TEXT NOT NULL,
    discovered_topology_version INTEGER NOT NULL,
    PRIMARY KEY (window_key, event_seq)
);

CREATE INDEX IF NOT EXISTS window_events_event
ON window_events(event_seq);

CREATE TABLE IF NOT EXISTS late_ledger (
    event_seq TEXT PRIMARY KEY,
    window_key TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    occurred_clock_ms INTEGER NOT NULL,
    watermark_at_arrival INTEGER NOT NULL,
    reason TEXT NOT NULL,
    first_seen_topology_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    parent_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidations (
    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    patch_version INTEGER NOT NULL,
    window_key TEXT NOT NULL,
    triggers_json TEXT NOT NULL,
    propagation_paths_json TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(job_id, window_key)
);

CREATE INDEX IF NOT EXISTS invalidations_pending
ON invalidations(job_id, status, invalidation_id);
"""


class FileLock:
    """Cross-process exclusive writer lock using only the standard library."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"\0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        os.close(self.fd)
        self.fd = None


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")

    def _connect(self, *, writer: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if writer:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(events)")
        }
        if "silenced_standalone" not in columns:
            connection.execute(
                "ALTER TABLE events ADD COLUMN silenced_standalone INTEGER NOT NULL DEFAULT 0"
            )
        self.set_state_if_missing(connection, "schema_version", str(SCHEMA_VERSION))

    @contextlib.contextmanager
    def writer(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        if not create and not self.path.exists():
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "workbench database does not exist",
                {"path": str(self.path)},
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            connection = self._connect(writer=True)
            try:
                self.initialize(connection)
                yield connection
            finally:
                connection.close()

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "workbench database does not exist",
                {"path": str(self.path)},
            )
        connection = self._connect(writer=False)
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def get_state(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    @staticmethod
    def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO state(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @classmethod
    def set_state_if_missing(cls, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO state(key, value) VALUES(?, ?)",
            (key, value),
        )

    @classmethod
    def published_version(cls, connection: sqlite3.Connection) -> int | None:
        value = cls.get_state(connection, "published_topology_version")
        return int(value) if value is not None else None

    @classmethod
    def max_seen_version(cls, connection: sqlite3.Connection) -> int:
        value = cls.get_state(connection, "max_seen_topology_version")
        return int(value) if value is not None else 0

    @classmethod
    def load_topology(cls, connection: sqlite3.Connection, version: int) -> Topology:
        row = connection.execute(
            "SELECT * FROM topologies WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "topology version is missing from persisted state",
                {"topology_version": version},
            )
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "persisted topology payload is not valid JSON",
                {"topology_version": version},
            ) from exc
        topology = parse_topology_payload(payload)
        return replace(
            topology,
            parent_version=row["parent_version"],
            source_kind=str(row["source_kind"]),
            source_digest=str(row["source_digest"]),
        )

    @classmethod
    def load_published_topology(cls, connection: sqlite3.Connection) -> Topology | None:
        version = cls.published_version(connection)
        return cls.load_topology(connection, version) if version is not None else None

    @staticmethod
    def store_topology(
        connection: sqlite3.Connection,
        topology: Topology,
        *,
        parent_version: int | None,
        content_digest: str,
        source_kind: str,
        source_digest: str,
        published: bool,
    ) -> None:
        existing = connection.execute(
            "SELECT content_digest FROM topologies WHERE version = ?",
            (topology.version,),
        ).fetchone()
        if existing is not None:
            if str(existing["content_digest"]) != content_digest:
                raise WorkbenchError(
                    E_STATE_CORRUPT,
                    "topology version already exists with a different digest",
                    {
                        "existing_digest": str(existing["content_digest"]),
                        "new_digest": content_digest,
                        "topology_version": topology.version,
                    },
                )
            return
        connection.execute(
            """
            INSERT INTO topologies(
                version, parent_version, effective_clock_ms, content_digest,
                payload_json, source_kind, source_digest, published
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topology.version,
                parent_version,
                topology.effective_clock_ms,
                content_digest,
                json.dumps(topology.canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                source_kind,
                source_digest,
                1 if published else 0,
            ),
        )
        for service in sorted(topology.services.values(), key=lambda item: item.service_name):
            connection.execute(
                """
                INSERT INTO topology_services(version, service_name, failure_domain, upstream_json)
                VALUES(?, ?, ?, ?)
                """,
                (
                    topology.version,
                    service.service_name,
                    service.failure_domain,
                    json.dumps(list(service.upstream_services), separators=(",", ":")),
                ),
            )
        for instance in sorted(topology.instances.values(), key=lambda item: item.instance_id):
            connection.execute(
                """
                INSERT INTO topology_instances(
                    version, instance_id, service_name, failure_domain, weight
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    topology.version,
                    instance.instance_id,
                    instance.service_name,
                    instance.failure_domain,
                    instance.weight,
                ),
            )
        for silence in sorted(topology.silences.values(), key=lambda item: item.silence_id):
            connection.execute(
                """
                INSERT INTO topology_silences(
                    version, silence_id, instance_id, service_name, rule_name,
                    start_clock_ms, end_clock_ms, kind
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topology.version,
                    silence.silence_id,
                    silence.instance_id,
                    silence.service_name,
                    silence.rule_name,
                    silence.start_clock_ms,
                    silence.end_clock_ms,
                    silence.kind,
                ),
            )

    @classmethod
    def pending_jobs(cls, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT * FROM jobs WHERE status != 'DONE' ORDER BY target_version, job_id"
            )
        )

    @classmethod
    def ensure_no_pending_jobs(cls, connection: sqlite3.Connection) -> None:
        jobs = cls.pending_jobs(connection)
        if jobs:
            raise WorkbenchError(
                "E_RESUME_REQUIRED",
                "a topology propagation job is incomplete; run resume first",
                {
                    "jobs": [
                        {
                            "job_id": str(row["job_id"]),
                            "parent_version": int(row["parent_version"]),
                            "status": str(row["status"]),
                            "target_version": int(row["target_version"]),
                        }
                        for row in jobs
                    ]
                },
            )
