from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from .errors import BusinessError, ErrorCode

T = TypeVar("T")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


@dataclass(frozen=True)
class LedgerRevision:
    version: int
    parent_version: int
    state_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "state_sha256": self.state_sha256,
        }


@dataclass(frozen=True)
class ExecutionResult:
    result: dict[str, Any]
    revision: LedgerRevision
    applied: bool


class LedgerStore:
    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 30_000) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def initialize_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_versions (
                    version INTEGER PRIMARY KEY,
                    parent_version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    commit_kind TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS current_ledger (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL,
                    FOREIGN KEY (version) REFERENCES ledger_versions(version)
                );

                CREATE TABLE IF NOT EXISTS idempotency (
                    instance_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    ledger_version INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (instance_id, round_id, source)
                );
                """
            )
        finally:
            connection.close()

    def initialize(self, initial_state: dict[str, Any]) -> LedgerRevision:
        self.initialize_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM current_ledger WHERE singleton = 1"
            ).fetchone()
            if row is not None:
                state, revision = self._read_current(connection)
                if state.get("input_fingerprint") != initial_state.get("input_fingerprint"):
                    raise BusinessError(
                        ErrorCode.INPUT_FINGERPRINT_MISMATCH,
                        "database was initialized with a different input bundle",
                        {
                            "stored_fingerprint": state.get("input_fingerprint"),
                            "provided_fingerprint": initial_state.get("input_fingerprint"),
                        },
                    )
                connection.commit()
                return revision

            state = dict(initial_state)
            state["generation"] = 1
            text = canonical_json(state)
            digest = sha256_text(text)
            connection.execute(
                """
                INSERT INTO ledger_versions
                    (version, parent_version, state_json, state_sha256, commit_kind)
                VALUES (1, 0, ?, ?, 'INITIALIZE')
                """,
                (text, digest),
            )
            connection.execute(
                "INSERT INTO current_ledger (singleton, version) VALUES (1, 1)"
            )
            connection.commit()
            return LedgerRevision(version=1, parent_version=0, state_sha256=digest)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_current(
        self, connection: sqlite3.Connection
    ) -> tuple[dict[str, Any], LedgerRevision]:
        row = connection.execute(
            """
            SELECT lv.version, lv.parent_version, lv.state_json, lv.state_sha256
            FROM current_ledger cl
            JOIN ledger_versions lv ON lv.version = cl.version
            WHERE cl.singleton = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError(ErrorCode.LEDGER_CORRUPTION, "current ledger pointer is missing")
        text = row["state_json"]
        actual = sha256_text(text)
        if actual != row["state_sha256"]:
            raise BusinessError(
                ErrorCode.LEDGER_CORRUPTION,
                "ledger state checksum mismatch",
                {"version": row["version"], "expected": row["state_sha256"], "actual": actual},
            )
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                ErrorCode.LEDGER_CORRUPTION,
                "ledger state is not valid JSON",
                {"version": row["version"], "reason": str(exc)},
            ) from exc
        return state, LedgerRevision(
            version=row["version"],
            parent_version=row["parent_version"],
            state_sha256=row["state_sha256"],
        )

    def read(self) -> tuple[dict[str, Any], LedgerRevision]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            state, revision = self._read_current(connection)
            connection.commit()
            return state, revision
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(
        self,
        *,
        instance_id: str,
        round_id: str,
        source: str,
        request: Any,
        commit_kind: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
        crash_hook: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        """Execute one optimistic, serialized ledger transaction.

        BEGIN IMMEDIATE ensures that concurrent processes cannot both reserve
        the same node capacity.  The current pointer is replaced only after a
        complete immutable version row has been inserted.
        """
        digest = request_hash(request)
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(8):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                state, revision = self._read_current(connection)
                existing = connection.execute(
                    """
                    SELECT request_hash, ledger_version, result_json
                    FROM idempotency
                    WHERE instance_id = ? AND round_id = ? AND source = ?
                    """,
                    (instance_id, round_id, source),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != digest:
                        raise BusinessError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "same idempotency key was submitted with a different payload",
                            {
                                "instance_id": instance_id,
                                "round_id": round_id,
                                "source": source,
                            },
                        )
                    stored_version = existing["ledger_version"]
                    stored_row = connection.execute(
                        """
                        SELECT parent_version, state_sha256
                        FROM ledger_versions WHERE version = ?
                        """,
                        (stored_version,),
                    ).fetchone()
                    if stored_row is None:
                        raise BusinessError(
                            ErrorCode.LEDGER_CORRUPTION,
                            "idempotency entry points to a missing ledger version",
                            {"ledger_version": stored_version},
                        )
                    connection.commit()
                    stored_result = json.loads(existing["result_json"])
                    return ExecutionResult(
                        result=stored_result,
                        revision=LedgerRevision(
                            version=stored_version,
                            parent_version=stored_row["parent_version"],
                            state_sha256=stored_row["state_sha256"],
                        ),
                        applied=False,
                    )

                result = mutator(state)
                next_version = revision.version + 1
                state["generation"] = next_version
                text = canonical_json(state)
                state_digest = sha256_text(text)
                result = dict(result)
                result["ledger_revision"] = {
                    "version": next_version,
                    "parent_version": revision.version,
                    "state_sha256": state_digest,
                }
                result_text = canonical_json(result)

                connection.execute(
                    """
                    INSERT INTO ledger_versions
                        (version, parent_version, state_json, state_sha256, commit_kind)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (next_version, revision.version, text, state_digest, commit_kind),
                )
                if crash_hook is not None:
                    crash_hook("after_version_insert")
                changed = connection.execute(
                    """
                    UPDATE current_ledger SET version = ?
                    WHERE singleton = 1 AND version = ?
                    """,
                    (next_version, revision.version),
                )
                if changed.rowcount != 1:
                    raise BusinessError(
                        ErrorCode.LEDGER_CORRUPTION,
                        "current ledger version changed inside a serialized transaction",
                    )
                if crash_hook is not None:
                    crash_hook("after_pointer_swap")
                connection.execute(
                    """
                    INSERT INTO idempotency
                        (instance_id, round_id, source, request_hash, ledger_version, result_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (instance_id, round_id, source, digest, next_version, result_text),
                )
                connection.commit()
                return ExecutionResult(
                    result=result,
                    revision=LedgerRevision(next_version, revision.version, state_digest),
                    applied=True,
                )
            except sqlite3.OperationalError as exc:
                connection.rollback()
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.01 * (attempt + 1))
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        raise BusinessError(
            ErrorCode.INTERNAL_ERROR,
            "database remained locked after retries",
            {"reason": str(last_error) if last_error else "unknown"},
        )

