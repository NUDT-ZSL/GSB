from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .canonical import canonical_dumps
from .errors import DiagnosticError, ErrorCode


def default_ledger(run_id: str, policy: str) -> dict[str, Any]:
    return {
        "schema": "diagnostic-ledger-v1",
        "run_id": run_id,
        "policy": policy,
        "status": "initialized",
        "ledger_version": 0,
        "clock_watermark": 0,
        "seq_watermark": 0,
        "completed_seqs": [],
        "pending": None,
        "pending_operations": {},
        "cache": {},
        "orphans": [],
        "dirty": [],
        "findings": [],
        "snapshot_decision": None,
        "clock_fingerprint": None,
        "applied_log_entries": [],
        "replay_watermark": {
            "log_name": "metadata-audit.jsonl",
            "byte_offset": 0,
            "entry_count": 0,
            "last_entry_id": None,
            "last_committed_seq": 0,
        },
        "log_outcomes": [],
        "counts": {
            "events": 0,
            "double_success": 0,
            "orphan_object": 0,
            "dirty_cache": 0,
            "log_complete": 0,
            "log_half_written": 0,
            "log_lost": 0,
            "interrupted": 0,
            "recovered": 0,
            "duplicate_skips": 0,
        },
    }


def stabilize_document(document: dict[str, Any]) -> dict[str, Any]:
    """Sort unordered collections so canonical ledger JSON stays reproducible."""
    if isinstance(document.get("findings"), list):
        document["findings"].sort(
            key=lambda item: (
                int(item.get("event_seq") or item.get("seq") or 0),
                str(item.get("object_key") or ""),
                str(item.get("policy") or ""),
                str(item.get("worker_id") or ""),
            )
        )
    for field_name, id_field in (("orphans", "orphan_id"), ("dirty", "dirty_id")):
        if isinstance(document.get(field_name), list):
            document[field_name].sort(key=lambda item: str(item.get(id_field) or ""))
    if isinstance(document.get("completed_seqs"), list):
        document["completed_seqs"] = sorted(set(document["completed_seqs"]))
    if isinstance(document.get("applied_log_entries"), list):
        document["applied_log_entries"] = sorted(set(document["applied_log_entries"]))
    if isinstance(document.get("log_outcomes"), list):
        document["log_outcomes"].sort(key=lambda item: canonical_dumps(item))
    return document


class Ledger:
    def __init__(self, state_dir: str | Path, run_id: str, policy: str, worker_id: str | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.run_id = run_id
        self.policy = policy
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "ledger.sqlite"
        self.lock_path = self.state_dir / "ledger.lock"
        self.objects_dir = self.state_dir / "objects"
        self.tmp_dir = self.state_dir / "tmp"
        self.logs_dir = self.state_dir / "logs"
        self.evidence_dir = self.state_dir / "evidence"
        for directory in (self.objects_dir, self.tmp_dir, self.logs_dir, self.evidence_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.logs_dir / "metadata-audit.jsonl"
        self.log_lock_path = self.logs_dir / "metadata-audit.lock"
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        return connection

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _initialize_database(self) -> None:
        with self._file_lock(self.lock_path):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ledger_pointer (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        version INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        policy TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        updated_clock INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ledger_revisions (
                        version INTEGER PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        policy TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        created_clock INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS object_claims (
                        object_key TEXT NOT NULL,
                        content_version INTEGER NOT NULL,
                        claim_scope TEXT NOT NULL,
                        event_seq INTEGER NOT NULL,
                        policy TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        logical_clock INTEGER NOT NULL,
                        expected_signature TEXT NOT NULL,
                        PRIMARY KEY (object_key, content_version, claim_scope)
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_completions (
                        event_seq INTEGER PRIMARY KEY,
                        object_key TEXT NOT NULL,
                        content_version INTEGER NOT NULL,
                        policy TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        verdict TEXT NOT NULL,
                        logical_clock INTEGER NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS replay_leases (
                        log_name TEXT PRIMARY KEY,
                        worker_id TEXT NOT NULL,
                        byte_offset INTEGER NOT NULL,
                        updated_clock INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute("SELECT 1 FROM ledger_pointer WHERE id=1").fetchone()
                if row is None:
                    document = default_ledger(self.run_id, self.policy)
                    payload = canonical_dumps(document)
                    connection.execute(
                        "INSERT INTO ledger_pointer(id, version, run_id, policy, document_json, updated_clock) "
                        "VALUES (1, ?, ?, ?, ?, 0)",
                        (document["ledger_version"], self.run_id, self.policy, payload),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def read(self) -> dict[str, Any]:
        with self._file_lock(self.lock_path):
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT document_json FROM ledger_pointer WHERE id=1"
                ).fetchone()
                if row is None:
                    raise DiagnosticError(ErrorCode.LEDGER_ERROR, "ledger pointer is missing")
                return json.loads(row["document_json"])
            finally:
                connection.close()

    def update(
        self,
        mutator: Callable[[dict[str, Any], sqlite3.Connection], Any],
        *,
        logical_clock: int = 0,
    ) -> Any:
        """Run a SQLite transaction and atomically replace the whole pointer."""
        with self._file_lock(self.lock_path):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT document_json FROM ledger_pointer WHERE id=1"
                ).fetchone()
                if row is None:
                    document = default_ledger(self.run_id, self.policy)
                else:
                    document = json.loads(row["document_json"])
                result = mutator(document, connection)
                stabilize_document(document)
                document["ledger_version"] = int(document.get("ledger_version", 0)) + 1
                document["updated_logical_clock"] = logical_clock
                payload = canonical_dumps(document)
                version = document["ledger_version"]
                connection.execute(
                    "INSERT OR REPLACE INTO ledger_revisions"
                    "(version, run_id, policy, document_json, created_clock) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version, self.run_id, self.policy, payload, logical_clock),
                )
                connection.execute(
                    "INSERT INTO ledger_pointer(id, version, run_id, policy, document_json, updated_clock) "
                    "VALUES (1, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
                    "run_id=excluded.run_id, policy=excluded.policy, "
                    "document_json=excluded.document_json, updated_clock=excluded.updated_clock",
                    (version, self.run_id, self.policy, payload, logical_clock),
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def claim_object_version(
        self,
        connection: sqlite3.Connection,
        *,
        object_key: str,
        version: int,
        event_seq: int,
        logical_clock: int,
        expected_signature: str,
        claim_scope: str = "diagnostic-once",
    ) -> tuple[bool, dict[str, Any] | None]:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO object_claims(
                object_key, content_version, claim_scope, event_seq, policy,
                worker_id, logical_clock, expected_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_key,
                version,
                claim_scope,
                event_seq,
                self.policy,
                self.worker_id,
                logical_clock,
                expected_signature,
            ),
        )
        if cursor.rowcount == 1:
            return True, None
        row = connection.execute(
            "SELECT * FROM object_claims WHERE object_key=? AND content_version=? AND claim_scope=?",
            (object_key, version, claim_scope),
        ).fetchone()
        return False, dict(row) if row is not None else None

    def complete_event_once(
        self,
        connection: sqlite3.Connection,
        *,
        event: dict[str, Any],
        verdict: str,
    ) -> bool:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO event_completions"
            "(event_seq, object_key, content_version, policy, worker_id, verdict, logical_clock) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event["seq"],
                event["key"],
                event["content_version"],
                self.policy,
                self.worker_id,
                verdict,
                event["logical_clock"],
            ),
        )
        return cursor.rowcount == 1

    def acquire_replay_lease(
        self,
        connection: sqlite3.Connection,
        *,
        log_name: str,
        byte_offset: int,
        logical_clock: int,
    ) -> bool:
        connection.execute(
            "INSERT INTO replay_leases(log_name, worker_id, byte_offset, updated_clock) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(log_name) DO UPDATE SET worker_id=excluded.worker_id, "
            "byte_offset=excluded.byte_offset, updated_clock=excluded.updated_clock",
            (log_name, self.worker_id, byte_offset, logical_clock),
        )
        return True

    def verify_invariants(self) -> dict[str, Any]:
        document = self.read()
        connection = self._connect()
        try:
            duplicate_claims = connection.execute(
                "SELECT object_key, content_version, COUNT(*) AS count "
                "FROM object_claims GROUP BY object_key, content_version, claim_scope HAVING count > 1"
            ).fetchall()
            completed_rows = connection.execute(
                "SELECT event_seq, COUNT(*) AS count FROM event_completions GROUP BY event_seq HAVING count > 1"
            ).fetchall()
            pointer = connection.execute("SELECT version, document_json FROM ledger_pointer WHERE id=1").fetchone()
            canonical_payload = canonical_dumps(document)
            pointer_matches = pointer is not None and pointer["document_json"] == canonical_payload
            return {
                "ok": not duplicate_claims and not completed_rows and pointer_matches,
                "pointer_matches_canonical_document": bool(pointer_matches),
                "duplicate_claim_groups": [dict(row) for row in duplicate_claims],
                "duplicate_completed_event_groups": [dict(row) for row in completed_rows],
                "ledger_version": document.get("ledger_version"),
                "revision_count": connection.execute("SELECT COUNT(*) AS n FROM ledger_revisions").fetchone()["n"],
            }
        finally:
            connection.close()
