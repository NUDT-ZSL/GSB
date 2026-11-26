"""Versioned SQLite ledger with atomic snapshot-pointer replacement."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import signal
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import (
    CONCURRENCY_LOCK_FAILED,
    LEDGER_CORRUPT,
    LEDGER_NOT_INITIALIZED,
    LEDGER_VERSION_REGRESSION,
    StoreError,
)
from .util import canonical_json, sha256_text

LEDGER_SCHEMA = "seqledger-ledger/v1"


def empty_state() -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "version": 0,
        "revision": 0,
        "logical_clock_ms": 0,
        "topology": None,
        "subscriptions": [],
        "partitions": {},
        "event_index": {},
        "last_event_seq": -1,
        "next_decision_seq": 0,
        "next_operation_seq": 0,
        "semantics": {
            "view": None,
            "last_merge_clock_ms": None,
            "v2_current_generation": 0,
            "patch_history": [],
        },
        "delivery_claims": {},
        "dead_letter_index": {},
        "invalidation_history": [],
        "operations": [],
    }


@dataclass(frozen=True)
class CommitResult:
    changed: bool
    version: int
    revision: int
    sha256: str


class LedgerStore:
    """A single SQLite file whose visible ledger is one immutable JSON snapshot.

    Every state-changing call builds the next snapshot in a temporary SQLite
    table, inserts it as a new immutable version, and advances a one-row pointer
    in the same transaction. Readers therefore never observe a half-ledger.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        fault_kill_after_commits: int | None = None,
        fault_kill_before_commit: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(str(self.db_path) + ".lock")
        self.fault_kill_after_commits = fault_kill_after_commits
        self.fault_kill_before_commit = fault_kill_before_commit
        self.commit_count = 0

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as exc:
            raise StoreError(
                CONCURRENCY_LOCK_FAILED,
                "Could not open the ledger file lock",
                diagnostics={"lock_path": str(self.lock_path), "reason": str(exc)},
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise StoreError(
                LEDGER_CORRUPT,
                "Could not open the ledger database",
                diagnostics={"db_path": str(self.db_path), "reason": str(exc)},
            ) from exc

    def initialize(self) -> CommitResult:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ledger_versions ("
                    "version INTEGER PRIMARY KEY, "
                    "sha256 TEXT NOT NULL, "
                    "body TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ledger_pointer ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "current_version INTEGER NOT NULL, "
                    "snapshot_sha256 TEXT NOT NULL"
                    ")"
                )
                existing = connection.execute(
                    "SELECT current_version, snapshot_sha256 FROM ledger_pointer "
                    "WHERE singleton = 1"
                ).fetchone()
                if existing is not None:
                    state, sha = self._read_state_on_connection(connection)
                    connection.execute("COMMIT")
                    return CommitResult(
                        changed=False,
                        version=int(existing["current_version"]),
                        revision=int(state["revision"]),
                        sha256=sha,
                    )

                state = empty_state()
                body = canonical_json(state)
                digest = sha256_text(body)
                connection.execute(
                    "CREATE TEMP TABLE _seqledger_initial_stage ("
                    "version INTEGER NOT NULL, "
                    "body TEXT NOT NULL, "
                    "sha256 TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "INSERT INTO _seqledger_initial_stage(version, body, sha256) "
                    "VALUES (?, ?, ?)",
                    (0, body, digest),
                )
                connection.execute(
                    "INSERT INTO ledger_versions(version, sha256, body) "
                    "SELECT version, sha256, body FROM _seqledger_initial_stage"
                )
                connection.execute(
                    "INSERT INTO ledger_pointer(singleton, current_version, snapshot_sha256) "
                    "VALUES (1, 0, ?)",
                    (digest,),
                )
                connection.execute("COMMIT")
                return CommitResult(
                    changed=True,
                    version=0,
                    revision=0,
                    sha256=digest,
                )
            except StoreError:
                connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(
                    LEDGER_CORRUPT,
                    "Could not initialize the ledger database",
                    diagnostics={"db_path": str(self.db_path), "reason": str(exc)},
                ) from exc
            finally:
                connection.close()

    def _verify_versions_against_pointer(
        self,
        connection: sqlite3.Connection,
        *,
        current_version: int,
    ) -> None:
        maximum = connection.execute(
            "SELECT MAX(version) AS maximum FROM ledger_versions"
        ).fetchone()["maximum"]
        if maximum is None:
            raise StoreError(
                LEDGER_CORRUPT,
                "Ledger database has no version rows",
                diagnostics={"db_path": str(self.db_path)},
            )
        if int(maximum) > current_version:
            raise StoreError(
                LEDGER_VERSION_REGRESSION,
                "Ledger pointer regressed behind an existing immutable version",
                diagnostics={
                    "db_path": str(self.db_path),
                    "current_version": current_version,
                    "maximum_version": int(maximum),
                },
            )
        if int(maximum) < current_version:
            raise StoreError(
                LEDGER_CORRUPT,
                "Ledger pointer references a missing snapshot",
                diagnostics={
                    "db_path": str(self.db_path),
                    "current_version": current_version,
                    "maximum_version": int(maximum),
                },
            )

    def _read_state_on_connection(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[dict[str, Any], str]:
        pointer = connection.execute(
            "SELECT current_version, snapshot_sha256 FROM ledger_pointer "
            "WHERE singleton = 1"
        ).fetchone()
        if pointer is None:
            raise StoreError(
                LEDGER_NOT_INITIALIZED,
                "Ledger pointer is missing",
                diagnostics={"db_path": str(self.db_path)},
            )
        current_version = int(pointer["current_version"])
        self._verify_versions_against_pointer(
            connection,
            current_version=current_version,
        )
        row = connection.execute(
            "SELECT version, body, sha256 FROM ledger_versions WHERE version = ?",
            (current_version,),
        ).fetchone()
        if row is None:
            raise StoreError(
                LEDGER_CORRUPT,
                "Current ledger snapshot is missing",
                diagnostics={"db_path": str(self.db_path), "version": current_version},
            )
        body = str(row["body"])
        digest = sha256_text(body)
        if digest != row["sha256"] or digest != pointer["snapshot_sha256"]:
            raise StoreError(
                LEDGER_CORRUPT,
                "Ledger snapshot hash does not match its pointer",
                diagnostics={
                    "db_path": str(self.db_path),
                    "version": current_version,
                    "stored_sha256": row["sha256"],
                    "actual_sha256": digest,
                    "pointer_sha256": pointer["snapshot_sha256"],
                },
            )
        try:
            state = json.loads(body)
        except json.JSONDecodeError as exc:
            raise StoreError(
                LEDGER_CORRUPT,
                "Ledger snapshot body is not valid JSON",
                diagnostics={"db_path": str(self.db_path), "version": current_version},
            ) from exc
        if state.get("schema") != LEDGER_SCHEMA or state.get("version") != current_version:
            raise StoreError(
                LEDGER_CORRUPT,
                "Ledger snapshot metadata is inconsistent with its pointer",
                diagnostics={
                    "db_path": str(self.db_path),
                    "version": current_version,
                    "body_schema": state.get("schema"),
                    "body_version": state.get("version"),
                },
            )
        return state, digest

    def read_state(self) -> tuple[dict[str, Any], CommitResult]:
        with self._file_lock():
            try:
                connection = self._connect()
                self._require_schema(connection)
                state, digest = self._read_state_on_connection(connection)
                return state, CommitResult(
                    changed=False,
                    version=int(state["version"]),
                    revision=int(state["revision"]),
                    sha256=digest,
                )
            finally:
                if "connection" in locals():
                    connection.close()

    def _require_schema(self, connection: sqlite3.Connection) -> None:
        existing = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"ledger_versions", "ledger_pointer"}
        if not required.issubset(existing):
            raise StoreError(
                LEDGER_NOT_INITIALIZED,
                "Ledger database has not been initialized",
                diagnostics={
                    "db_path": str(self.db_path),
                    "missing_tables": sorted(required - existing),
                },
            )

    def commit(
        self,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        expected_version: int | None = None,
    ) -> CommitResult:
        """Apply one state mutation and atomically move the ledger pointer."""

        with self._file_lock():
            connection = self._connect()
            try:
                self._require_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                old_state, old_sha = self._read_state_on_connection(connection)
                current_version = int(old_state["version"])
                if expected_version is not None and expected_version != current_version:
                    raise StoreError(
                        LEDGER_VERSION_REGRESSION,
                        "Expected ledger version does not match the current pointer",
                        diagnostics={
                            "expected_version": expected_version,
                            "current_version": current_version,
                        },
                    )

                working = copy.deepcopy(old_state)
                returned = mutator(working)
                if returned is not None:
                    working = returned
                old_body_for_noop = canonical_json(old_state)
                if canonical_json(working) == old_body_for_noop:
                    connection.execute("COMMIT")
                    return CommitResult(
                        changed=False,
                        version=current_version,
                        revision=int(old_state["revision"]),
                        sha256=old_sha,
                    )

                next_version = current_version + 1
                if next_version <= current_version:
                    raise StoreError(
                        LEDGER_VERSION_REGRESSION,
                        "Computed ledger version would not increase monotonically",
                        diagnostics={
                            "current_version": current_version,
                            "next_version": next_version,
                        },
                    )
                working["version"] = next_version
                working["revision"] = int(old_state["revision"]) + 1
                body = canonical_json(working)
                digest = sha256_text(body)

                if self.fault_kill_before_commit:
                    os.kill(os.getpid(), signal.SIGKILL)

                connection.execute("DROP TABLE IF EXISTS temp._seqledger_next_stage")
                connection.execute(
                    "CREATE TEMP TABLE _seqledger_next_stage ("
                    "version INTEGER NOT NULL, "
                    "body TEXT NOT NULL, "
                    "sha256 TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "INSERT INTO _seqledger_next_stage(version, body, sha256) "
                    "VALUES (?, ?, ?)",
                    (next_version, body, digest),
                )
                staged = connection.execute(
                    "SELECT version, sha256, body FROM _seqledger_next_stage"
                ).fetchone()
                if (
                    staged is None
                    or int(staged["version"]) != next_version
                    or staged["sha256"] != digest
                    or sha256_text(str(staged["body"])) != digest
                ):
                    raise StoreError(
                        LEDGER_CORRUPT,
                        "Temporary ledger stage failed verification",
                        diagnostics={"next_version": next_version},
                    )
                connection.execute(
                    "INSERT INTO ledger_versions(version, sha256, body) "
                    "SELECT version, sha256, body FROM _seqledger_next_stage"
                )
                pointer_update = connection.execute(
                    "UPDATE ledger_pointer SET current_version = ?, snapshot_sha256 = ? "
                    "WHERE singleton = 1 AND current_version = ? AND snapshot_sha256 = ?",
                    (next_version, digest, current_version, old_sha),
                )
                if pointer_update.rowcount != 1:
                    raise StoreError(
                        LEDGER_VERSION_REGRESSION,
                        "Ledger pointer replacement was rejected",
                        diagnostics={
                            "current_version": current_version,
                            "next_version": next_version,
                        },
                    )
                connection.execute("COMMIT")
                self.commit_count += 1

                if (
                    self.fault_kill_after_commits is not None
                    and self.commit_count == self.fault_kill_after_commits
                ):
                    os.kill(os.getpid(), signal.SIGKILL)
                return CommitResult(
                    changed=True,
                    version=next_version,
                    revision=int(working["revision"]),
                    sha256=digest,
                )
            except StoreError:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(
                    LEDGER_CORRUPT,
                    "Ledger transaction failed",
                    diagnostics={"db_path": str(self.db_path), "reason": str(exc)},
                ) from exc
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def inspect(self) -> dict[str, Any]:
        """Validate every immutable version and the active pointer."""

        with self._file_lock():
            connection = self._connect()
            try:
                self._require_schema(connection)
                rows = connection.execute(
                    "SELECT version, sha256, body FROM ledger_versions ORDER BY version"
                ).fetchall()
                if not rows:
                    raise StoreError(
                        LEDGER_CORRUPT,
                        "Ledger database contains no versions",
                        diagnostics={"db_path": str(self.db_path)},
                    )
                version_numbers = [int(row["version"]) for row in rows]
                expected_numbers = list(range(version_numbers[0], version_numbers[-1] + 1))
                if version_numbers != expected_numbers:
                    raise StoreError(
                        LEDGER_VERSION_REGRESSION,
                        "Ledger version sequence is not contiguous",
                        diagnostics={
                            "versions": version_numbers,
                            "expected": expected_numbers,
                        },
                    )
                verified_rows: list[dict[str, Any]] = []
                for row in rows:
                    body = str(row["body"])
                    digest = sha256_text(body)
                    if digest != row["sha256"]:
                        raise StoreError(
                            LEDGER_CORRUPT,
                            f"Ledger version {row['version']} hash mismatch",
                            diagnostics={
                                "version": int(row["version"]),
                                "stored_sha256": row["sha256"],
                                "actual_sha256": digest,
                            },
                        )
                    parsed = json.loads(body)
                    if (
                        parsed.get("schema") != LEDGER_SCHEMA
                        or parsed.get("version") != int(row["version"])
                    ):
                        raise StoreError(
                            LEDGER_CORRUPT,
                            f"Ledger version {row['version']} body metadata mismatch",
                            diagnostics={"version": int(row["version"])},
                        )
                    verified_rows.append(
                        {
                            "version": int(row["version"]),
                            "sha256": digest,
                            "revision": int(parsed.get("revision", 0)),
                            "logical_clock_ms": int(parsed.get("logical_clock_ms", 0)),
                        }
                    )
                pointer = connection.execute(
                    "SELECT current_version, snapshot_sha256 FROM ledger_pointer "
                    "WHERE singleton = 1"
                ).fetchone()
                if pointer is None:
                    raise StoreError(
                        LEDGER_NOT_INITIALIZED,
                        "Ledger pointer is missing",
                        diagnostics={"db_path": str(self.db_path)},
                    )
                last = verified_rows[-1]
                if (
                    int(pointer["current_version"]) != last["version"]
                    or pointer["snapshot_sha256"] != last["sha256"]
                ):
                    raise StoreError(
                        LEDGER_VERSION_REGRESSION,
                        "Ledger pointer does not match the latest immutable version",
                        diagnostics={
                            "pointer_version": int(pointer["current_version"]),
                            "latest_version": last["version"],
                        },
                    )
                state, active_sha = self._read_state_on_connection(connection)
                return {
                    "ok": True,
                    "db_path": str(self.db_path),
                    "current_version": int(pointer["current_version"]),
                    "current_sha256": active_sha,
                    "revision": int(state["revision"]),
                    "versions": verified_rows,
                    "version_count": len(verified_rows),
                }
            finally:
                connection.close()
