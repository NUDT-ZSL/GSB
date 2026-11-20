"""SQLite persistence, atomic commits, file locks, and staged recovery."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, sha256_json
from .errors import (
    BusinessError,
    E_INDEX_DIGEST_MISMATCH,
    E_LEDGER_ROLLBACK,
    E_LOCK_TIMEOUT,
    E_NOT_INITIALIZED,
)
from .sample import Manifest, manifest_from_json


SCHEMA_VERSION = 1


DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    document_json TEXT NOT NULL,
    sample_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    chain_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    pk_key TEXT PRIMARY KEY,
    row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    seq INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    status TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    ledger_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS split_events (
    ledger_version INTEGER NOT NULL,
    index_name TEXT NOT NULL,
    event_order INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (ledger_version, index_name, event_order)
);
CREATE TABLE IF NOT EXISTS invalidations (
    ledger_version INTEGER NOT NULL,
    record_order INTEGER NOT NULL,
    invalidation_json TEXT NOT NULL,
    PRIMARY KEY (ledger_version, record_order)
);
CREATE TABLE IF NOT EXISTS ledger_events (
    ledger_version INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    seq INTEGER,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    base_ledger_version INTEGER NOT NULL,
    phase TEXT NOT NULL,
    staging_json TEXT NOT NULL,
    started_clock_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_seq ON attempts(seq);
"""


def ensure_workspace(workspace: str | os.PathLike[str]) -> Path:
    root = Path(workspace).resolve()
    (root / ".index_maint").mkdir(parents=True, exist_ok=True)
    return root


def database_path(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace).resolve() / ".index_maint" / "index.sqlite3"


def lock_path(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace).resolve() / ".index_maint" / "maintenance.lock"


@contextmanager
def file_lock(
    path: str | os.PathLike[str],
    *,
    timeout: float = 30.0,
) -> Iterator[None]:
    lock_file = Path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    raise BusinessError(
                        E_LOCK_TIMEOUT,
                        "timed out waiting for the workspace maintenance lock",
                        {"timeout_seconds": timeout},
                    )
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def open_initialized(path: str | os.PathLike[str]) -> sqlite3.Connection:
    connection = connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise BusinessError(
                E_NOT_INITIALIZED,
                "workspace has not been loaded",
                {"database": Path(path).name},
            )
        return connection
    except Exception:
        connection.close()
        raise


def _set_meta(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def initialize(
    path: str | os.PathLike[str],
    manifest: Manifest,
    snapshots: dict[str, dict[str, Any]],
) -> None:
    connection = connect(path)
    try:
        connection.executescript(DDL)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM manifest")
        connection.execute(
            "INSERT INTO manifest(id,document_json,sample_sha256) VALUES(1,?,?)",
            (canonical_json(manifest.to_dict()), manifest.sample_sha256),
        )
        _set_meta(
            connection,
            {
                "schema_version": SCHEMA_VERSION,
                "ledger_version": 0,
                "watermark_seq": 0,
                "logical_clock_ms": manifest.config.logical_start_ms,
                "sample_sha256": manifest.sample_sha256,
                "fixed_scale": manifest.config.fixed_scale,
                "rounding": manifest.config.rounding,
                "tolerance_units": manifest.config.tolerance_units,
                "clock_tolerance_ms": manifest.config.clock_tolerance_ms,
            },
        )
        for name, snapshot in sorted(snapshots.items()):
            connection.execute(
                "INSERT INTO snapshots(name,kind,snapshot_json,digest,chain_version) "
                "VALUES(?,?,?,?,?)",
                (
                    name,
                    snapshot["kind"],
                    canonical_json(snapshot),
                    snapshot["digest"],
                    snapshot["tree"]["chain_version"],
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_manifest(connection: sqlite3.Connection) -> Manifest:
    row = connection.execute(
        "SELECT document_json,sample_sha256 FROM manifest WHERE id=1"
    ).fetchone()
    if row is None:
        raise BusinessError(
            E_NOT_INITIALIZED,
            "workspace manifest is missing",
        )
    manifest = manifest_from_json(row["document_json"])
    if manifest.sample_sha256 != row["sample_sha256"]:
        raise BusinessError(
            E_LEDGER_ROLLBACK,
            "stored manifest digest does not match its normalized content",
            {
                "stored": row["sample_sha256"],
                "actual": manifest.sample_sha256,
            },
        )
    return manifest


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key,value FROM meta ORDER BY key")
    }


def read_meta_int(connection: sqlite3.Connection, key: str) -> int:
    row = connection.execute(
        "SELECT value FROM meta WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        raise BusinessError(
            E_NOT_INITIALIZED,
            "required metadata key is missing",
            {"key": key},
        )
    try:
        return int(row["value"])
    except ValueError as exc:
        raise BusinessError(
            E_LEDGER_ROLLBACK,
            "metadata value is not an integer",
            {"key": key, "value": row["value"]},
        ) from exc


def check_ledger_consistency(
    connection: sqlite3.Connection,
    *,
    expected_ledger_version: int | None = None,
) -> int:
    ledger_version = read_meta_int(connection, "ledger_version")
    max_event = connection.execute(
        "SELECT COALESCE(MAX(ledger_version),0) AS version FROM ledger_events"
    ).fetchone()["version"]
    if max_event is not None and ledger_version < int(max_event):
        raise BusinessError(
            E_LEDGER_ROLLBACK,
            "ledger version moved backwards",
            {
                "meta_ledger_version": ledger_version,
                "max_event_ledger_version": int(max_event),
            },
        )
    if expected_ledger_version is not None and ledger_version < expected_ledger_version:
        raise BusinessError(
            E_LEDGER_ROLLBACK,
            "current ledger version is older than the caller's expected version",
            {
                "current": ledger_version,
                "expected": expected_ledger_version,
            },
        )
    return ledger_version


def read_snapshots(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    import json

    result: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT name,kind,snapshot_json,digest,chain_version "
        "FROM snapshots ORDER BY name"
    ):
        snapshot = json.loads(row["snapshot_json"])
        if snapshot.get("digest") != row["digest"]:
            raise BusinessError(
                E_INDEX_DIGEST_MISMATCH,
                "stored index digest column does not match snapshot content",
                {
                    "index": row["name"],
                    "stored": row["digest"],
                    "actual": snapshot.get("digest"),
                },
            )
        if snapshot.get("kind") != row["kind"]:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "stored index kind does not match snapshot content",
                {
                    "index": row["name"],
                    "stored": row["kind"],
                    "actual": snapshot.get("kind"),
                },
            )
        if (
            snapshot.get("tree", {}).get("chain_version")
            != row["chain_version"]
        ):
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "stored split chain version does not match snapshot content",
                {
                    "index": row["name"],
                    "stored": row["chain_version"],
                    "actual": snapshot.get("tree", {}).get("chain_version"),
                },
            )
        result[row["name"]] = snapshot
    if not result:
        raise BusinessError(E_NOT_INITIALIZED, "no index snapshots are stored")
    return result


def read_rows(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    import json

    result: dict[str, dict[str, Any]] = {}
    for row in connection.execute("SELECT pk_key,row_json FROM rows ORDER BY pk_key"):
        result[row["pk_key"]] = json.loads(row["row_json"])
    return result


def replace_rows(
    connection: sqlite3.Connection,
    rows: dict[str, dict[str, Any]],
) -> None:
    connection.execute("DELETE FROM rows")
    for key, row in sorted(rows.items()):
        connection.execute(
            "INSERT INTO rows(pk_key,row_json) VALUES(?,?)",
            (key, canonical_json(row)),
        )


def read_attempts(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    result: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT attempt_id,seq,base_ledger_version,phase,staging_json,started_clock_ms "
        "FROM attempts ORDER BY seq,attempt_id"
    ):
        result.append(
            {
                "attempt_id": row["attempt_id"],
                "seq": row["seq"],
                "base_ledger_version": row["base_ledger_version"],
                "phase": row["phase"],
                "staging": json.loads(row["staging_json"]),
                "started_clock_ms": row["started_clock_ms"],
            }
        )
    return result


def upsert_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    seq: int,
    base_ledger_version: int,
    phase: str,
    staging: dict[str, Any],
    started_clock_ms: int,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO attempts("
            "attempt_id,seq,base_ledger_version,phase,staging_json,started_clock_ms"
            ") VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET "
            "phase=excluded.phase,staging_json=excluded.staging_json",
            (
                attempt_id,
                seq,
                base_ledger_version,
                phase,
                canonical_json(staging),
                started_clock_ms,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_attempt(connection: sqlite3.Connection, attempt_id: str) -> None:
    connection.execute("DELETE FROM attempts WHERE attempt_id=?", (attempt_id,))


def clear_attempts_for_seq(connection: sqlite3.Connection, seq: int) -> None:
    connection.execute("DELETE FROM attempts WHERE seq=?", (seq,))


def read_operations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    result: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT seq,digest,status,clock_ms,ledger_version,result_json,error_json "
        "FROM operations ORDER BY seq"
    ):
        result.append(
            {
                "seq": row["seq"],
                "digest": row["digest"],
                "status": row["status"],
                "clock_ms": row["clock_ms"],
                "ledger_version": row["ledger_version"],
                "result": json.loads(row["result_json"]),
                "error": json.loads(row["error_json"])
                if row["error_json"] is not None
                else None,
            }
        )
    return result


def read_ledger_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    return [
        {
            "ledger_version": row["ledger_version"],
            "kind": row["kind"],
            "seq": row["seq"],
            "detail": json.loads(row["detail_json"]),
        }
        for row in connection.execute(
            "SELECT ledger_version,kind,seq,detail_json "
            "FROM ledger_events ORDER BY ledger_version"
        )
    ]


def read_split_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    return [
        {
            "ledger_version": row["ledger_version"],
            "index_name": row["index_name"],
            "event_order": row["event_order"],
            "event": json.loads(row["event_json"]),
        }
        for row in connection.execute(
            "SELECT ledger_version,index_name,event_order,event_json "
            "FROM split_events ORDER BY ledger_version,index_name,event_order"
        )
    ]


def read_invalidations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    import json

    return [
        {
            "ledger_version": row["ledger_version"],
            "record_order": row["record_order"],
            "record": json.loads(row["invalidation_json"]),
        }
        for row in connection.execute(
            "SELECT ledger_version,record_order,invalidation_json "
            "FROM invalidations ORDER BY ledger_version,record_order"
        )
    ]


def store_split_events(
    connection: sqlite3.Connection,
    ledger_version: int,
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    for position, event in enumerate(events, start=1):
        index_name = event["tree"]
        event_order = event.get("event_order", position)
        connection.execute(
            "INSERT INTO split_events(ledger_version,index_name,event_order,event_json) "
            "VALUES(?,?,?,?)",
            (ledger_version, index_name, event_order, canonical_json(event)),
        )


def store_invalidations(
    connection: sqlite3.Connection,
    ledger_version: int,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    for position, record in enumerate(records, start=1):
        connection.execute(
            "INSERT INTO invalidations(ledger_version,record_order,invalidation_json) "
            "VALUES(?,?,?)",
            (ledger_version, record.get("record_order", position), canonical_json(record)),
        )


def store_snapshot(
    connection: sqlite3.Connection,
    name: str,
    snapshot: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO snapshots(name,kind,snapshot_json,digest,chain_version) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "kind=excluded.kind,snapshot_json=excluded.snapshot_json,"
        "digest=excluded.digest,chain_version=excluded.chain_version",
        (
            name,
            snapshot["kind"],
            canonical_json(snapshot),
            snapshot["digest"],
            snapshot["tree"]["chain_version"],
        ),
    )


def store_operation(
    connection: sqlite3.Connection,
    *,
    seq: int,
    digest: str,
    status: str,
    clock_ms: int,
    ledger_version: int,
    result: dict[str, Any],
    error: dict[str, Any] | None,
) -> None:
    connection.execute(
        "INSERT INTO operations("
        "seq,digest,status,clock_ms,ledger_version,result_json,error_json"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            seq,
            digest,
            status,
            clock_ms,
            ledger_version,
            canonical_json(result),
            canonical_json(error) if error is not None else None,
        ),
    )


def store_ledger_event(
    connection: sqlite3.Connection,
    *,
    ledger_version: int,
    kind: str,
    seq: int | None,
    detail: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO ledger_events(ledger_version,kind,seq,detail_json) "
        "VALUES(?,?,?,?)",
        (ledger_version, kind, seq, canonical_json(detail)),
    )


def update_progress_meta(
    connection: sqlite3.Connection,
    *,
    ledger_version: int,
    watermark_seq: int,
    logical_clock_ms: int,
) -> None:
    _set_meta(
        connection,
        {
            "ledger_version": ledger_version,
            "watermark_seq": watermark_seq,
            "logical_clock_ms": logical_clock_ms,
        },
    )


def snapshot_state_digest(
    snapshots: dict[str, dict[str, Any]],
) -> str:
    return sha256_json(
        {
            name: snapshot["digest"]
            for name, snapshot in sorted(snapshots.items())
        }
    )
