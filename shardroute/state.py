"""SQLite-backed rule pointer, query audit, and atomic result state."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import BizError, canonical_json, digest
from .rules import QueryTemplate, RuleSnapshot
from .sample import RulePackage, Sample


SCHEMA_VERSION = 1
UNFINISHED_STATUSES = {
    "STARTED",
    "ROUTED",
    "REWRITTEN",
    "EXECUTING",
    "EXECUTED",
    "MERGED",
}


SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE rule_versions (
    version TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    rollback_target TEXT,
    snapshot_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL,
    activated_clock_ms INTEGER
);

CREATE TABLE rule_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version TEXT NOT NULL REFERENCES rule_versions(version),
    generation INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE rule_activations (
    activation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    generation INTEGER NOT NULL UNIQUE,
    clock_ms INTEGER NOT NULL,
    UNIQUE(version, digest)
);

CREATE TABLE queries (
    query_id TEXT PRIMARY KEY,
    query_key TEXT NOT NULL,
    template_id TEXT NOT NULL,
    rule_version TEXT NOT NULL REFERENCES rule_versions(version),
    rule_generation INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    target_shards_json TEXT,
    rewritten_json TEXT,
    result_json TEXT,
    result_digest TEXT,
    error_json TEXT,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER,
    UNIQUE(query_key, rule_version)
);

CREATE TABLE shard_outcomes (
    query_id TEXT NOT NULL REFERENCES queries(query_id),
    shard_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    result_digest TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(query_id, shard_id)
);

CREATE TABLE query_staging (
    query_id TEXT PRIMARY KEY REFERENCES queries(query_id),
    payload_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE query_results (
    query_id TEXT PRIMARY KEY,
    query_key TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    UNIQUE(query_key, rule_version)
);

CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT,
    event TEXT NOT NULL,
    stage TEXT,
    clock_ms INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX idx_queries_status ON queries(status);
CREATE INDEX idx_audit_query ON audit_log(query_id, seq);
"""


@dataclass(frozen=True)
class SnapshotView:
    version: str
    generation: int
    digest: str
    effective_clock_ms: int
    rollback_target: str | None
    snapshot: RuleSnapshot
    snapshot_json: str

    def public_rule(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "effective_clock_ms": self.effective_clock_ms,
            "generation": self.generation,
            "rollback_target": self.rollback_target,
            "version": self.version,
        }


class FileLock:
    """Small advisory lock used around state creation and rule transitions."""

    def __init__(self, path: Path, timeout_seconds: float = 30.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._file: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    raise BizError(
                        "E_STATE_LOCK_TIMEOUT",
                        "timed out waiting for the state file lock",
                        details={"lock_path": str(self.path)},
                    )
                time.sleep(0.01)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BizError(
            "E_STATE_NOT_FOUND",
            "state database does not exist; run load first",
            details={"state": str(path)},
        )
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[None]:
    connection.execute(f"BEGIN {mode}")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
    text = value if isinstance(value, str) else canonical_json(value)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, text),
    )


def _touch_clock(connection: sqlite3.Connection, clock_ms: int) -> None:
    raw = _meta(connection, "last_clock_ms")
    previous = int(raw) if raw is not None else -1
    _set_meta(connection, "last_clock_ms", max(previous, clock_ms))


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

    def connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    def lock(self) -> FileLock:
        return FileLock(self.lock_path)

    @classmethod
    def initialize(cls, path: str | Path, sample: Sample, clock_ms: int) -> dict[str, Any]:
        store = cls(path)
        state_path = store.path
        initial = sample.rule(sample.initial_version)
        if clock_ms < initial.effective_clock_ms:
            raise BizError(
                "E_CLOCK_BEFORE_EFFECTIVE",
                "logical clock is before the initial rule version becomes effective",
                rule_version=initial.version,
                details={
                    "clock_ms": clock_ms,
                    "effective_clock_ms": initial.effective_clock_ms,
                },
            )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with store.lock():
            if state_path.exists() and state_path.stat().st_size > 0:
                existing = store._read_existing_identity()
                if existing["sample_digest"] != sample.sample_digest:
                    raise BizError(
                        "E_STATE_EXISTS",
                        "state database already exists for a different sample",
                        details={
                            "existing_sample_digest": existing["sample_digest"],
                            "requested_sample_digest": sample.sample_digest,
                        },
                    )
                status = store.status_payload()
                return {
                    "already_loaded": True,
                    "current_rule_version": status["current_rule_version"],
                    "loaded_versions": status["loaded_versions"],
                    "query_templates": status["query_templates"],
                    "sample_digest": sample.sample_digest,
                    "status": "LOADED",
                }
            temp_path = state_path.with_name(state_path.name + f".tmp.{os.getpid()}")
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(temp_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
            connection = sqlite3.connect(temp_path, timeout=30.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.executescript(SCHEMA_SQL)
                with transaction(connection):
                    _set_meta(connection, "schema_version", SCHEMA_VERSION)
                    _set_meta(connection, "sample_digest", sample.sample_digest)
                    _set_meta(connection, "sample_root", str(sample.root))
                    _set_meta(connection, "initial_version", sample.initial_version)
                    _set_meta(connection, "last_clock_ms", clock_ms)
                    _set_meta(
                        connection,
                        "query_templates_json",
                        {
                            template_id: sample.query_templates[template_id].to_dict()
                            for template_id in sorted(sample.query_templates)
                        },
                    )
                    for version in sorted(sample.rules):
                        package = sample.rules[version]
                        connection.execute(
                            "INSERT INTO rule_versions("
                            "version, digest, effective_clock_ms, rollback_target, snapshot_json, "
                            "loaded_clock_ms, activated_clock_ms"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                package.version,
                                package.digest,
                                package.effective_clock_ms,
                                package.rollback_target,
                                canonical_json(package.snapshot.to_dict()),
                                clock_ms,
                                clock_ms if package.version == sample.initial_version else None,
                            ),
                        )
                    connection.execute(
                        "INSERT INTO rule_pointer(singleton, version, generation, updated_clock_ms) "
                        "VALUES(1, ?, 1, ?)",
                        (sample.initial_version, clock_ms),
                    )
                    connection.execute(
                        "INSERT INTO rule_activations(version, digest, generation, clock_ms) "
                        "VALUES(?, ?, 1, ?)",
                        (initial.version, initial.digest, clock_ms),
                    )
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            os.replace(temp_path, state_path)
            for suffix in ("-wal", "-shm"):
                candidate = Path(str(temp_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        status = store.status_payload()
        return {
            "already_loaded": False,
            "current_rule_version": status["current_rule_version"],
            "loaded_versions": status["loaded_versions"],
            "query_templates": status["query_templates"],
            "sample_digest": sample.sample_digest,
            "status": "LOADED",
        }

    def _read_existing_identity(self) -> dict[str, str]:
        try:
            connection = self.connect()
            try:
                schema_version = _meta(connection, "schema_version")
                sample_digest = _meta(connection, "sample_digest")
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BizError(
                "E_STATE_CORRUPT",
                "existing state database is not a valid shardroute state",
                details={"reason": str(exc)},
            ) from exc
        if schema_version != str(SCHEMA_VERSION) or sample_digest is None:
            raise BizError("E_STATE_CORRUPT", "existing state database metadata is incomplete")
        return {"sample_digest": sample_digest}

    def read_snapshot(self, version: str | None = None) -> SnapshotView:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            if version is None:
                row = connection.execute(
                    "SELECT p.version, p.generation, r.digest, r.effective_clock_ms, "
                    "r.rollback_target, r.snapshot_json "
                    "FROM rule_pointer p JOIN rule_versions r ON r.version = p.version "
                    "WHERE p.singleton = 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT r.version, "
                    "CASE WHEN p.version = r.version THEN p.generation "
                    "ELSE COALESCE(a.generation, 0) END AS generation, "
                    "r.digest, r.effective_clock_ms, r.rollback_target, r.snapshot_json "
                    "FROM rule_versions r "
                    "LEFT JOIN rule_pointer p ON p.singleton = 1 "
                    "LEFT JOIN rule_activations a ON a.version = r.version "
                    "WHERE r.version = ?",
                    (version,),
                ).fetchone()
            if row is None:
                raise BizError(
                    "E_RULE_NOT_FOUND",
                    "bound rule version is absent from persistent state",
                    rule_version=version,
                )
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        snapshot_json = str(row["snapshot_json"])
        snapshot = RuleSnapshot.from_dict(json.loads(snapshot_json))
        actual_digest = snapshot.content_digest()
        if actual_digest != row["digest"]:
            raise BizError(
                "E_STATE_CORRUPT",
                "stored rule snapshot digest does not match",
                rule_version=str(row["version"]),
                details={"actual_digest": actual_digest, "stored_digest": row["digest"]},
            )
        generation = row["generation"]
        if version is not None and generation is None:
            generation = 0
        return SnapshotView(
            version=str(row["version"]),
            generation=int(generation),
            digest=str(row["digest"]),
            effective_clock_ms=int(row["effective_clock_ms"]),
            rollback_target=row["rollback_target"],
            snapshot=snapshot,
            snapshot_json=snapshot_json,
        )

    def query_templates(self) -> dict[str, QueryTemplate]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            raw = _meta(connection, "query_templates_json")
            connection.execute("COMMIT")
        finally:
            connection.close()
        if raw is None:
            raise BizError("E_STATE_CORRUPT", "query template catalog is missing")
        try:
            parsed = json.loads(raw)
            return {
                template_id: QueryTemplate.from_dict(parsed[template_id])
                for template_id in sorted(parsed)
            }
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise BizError("E_STATE_CORRUPT", "query template catalog is invalid") from exc

    def query_template(self, template_id: str) -> QueryTemplate:
        templates = self.query_templates()
        try:
            return templates[template_id]
        except KeyError as exc:
            raise BizError(
                "E_TEMPLATE_NOT_FOUND",
                "query template is not present in persistent state",
                details={"known_templates": sorted(templates), "query_template": template_id},
            ) from exc

    def sample_root(self) -> Path:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            value = _meta(connection, "sample_root")
            connection.execute("COMMIT")
        finally:
            connection.close()
        if value is None:
            raise BizError("E_STATE_CORRUPT", "sample root is missing from persistent state")
        return Path(value)

    def apply_rule(self, package: RulePackage, clock_ms: int) -> dict[str, Any]:
        if clock_ms < package.effective_clock_ms:
            raise BizError(
                "E_CLOCK_BEFORE_EFFECTIVE",
                "logical clock is before the rule version becomes effective",
                rule_version=package.version,
                details={
                    "clock_ms": clock_ms,
                    "effective_clock_ms": package.effective_clock_ms,
                },
            )
        with self.lock():
            connection = self.connect()
            try:
                with transaction(connection):
                    pointer = connection.execute(
                        "SELECT p.version, p.generation, r.digest, r.effective_clock_ms, "
                        "r.rollback_target, r.snapshot_json "
                        "FROM rule_pointer p JOIN rule_versions r ON r.version = p.version "
                        "WHERE p.singleton = 1"
                    ).fetchone()
                    if pointer is None:
                        raise BizError("E_STATE_CORRUPT", "rule pointer is missing")
                    stored = connection.execute(
                        "SELECT digest, effective_clock_ms, snapshot_json "
                        "FROM rule_versions WHERE version = ?",
                        (package.version,),
                    ).fetchone()
                    if stored is None:
                        raise BizError(
                            "E_RULE_NOT_FOUND",
                            "requested rule version was not loaded into state",
                            rule_version=package.version,
                        )
                    if stored["digest"] != package.digest:
                        raise BizError(
                            "E_RULE_CONFLICT",
                            "same rule version was submitted with different content",
                            rule_version=package.version,
                            details={
                                "existing_digest": stored["digest"],
                                "requested_digest": package.digest,
                            },
                        )
                    if package.rollback_target is not None:
                        raise BizError(
                            "E_RULE_ROLLBACK",
                            "a rule package that requests rollback is not accepted",
                            rule_version=package.version,
                            details={
                                "current_version": pointer["version"],
                                "rollback_target": package.rollback_target,
                            },
                        )
                    if package.version == pointer["version"]:
                        _touch_clock(connection, clock_ms)
                        return {
                            "activated": False,
                            "clock_ms": clock_ms,
                            "digest": package.digest,
                            "generation": int(pointer["generation"]),
                            "idempotent": True,
                            "previous_version": pointer["version"],
                            "version": package.version,
                        }
                    prior_activation = connection.execute(
                        "SELECT generation FROM rule_activations WHERE version = ?",
                        (package.version,),
                    ).fetchone()
                    if prior_activation is not None:
                        raise BizError(
                            "E_RULE_ROLLBACK",
                            "a previously active rule version cannot be reactivated",
                            rule_version=package.version,
                            details={
                                "current_version": pointer["version"],
                                "previous_generation": int(prior_activation["generation"]),
                            },
                        )
                    if int(stored["effective_clock_ms"]) < int(pointer["effective_clock_ms"]):
                        raise BizError(
                            "E_RULE_ROLLBACK",
                            "rule version effective clock is older than the current version",
                            rule_version=package.version,
                            details={
                                "current_effective_clock_ms": int(pointer["effective_clock_ms"]),
                                "current_version": pointer["version"],
                                "requested_effective_clock_ms": int(stored["effective_clock_ms"]),
                            },
                        )
                    if int(stored["effective_clock_ms"]) == int(pointer["effective_clock_ms"]):
                        raise BizError(
                            "E_RULE_CONFLICT",
                            "distinct rule versions cannot share the same effective clock",
                            rule_version=package.version,
                            details={"current_version": pointer["version"]},
                        )
                    generation = int(pointer["generation"]) + 1
                    connection.execute(
                        "UPDATE rule_versions SET activated_clock_ms = ? WHERE version = ?",
                        (clock_ms, package.version),
                    )
                    connection.execute(
                        "INSERT INTO rule_activations(version, digest, generation, clock_ms) "
                        "VALUES(?, ?, ?, ?)",
                        (package.version, package.digest, generation, clock_ms),
                    )
                    connection.execute(
                        "UPDATE rule_pointer SET version = ?, generation = ?, updated_clock_ms = ? "
                        "WHERE singleton = 1",
                        (package.version, generation, clock_ms),
                    )
                    _touch_clock(connection, clock_ms)
                    return {
                        "activated": True,
                        "clock_ms": clock_ms,
                        "digest": package.digest,
                        "generation": generation,
                        "idempotent": False,
                        "previous_version": pointer["version"],
                        "version": package.version,
                    }
            finally:
                connection.close()

    def status_payload(self) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            pointer = connection.execute(
                "SELECT p.version, p.generation, p.updated_clock_ms, r.digest, "
                "r.effective_clock_ms, r.snapshot_json "
                "FROM rule_pointer p JOIN rule_versions r ON r.version = p.version "
                "WHERE p.singleton = 1"
            ).fetchone()
            if pointer is None:
                raise BizError("E_STATE_CORRUPT", "rule pointer is missing")
            completed = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM query_results"
                ).fetchone()["count"]
            )
            failed = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM queries WHERE status = 'FAILED'"
                ).fetchone()["count"]
            )
            unfinished = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM queries WHERE status IN "
                    "('STARTED','ROUTED','REWRITTEN','EXECUTING','EXECUTED','MERGED')"
                ).fetchone()["count"]
            )
            loaded_versions = [
                str(row["version"])
                for row in connection.execute(
                    "SELECT version FROM rule_versions ORDER BY version"
                ).fetchall()
            ]
            templates_raw = _meta(connection, "query_templates_json")
            template_ids = sorted(json.loads(templates_raw)) if templates_raw else []
            last_clock_raw = _meta(connection, "last_clock_ms")
            connection.execute("COMMIT")
        finally:
            connection.close()
        snapshot = RuleSnapshot.from_dict(json.loads(pointer["snapshot_json"]))
        actual_digest = snapshot.content_digest()
        if actual_digest != pointer["digest"]:
            raise BizError(
                "E_STATE_CORRUPT",
                "current rule snapshot digest is invalid",
                details={"actual_digest": actual_digest, "stored_digest": pointer["digest"]},
            )
        mapping_summary = snapshot.mapping_summary()
        return {
            "completed_queries": completed,
            "current_rule_version": str(pointer["version"]),
            "effective_clock_ms": int(pointer["effective_clock_ms"]),
            "failed_queries": failed,
            "generation": int(pointer["generation"]),
            "last_clock_ms": int(last_clock_raw or 0),
            "loaded_versions": loaded_versions,
            "mapping_digest": digest(mapping_summary),
            "query_templates": template_ids,
            "rule_digest": str(pointer["digest"]),
            "shard_mapping": mapping_summary,
            "unfinished_queries": unfinished,
            "updated_clock_ms": int(pointer["updated_clock_ms"]),
        }

