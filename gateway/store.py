"""SQLite persistence: immutable snapshots, one atomic pointer, event journal."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json, sha256_obj, sha256_text
from .engine import Engine, build_genesis
from .errors import GatewayError
from .filelock import FileLock


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inputs (
    kind TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    event_hash TEXT NOT NULL,
    event_json TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _failpoint(name: str) -> None:
    """Test-only hard termination point.

    ``os._exit`` intentionally skips Python cleanup so the next process sees
    exactly what SQLite recovered from the journal/WAL.
    """

    if os.environ.get("GW_FAILPOINT") == name:
        os._exit(137)


class Store:
    def __init__(self, path: str | Path, *, failpoint: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = Path(str(self.path) + ".lock")
        if failpoint is not None:
            os.environ["GW_FAILPOINT"] = failpoint

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.path.exists():
                raise GatewayError(
                    "E_NOT_LOADED",
                    "ledger database does not exist",
                    db=str(self.path),
                )
        # Open without ``mode=ro`` even for readers: SQLite may need to recover
        # a hot WAL after a forced process termination.  Readers still only
        # issue SELECTs and observe one committed snapshot.
        conn = sqlite3.connect(str(self.path), timeout=60.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _has_pointer_table(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pointer'"
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Load / genesis
    # ------------------------------------------------------------------
    def load(self, normalized: dict[str, Any]) -> dict[str, Any]:
        fingerprint = normalized["fingerprint"]
        blobs = normalized["blobs"]

        existing = self._read_loaded_state_if_any()
        if existing is not None:
            state, version = existing
            if state["db_id"] == fingerprint:
                return {
                    "ok": True,
                    "command": "load",
                    "db_id": fingerprint,
                    "version": version,
                    "config_version": state["config_version"],
                    "idempotent": True,
                }
            current_config_version = int(state["config_version"])
            new_config_version = int(normalized["config"]["config_version"])
            if new_config_version < current_config_version:
                raise GatewayError(
                    "E_CONFIG_VERSION_REGRESSION",
                    "token configuration version cannot move backwards",
                    current_config_version=current_config_version,
                    requested_config_version=new_config_version,
                )
            if new_config_version == current_config_version:
                raise GatewayError(
                    "E_CONFIG_VERSION_CONFLICT",
                    "same token configuration version has different input content",
                    config_version=new_config_version,
                )
            raise GatewayError(
                "E_ALREADY_LOADED",
                "database already contains a ledger; use a new database for a new config version",
                current_config_version=current_config_version,
                requested_config_version=new_config_version,
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(SCHEMA_SQL)
                conn.execute("BEGIN IMMEDIATE")
                if self._has_pointer_table(conn):
                    row = conn.execute(
                        "SELECT version FROM pointer WHERE id = 1"
                    ).fetchone()
                    if row is not None:
                        conn.execute("ROLLBACK")
                        state, version = self._read_loaded_state_if_any(required=True)
                        if state["db_id"] == fingerprint:
                            return {
                                "ok": True,
                                "command": "load",
                                "db_id": fingerprint,
                                "version": version,
                                "config_version": state["config_version"],
                                "idempotent": True,
                            }
                        raise GatewayError(
                            "E_ALREADY_LOADED",
                            "database was initialized by a concurrent loader",
                        )

                now = utc_now()
                for kind in sorted(blobs):
                    payload = canonical_json(blobs[kind])
                    conn.execute(
                        "INSERT INTO inputs(kind, sha256, payload_json) VALUES (?, ?, ?)",
                        (kind, sha256_text(payload), payload),
                    )
                state = build_genesis(normalized)
                state_json = canonical_json(state)
                state_hash = sha256_text(state_json)
                conn.execute(
                    "INSERT INTO snapshots(version, parent_version, state_hash, state_json, created_at) "
                    "VALUES (1, NULL, ?, ?, ?)",
                    (state_hash, state_json, now),
                )
                conn.execute(
                    "INSERT INTO pointer(id, version, state_hash, updated_at) "
                    "VALUES (1, 1, ?, ?)",
                    (state_hash, now),
                )
                conn.execute(
                    "INSERT INTO events(version, event_hash, event_json) VALUES (0, ?, ?)",
                    (
                        sha256_obj(
                            {
                                "type": "genesis",
                                "db_id": fingerprint,
                                "input_hashes": {
                                    kind: sha256_text(canonical_json(blobs[kind]))
                                    for kind in sorted(blobs)
                                },
                            }
                        ),
                        canonical_json(
                            {
                                "type": "genesis",
                                "db_id": fingerprint,
                                "input_hashes": {
                                    kind: sha256_text(canonical_json(blobs[kind]))
                                    for kind in sorted(blobs)
                                },
                            }
                        ),
                    ),
                )
                _failpoint("load_before_commit")
                conn.execute("COMMIT")
                _failpoint("load_after_commit")
                return {
                    "ok": True,
                    "command": "load",
                    "db_id": fingerprint,
                    "version": 1,
                    "config_version": state["config_version"],
                    "services": len(state["services"]),
                    "requests": len(state["requests"]),
                    "idempotent": False,
                }
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _read_loaded_state_if_any(
        self, *, required: bool = False
    ) -> tuple[dict[str, Any], int] | None:
        if not self.path.exists():
            if required:
                raise GatewayError(
                    "E_NOT_LOADED", "ledger database does not exist", db=str(self.path)
                )
            return None
        try:
            conn = self._connect(readonly=True)
        except sqlite3.Error as exc:
            raise GatewayError(
                "E_DB_CORRUPT", f"cannot open ledger database: {exc}", db=str(self.path)
            ) from exc
        try:
            if not self._has_pointer_table(conn):
                if required:
                    raise GatewayError(
                        "E_NOT_LOADED",
                        "ledger database has not been loaded",
                        db=str(self.path),
                    )
                return None
            row = conn.execute(
                "SELECT p.version, s.state_json, s.state_hash, p.state_hash AS pointer_hash "
                "FROM pointer p JOIN snapshots s ON s.version = p.version "
                "WHERE p.id = 1"
            ).fetchone()
            if row is None:
                if required:
                    raise GatewayError(
                        "E_DB_CORRUPT",
                        "ledger pointer is missing",
                        db=str(self.path),
                    )
                return None
            state = json.loads(row["state_json"])
            if sha256_text(row["state_json"]) != row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "ledger snapshot hash does not match pointer",
                    version=int(row["version"]),
                )
            if row["pointer_hash"] != row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "pointer hash does not match snapshot hash",
                    version=int(row["version"]),
                )
            return state, int(row["version"])
        except sqlite3.Error as exc:
            raise GatewayError(
                "E_DB_CORRUPT", f"cannot read ledger database: {exc}", db=str(self.path)
            ) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    def read_state(self) -> tuple[dict[str, Any], int, str]:
        if not self.path.exists():
            raise GatewayError(
                "E_NOT_LOADED", "ledger database does not exist", db=str(self.path)
            )
        conn = self._connect(readonly=True)
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT p.version, s.state_json, s.state_hash, p.state_hash AS pointer_hash "
                "FROM pointer p JOIN snapshots s ON s.version = p.version "
                "WHERE p.id = 1"
            ).fetchone()
            if row is None:
                raise GatewayError("E_NOT_LOADED", "ledger has not been initialized")
            state_json = row["state_json"]
            state_hash = sha256_text(state_json)
            if state_hash != row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "snapshot hash mismatch",
                    version=int(row["version"]),
                )
            if row["pointer_hash"] != row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "pointer hash does not match snapshot hash",
                    version=int(row["version"]),
                )
            conn.execute("COMMIT")
            return json.loads(state_json), int(row["version"]), state_hash
        except sqlite3.Error as exc:
            raise GatewayError(
                "E_DB_CORRUPT", f"cannot read ledger: {exc}", db=str(self.path)
            ) from exc
        finally:
            conn.close()

    def read_inputs(self) -> dict[str, Any]:
        conn = self._connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT kind, sha256, payload_json FROM inputs ORDER BY kind"
            ).fetchall()
            if not rows:
                raise GatewayError("E_NOT_LOADED", "ledger inputs are missing")
            result: dict[str, Any] = {}
            for row in rows:
                payload = row["payload_json"]
                actual = sha256_text(payload)
                if actual != row["sha256"]:
                    raise GatewayError(
                        "E_DB_CORRUPT",
                        f"input blob hash mismatch for {row['kind']}",
                        kind=row["kind"],
                    )
                result[row["kind"]] = json.loads(payload)
            return result
        finally:
            conn.close()

    def read_events(self) -> list[dict[str, Any]]:
        conn = self._connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT seq, version, event_hash, event_json FROM events ORDER BY seq"
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in rows:
                event_json = row["event_json"]
                if sha256_text(event_json) != row["event_hash"]:
                    raise GatewayError(
                        "E_DB_CORRUPT",
                        "event hash mismatch",
                        seq=int(row["seq"]),
                    )
                events.append(
                    {
                        "seq": int(row["seq"]),
                        "version": int(row["version"]),
                        "event": json.loads(event_json),
                    }
                )
            return events
        finally:
            conn.close()

    def read_snapshots(self) -> list[dict[str, Any]]:
        conn = self._connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT version, parent_version, state_hash FROM snapshots ORDER BY version"
            ).fetchall()
            return [
                {
                    "version": int(row["version"]),
                    "parent_version": row["parent_version"],
                    "state_hash": row["state_hash"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def read_bundle(self) -> dict[str, Any]:
        """Read inputs, snapshots, events and the pointer from one SQLite snapshot."""

        conn = self._connect(readonly=True)
        try:
            conn.execute("BEGIN")
            input_rows = conn.execute(
                "SELECT kind, sha256, payload_json FROM inputs ORDER BY kind"
            ).fetchall()
            if not input_rows:
                raise GatewayError("E_NOT_LOADED", "ledger inputs are missing")
            inputs: dict[str, Any] = {}
            for row in input_rows:
                payload = row["payload_json"]
                if sha256_text(payload) != row["sha256"]:
                    raise GatewayError(
                        "E_DB_CORRUPT",
                        f"input blob hash mismatch for {row['kind']}",
                        kind=row["kind"],
                    )
                inputs[row["kind"]] = json.loads(payload)

            snapshot_rows = conn.execute(
                "SELECT version, parent_version, state_hash FROM snapshots ORDER BY version"
            ).fetchall()
            snapshots = [
                {
                    "version": int(row["version"]),
                    "parent_version": row["parent_version"],
                    "state_hash": row["state_hash"],
                }
                for row in snapshot_rows
            ]

            event_rows = conn.execute(
                "SELECT seq, version, event_hash, event_json FROM events ORDER BY seq"
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in event_rows:
                event_json = row["event_json"]
                if sha256_text(event_json) != row["event_hash"]:
                    raise GatewayError(
                        "E_DB_CORRUPT",
                        "event hash mismatch",
                        seq=int(row["seq"]),
                    )
                events.append(
                    {
                        "seq": int(row["seq"]),
                        "version": int(row["version"]),
                        "event": json.loads(event_json),
                    }
                )

            pointer_row = conn.execute(
                "SELECT p.version, s.state_json, s.state_hash, p.state_hash AS pointer_hash "
                "FROM pointer p JOIN snapshots s ON s.version = p.version "
                "WHERE p.id = 1"
            ).fetchone()
            if pointer_row is None:
                raise GatewayError("E_NOT_LOADED", "ledger has not been initialized")
            state_json = pointer_row["state_json"]
            if sha256_text(state_json) != pointer_row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "snapshot hash mismatch",
                    version=int(pointer_row["version"]),
                )
            if pointer_row["pointer_hash"] != pointer_row["state_hash"]:
                raise GatewayError(
                    "E_DB_CORRUPT",
                    "pointer hash does not match snapshot hash",
                    version=int(pointer_row["version"]),
                )
            conn.execute("COMMIT")
            return {
                "inputs": inputs,
                "snapshots": snapshots,
                "events": events,
                "state": json.loads(state_json),
                "version": int(pointer_row["version"]),
                "state_hash": pointer_row["state_hash"],
            }
        except sqlite3.Error as exc:
            raise GatewayError(
                "E_DB_CORRUPT", f"cannot read ledger bundle: {exc}", db=str(self.path)
            ) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Atomic command application
    # ------------------------------------------------------------------
    def apply_round(self, command: dict[str, Any]) -> dict[str, Any]:
        if not self.path.exists():
            raise GatewayError(
                "E_NOT_LOADED", "ledger database does not exist", db=str(self.path)
            )
        with FileLock(self.lock_path):
            for attempt in range(3):
                conn = self._connect()
                try:
                    conn.execute("PRAGMA synchronous = FULL")
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT p.version, s.state_json, s.state_hash, "
                        "p.state_hash AS pointer_hash FROM pointer p "
                        "JOIN snapshots s ON s.version = p.version WHERE p.id = 1"
                    ).fetchone()
                    if row is None:
                        raise GatewayError("E_NOT_LOADED", "ledger has not been initialized")
                    old_version = int(row["version"])
                    old_json = row["state_json"]
                    if sha256_text(old_json) != row["state_hash"]:
                        raise GatewayError(
                            "E_DB_CORRUPT",
                            "snapshot hash mismatch before transaction",
                            version=old_version,
                        )
                    if row["pointer_hash"] != row["state_hash"]:
                        raise GatewayError(
                            "E_DB_CORRUPT",
                            "pointer hash mismatch before transaction",
                            version=old_version,
                        )
                    state = json.loads(old_json)
                    engine = Engine(state)
                    result = engine.run_round(command)
                    new_json = canonical_json(engine.state)
                    if new_json == old_json:
                        conn.execute("ROLLBACK")
                        result["idempotent"] = True
                        result["ledger_version"] = old_version
                        return result

                    _failpoint("after_mutation")
                    new_version = old_version + 1
                    state_hash = sha256_text(new_json)
                    now = utc_now()
                    conn.execute(
                        "INSERT INTO snapshots(version, parent_version, state_hash, state_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (new_version, old_version, state_hash, new_json, now),
                    )
                    _failpoint("after_snapshot_insert")
                    updated = conn.execute(
                        "UPDATE pointer SET version = ?, state_hash = ?, updated_at = ? "
                        "WHERE id = 1 AND version = ?",
                        (new_version, state_hash, now, old_version),
                    )
                    if updated.rowcount != 1:
                        conn.execute("ROLLBACK")
                        continue
                    event = {"type": "round", "command": command}
                    event_json = canonical_json(event)
                    conn.execute(
                        "INSERT INTO events(version, event_hash, event_json) VALUES (?, ?, ?)",
                        (new_version, sha256_text(event_json), event_json),
                    )
                    _failpoint("before_commit")
                    conn.execute("COMMIT")
                    _failpoint("after_commit")
                    result["ledger_version"] = new_version
                    return result
                except sqlite3.Error as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise GatewayError(
                        "E_DB_ERROR",
                        f"SQLite transaction failed: {exc}",
                        db=str(self.path),
                    ) from exc
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    conn.close()
        raise GatewayError("E_DB_CONFLICT", "ledger version changed repeatedly")


def apply_event_to_engine(engine: Engine, event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "round":
        raise GatewayError("E_DB_CORRUPT", "unsupported event type in journal")
    return engine.run_round(event["command"])
