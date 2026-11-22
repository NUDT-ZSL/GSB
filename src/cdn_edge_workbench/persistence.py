from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, ErrorCode, SimulatedCrash
from .models import CacheEntry
from .validation import canonical_json, sha256_hex


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    updated_run TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_versions (
    version INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    checksum TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    snapshot_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_seq TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_journal (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    planned_version INTEGER NOT NULL,
    plan_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    result_json TEXT,
    result_checksum TEXT,
    created_seq TEXT NOT NULL,
    updated_seq TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    path TEXT NOT NULL,
    node TEXT,
    stage TEXT NOT NULL,
    injection_seq INTEGER,
    cache_generation INTEGER,
    code TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS audit_run_committed ON audit (run_id, committed);

CREATE TABLE IF NOT EXISTS arbitrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, decision_hash)
);

CREATE TABLE IF NOT EXISTS evictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    cache_generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, node, path)
);

CREATE TABLE IF NOT EXISTS retention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    cache_generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, node, path)
);

CREATE TABLE IF NOT EXISTS consistency_receipts (
    receipt_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    cache_generation INTEGER NOT NULL,
    result_hash TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS replacement_ledger (
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    cache_generation INTEGER NOT NULL,
    from_generation INTEGER NOT NULL,
    result_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    committed_seq TEXT NOT NULL,
    PRIMARY KEY (node, path, cache_generation)
);

CREATE TABLE IF NOT EXISTS origin_watermarks (
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    last_logical_clock INTEGER NOT NULL,
    last_status TEXT NOT NULL,
    last_origin_etag TEXT,
    last_result_hash TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node, path)
);

CREATE TABLE IF NOT EXISTS watermarks (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    logical_clock INTEGER NOT NULL,
    index_version INTEGER NOT NULL,
    last_run_id TEXT NOT NULL
);
"""


def _seq() -> str:
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}"


def _validate_version(version: int) -> None:
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise BusinessError(
            ErrorCode.INVALID_ARGUMENT,
            "index version must be a positive integer",
            details={"version": version},
        )


def index_table_name(version: int) -> str:
    _validate_version(version)
    return f"index_entries_v{version}"


def _entry_checksum(entries: list[CacheEntry]) -> str:
    return sha256_hex(
        canonical_json(
            [
                entry.candidate_dict()
                for entry in sorted(entries, key=lambda item: (item.node, item.path))
            ]
        )
    )


@contextlib.contextmanager
def state_process_lock(state_dir: str | Path, timeout_seconds: float = 30.0) -> Iterator[None]:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".workbench.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BusinessError(
                        ErrorCode.CONCURRENT_MODIFICATION,
                        "timed out waiting for the state process lock",
                        details={"lock": str(lock_path), "timeout_seconds": timeout_seconds},
                    )
                time.sleep(0.025)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.database_path = self.state_dir / "workbench.sqlite3"

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_info(key, value) VALUES ('schema_version', '1')"
            )

    def current_pointer(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM index_pointer WHERE id=1").fetchone()
            return dict(row) if row is not None else None

    def current_version(self) -> int:
        pointer = self.current_pointer()
        return int(pointer["version"]) if pointer else 0

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_journal WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if result.get("result_json"):
                result["result"] = json.loads(result["result_json"])
            return result

    def logical_watermark(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute("SELECT logical_clock FROM watermarks WHERE id=1").fetchone()
            return int(row["logical_clock"]) if row is not None else None

    def record_plan(
        self,
        *,
        run_id: str,
        base_version: int,
        planned_version: int,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_version(planned_version)
        plan_json = canonical_json(plan)
        plan_hash = sha256_hex(plan_json)
        now = _seq()
        with self.connect() as connection:
            with self.transaction(connection):
                pointer = connection.execute(
                    "SELECT version FROM index_pointer WHERE id=1"
                ).fetchone()
                current = int(pointer["version"]) if pointer else 0
                if base_version != current:
                    raise BusinessError(
                        ErrorCode.CONCURRENT_MODIFICATION,
                        "index pointer changed while planning the run",
                        details={"expected_base_version": base_version, "current": current},
                    )
                if planned_version <= current:
                    raise BusinessError(
                        ErrorCode.INDEX_VERSION_ROLLBACK,
                        "planned index version would roll back the committed pointer",
                        details={"current": current, "planned": planned_version},
                    )
                existing = connection.execute(
                    "SELECT * FROM run_journal WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    existing_dict = dict(existing)
                    if existing_dict["plan_hash"] != plan_hash:
                        raise BusinessError(
                            ErrorCode.RUN_ID_CONFLICT,
                            "run_id already exists with a different plan",
                            details={"run_id": run_id},
                        )
                    if existing_dict["status"] == "committed":
                        return existing_dict
                    if existing_dict["status"] in {"planned", "prepared"}:
                        return existing_dict
                connection.execute(
                    """
                    INSERT INTO run_journal(
                        run_id, status, base_version, planned_version, plan_hash,
                        plan_json, result_json, result_checksum, created_seq, updated_seq
                    ) VALUES (?, 'planned', ?, ?, ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        status='planned',
                        base_version=excluded.base_version,
                        planned_version=excluded.planned_version,
                        plan_hash=excluded.plan_hash,
                        plan_json=excluded.plan_json,
                        result_json=NULL,
                        result_checksum=NULL,
                        updated_seq=excluded.updated_seq
                    """,
                    (
                        run_id,
                        base_version,
                        planned_version,
                        plan_hash,
                        plan_json,
                        now,
                        now,
                    ),
                )
                return {
                    "run_id": run_id,
                    "status": "planned",
                    "base_version": base_version,
                    "planned_version": planned_version,
                    "plan_hash": plan_hash,
                }

    def prepare_result(self, result: dict[str, Any]) -> str:
        run_id = result["run_id"]
        checksum_payload = {key: value for key, value in result.items() if key != "result_checksum"}
        result_checksum = sha256_hex(canonical_json(checksum_payload))
        result["result_checksum"] = result_checksum
        result_json = canonical_json(result)
        now = _seq()
        with self.connect() as connection:
            with self.transaction(connection):
                journal = connection.execute(
                    "SELECT * FROM run_journal WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if journal is None:
                    raise BusinessError(
                        ErrorCode.RECOVERY_INCOMPLETE,
                        "cannot prepare a result without a planned run",
                        details={"run_id": run_id},
                    )
                journal_dict = dict(journal)
                if journal_dict["status"] == "committed":
                    return journal_dict["result_checksum"]
                if journal_dict.get("result_checksum") == result_checksum:
                    return result_checksum
                if journal_dict.get("result_json") and journal_dict["result_checksum"] != result_checksum:
                    raise BusinessError(
                        ErrorCode.RUN_ID_CONFLICT,
                        "run already has a different prepared result",
                        details={"run_id": run_id},
                    )
                expected_plan_hash = journal_dict["plan_hash"]
                if result.get("plan_hash") != expected_plan_hash:
                    raise BusinessError(
                        ErrorCode.RUN_ID_CONFLICT,
                        "prepared result does not belong to the planned payload",
                        details={"run_id": run_id},
                    )
                for table in ("audit", "arbitrations", "evictions", "retention"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE run_id=? AND committed=0",
                        (run_id,),
                    )
                for index, audit in enumerate(result.get("audits", []), start=1):
                    seq = int(audit.get("seq", index))
                    connection.execute(
                        """
                        INSERT INTO audit(
                            run_id, seq, path, node, stage, injection_seq,
                            cache_generation, code, type, message, details_json, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            run_id,
                            seq,
                            audit["path"],
                            audit.get("node"),
                            audit["stage"],
                            audit.get("injection_seq"),
                            audit.get("cache_generation"),
                            audit["code"],
                            audit["type"],
                            audit["message"],
                            canonical_json(audit.get("details", {})),
                        ),
                    )
                for arbitration in result.get("arbitrations", []):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO arbitrations(
                            run_id, decision_hash, scope, target, status, payload_json, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            run_id,
                            arbitration["decision_hash"],
                            arbitration["scope"],
                            arbitration["target"],
                            arbitration["status"],
                            canonical_json(arbitration),
                        ),
                    )
                for eviction in result.get("evictions", []):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO evictions(
                            run_id, node, path, cache_generation, reason, payload_json, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            run_id,
                            eviction["node"],
                            eviction["path"],
                            int(eviction["cache_generation"]),
                            eviction["reason"],
                            canonical_json(eviction),
                        ),
                    )
                for retained in result.get("retention", []):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO retention(
                            run_id, node, path, cache_generation, reason, payload_json, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            run_id,
                            retained["node"],
                            retained["path"],
                            int(retained["cache_generation"]),
                            retained["reason"],
                            canonical_json(retained),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE run_journal SET
                        status='prepared', result_json=?, result_checksum=?, updated_seq=?
                    WHERE run_id=?
                    """,
                    (result_json, result_checksum, now, run_id),
                )
        return result_checksum

    def _version_table_exists(
        self,
        connection: sqlite3.Connection,
        table_name: str,
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def apply_prepared(
        self,
        run_id: str,
        *,
        fault_point: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal = connection.execute(
                    "SELECT * FROM run_journal WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if journal is None:
                    raise BusinessError(
                        ErrorCode.RECOVERY_INCOMPLETE,
                        f"run journal not found: {run_id}",
                        details={"run_id": run_id},
                    )
                journal_dict = dict(journal)
                if journal_dict["status"] == "committed":
                    connection.execute("COMMIT")
                    return json.loads(journal_dict["result_json"])
                if journal_dict["status"] != "prepared" or not journal_dict.get("result_json"):
                    raise BusinessError(
                        ErrorCode.RECOVERY_INCOMPLETE,
                        "run has no complete prepared result to apply",
                        details={"run_id": run_id, "status": journal_dict["status"]},
                    )
                result = json.loads(journal_dict["result_json"])
                planned_version = int(journal_dict["planned_version"])
                base_version = int(journal_dict["base_version"])
                pointer_row = connection.execute(
                    "SELECT * FROM index_pointer WHERE id=1"
                ).fetchone()
                current_version = int(pointer_row["version"]) if pointer_row else 0
                if current_version != base_version:
                    raise BusinessError(
                        ErrorCode.CONCURRENT_MODIFICATION,
                        "committed index changed while the run was prepared",
                        details={
                            "run_id": run_id,
                            "base_version": base_version,
                            "current_version": current_version,
                        },
                    )
                if planned_version != current_version + 1:
                    raise BusinessError(
                        ErrorCode.INDEX_VERSION_ROLLBACK,
                        "planned index version is not the next committed version",
                        details={
                            "current_version": current_version,
                            "planned_version": planned_version,
                        },
                    )
                watermarks = connection.execute(
                    "SELECT * FROM watermarks WHERE id=1"
                ).fetchone()
                old_clock = int(watermarks["logical_clock"]) if watermarks else None
                new_clock = int(result["logical_clock"])
                tolerance = int(result.get("clock_rollback_tolerance_ticks", 0))
                if old_clock is not None and new_clock < old_clock - tolerance:
                    raise BusinessError(
                        ErrorCode.CLOCK_ROLLBACK,
                        "logical clock rollback exceeds the configured tolerance",
                        details={
                            "last_logical_clock": old_clock,
                            "new_logical_clock": new_clock,
                            "tolerance_ticks": tolerance,
                        },
                    )
                entries = [
                    CacheEntry.from_mapping(item) for item in result.get("entries", [])
                ]
                checksum = _entry_checksum(entries)
                table_name = index_table_name(planned_version)
                if self._version_table_exists(connection, table_name):
                    existing_version = connection.execute(
                        "SELECT run_id, checksum FROM index_versions WHERE version=?",
                        (planned_version,),
                    ).fetchone()
                    if existing_version is None:
                        connection.execute(f'DROP TABLE "{table_name}"')
                    elif existing_version["run_id"] == run_id and existing_version["checksum"] == checksum:
                        connection.execute("COMMIT")
                        return result
                    else:
                        raise BusinessError(
                            ErrorCode.INDEX_VERSION_ROLLBACK,
                            "target index version table already belongs to another run",
                            details={"version": planned_version, "run_id": run_id},
                        )
                stage_name = f"stage_entries_{uuid.uuid4().hex}"
                connection.execute(
                    f"""
                    CREATE TABLE "{stage_name}" (
                        node TEXT NOT NULL,
                        path TEXT NOT NULL,
                        digest_algorithm TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        content_version INTEGER NOT NULL,
                        etag TEXT NOT NULL,
                        last_verified_clock INTEGER NOT NULL,
                        expires_at_clock INTEGER NOT NULL,
                        cache_generation INTEGER NOT NULL,
                        source_version TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        raw_time_json TEXT NOT NULL,
                        effective_ttl_seconds INTEGER NOT NULL,
                        cacheable INTEGER NOT NULL,
                        weak_etag INTEGER NOT NULL,
                        PRIMARY KEY (node, path)
                    )
                    """
                )
                for entry in sorted(entries, key=lambda item: (item.node, item.path)):
                    connection.execute(
                        f'INSERT INTO "{stage_name}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        entry.row(),
                    )
                if fault_point == "during_apply":
                    raise SimulatedCrash("simulated crash after staging index entries")
                connection.execute(f'ALTER TABLE "{stage_name}" RENAME TO "{table_name}"')
                now = _seq()
                connection.execute(
                    """
                    INSERT INTO index_versions(
                        version, run_id, checksum, entry_count, logical_clock,
                        snapshot_generation, status, created_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, 'committed', ?)
                    """,
                    (
                        planned_version,
                        run_id,
                        checksum,
                        len(entries),
                        new_clock,
                        int(result["snapshot_generation"]),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO index_pointer(id, version, checksum, entry_count, updated_run)
                    VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        version=excluded.version,
                        checksum=excluded.checksum,
                        entry_count=excluded.entry_count,
                        updated_run=excluded.updated_run
                    """,
                    (planned_version, checksum, len(entries), run_id),
                )
                for replacement in result.get("replacements", []):
                    result_hash = replacement["result_hash"]
                    existing = connection.execute(
                        """
                        SELECT result_hash FROM replacement_ledger
                        WHERE node=? AND path=? AND cache_generation=?
                        """,
                        (
                            replacement["node"],
                            replacement["path"],
                            int(replacement["cache_generation"]),
                        ),
                    ).fetchone()
                    if existing is not None:
                        if existing["result_hash"] != result_hash:
                            raise BusinessError(
                                ErrorCode.CONCURRENT_MODIFICATION,
                                "the same node/path/cache generation was already replaced with different content",
                                details={
                                    "node": replacement["node"],
                                    "path": replacement["path"],
                                    "cache_generation": replacement["cache_generation"],
                                },
                            )
                        continue
                    connection.execute(
                        """
                        INSERT INTO replacement_ledger(
                            node, path, cache_generation, from_generation,
                            result_hash, run_id, committed_seq
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            replacement["node"],
                            replacement["path"],
                            int(replacement["cache_generation"]),
                            int(replacement["from_generation"]),
                            result_hash,
                            run_id,
                            now,
                        ),
                    )
                for receipt in result.get("receipts", []):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO consistency_receipts(
                            receipt_key, run_id, node, path, cache_generation,
                            result_hash, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            receipt["receipt_key"],
                            run_id,
                            receipt["node"],
                            receipt["path"],
                            int(receipt["cache_generation"]),
                            receipt["result_hash"],
                        ),
                    )
                for watermark in result.get("origin_watermarks", []):
                    connection.execute(
                        """
                        INSERT INTO origin_watermarks(
                            node, path, last_logical_clock, last_status,
                            last_origin_etag, last_result_hash, last_run_id, committed
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(node, path) DO UPDATE SET
                            last_logical_clock=excluded.last_logical_clock,
                            last_status=excluded.last_status,
                            last_origin_etag=excluded.last_origin_etag,
                            last_result_hash=excluded.last_result_hash,
                            last_run_id=excluded.last_run_id,
                            committed=1
                        WHERE excluded.last_logical_clock >= origin_watermarks.last_logical_clock
                        """,
                        (
                            watermark["node"],
                            watermark["path"],
                            int(watermark["last_logical_clock"]),
                            watermark["last_status"],
                            watermark.get("last_origin_etag"),
                            watermark["last_result_hash"],
                            run_id,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO watermarks(id, logical_clock, index_version, last_run_id)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        logical_clock=excluded.logical_clock,
                        index_version=excluded.index_version,
                        last_run_id=excluded.last_run_id
                    """,
                    (new_clock, planned_version, run_id),
                )
                for table in ("audit", "arbitrations", "evictions", "retention"):
                    connection.execute(
                        f"UPDATE {table} SET committed=1 WHERE run_id=?",
                        (run_id,),
                    )
                connection.execute(
                    """
                    UPDATE run_journal SET status='committed', updated_seq=?
                    WHERE run_id=?
                    """,
                    (_seq(), run_id),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def read_committed_snapshot(self) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                pointer = connection.execute(
                    "SELECT * FROM index_pointer WHERE id=1"
                ).fetchone()
                if pointer is None:
                    connection.execute("COMMIT")
                    return {
                        "version": 0,
                        "checksum": None,
                        "entry_count": 0,
                        "entries": [],
                        "audits": [],
                        "arbitrations": [],
                        "evictions": [],
                        "retention": [],
                        "origin_watermarks": [],
                        "logical_clock": None,
                    }
                version = int(pointer["version"])
                version_row = connection.execute(
                    "SELECT * FROM index_versions WHERE version=? AND status='committed'",
                    (version,),
                ).fetchone()
                if version_row is None:
                    raise BusinessError(
                        ErrorCode.STATE_CORRUPT,
                        "index pointer references a missing committed version",
                        details={"version": version},
                    )
                table_name = index_table_name(version)
                if not self._version_table_exists(connection, table_name):
                    raise BusinessError(
                        ErrorCode.STATE_CORRUPT,
                        "committed index version table is missing",
                        details={"version": version, "table": table_name},
                    )
                rows = connection.execute(
                    f'SELECT * FROM "{table_name}" ORDER BY node, path'
                ).fetchall()
                entries = [self._entry_from_row(row) for row in rows]
                audits = [
                    self._audit_from_row(row)
                    for row in connection.execute(
                        "SELECT * FROM audit WHERE committed=1 ORDER BY run_id, seq"
                    )
                ]
                arbitrations = [
                    json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT * FROM arbitrations WHERE committed=1 ORDER BY run_id, scope, target"
                    )
                ]
                evictions = [
                    json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT * FROM evictions WHERE committed=1 ORDER BY run_id, node, path"
                    )
                ]
                retention = [
                    json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT * FROM retention WHERE committed=1 ORDER BY run_id, node, path"
                    )
                ]
                origin_watermarks = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM origin_watermarks WHERE committed=1 ORDER BY node, path"
                    )
                ]
                watermark = connection.execute(
                    "SELECT * FROM watermarks WHERE id=1"
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "version": version,
                    "checksum": pointer["checksum"],
                    "entry_count": pointer["entry_count"],
                    "index_version_row": dict(version_row),
                    "entries": [entry.candidate_dict() for entry in entries],
                    "audits": audits,
                    "arbitrations": arbitrations,
                    "evictions": evictions,
                    "retention": retention,
                    "origin_watermarks": origin_watermarks,
                    "logical_clock": int(watermark["logical_clock"]) if watermark else None,
                }
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _entry_from_row(self, row: sqlite3.Row) -> CacheEntry:
        return CacheEntry(
            node=row["node"],
            path=row["path"],
            digest_algorithm=row["digest_algorithm"],
            content_digest=row["content_digest"],
            content_version=int(row["content_version"]),
            etag=row["etag"],
            last_verified_clock=int(row["last_verified_clock"]),
            expires_at_clock=int(row["expires_at_clock"]),
            cache_generation=int(row["cache_generation"]),
            source_version=row["source_version"],
            source_ref=row["source_ref"],
            raw_time=json.loads(row["raw_time_json"]),
            effective_ttl_seconds=int(row["effective_ttl_seconds"]),
            cacheable=bool(row["cacheable"]),
            weak_etag=bool(row["weak_etag"]),
        )

    def _audit_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "seq": int(row["seq"]),
            "path": row["path"],
            "node": row["node"],
            "stage": row["stage"],
            "injection_seq": row["injection_seq"],
            "cache_generation": row["cache_generation"],
            "code": row["code"],
            "type": row["type"],
            "message": row["message"],
            "details": json.loads(row["details_json"]),
        }

    def audit_records(
        self,
        *,
        run_id: str | None = None,
        path: str | None = None,
        node: str | None = None,
        stage: str | None = None,
        committed: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["committed=?"]
        values: list[Any] = [1 if committed else 0]
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        if path is not None:
            clauses.append("path=?")
            values.append(path)
        if node is not None:
            clauses.append("node=?")
            values.append(node)
        if stage is not None:
            clauses.append("stage=?")
            values.append(stage)
        query = (
            "SELECT * FROM audit WHERE "
            + " AND ".join(clauses)
            + " ORDER BY run_id, seq"
        )
        with self.connect() as connection:
            return [
                self._audit_from_row(row)
                for row in connection.execute(query, values)
            ]

    def pending_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, status, base_version, planned_version,
                       result_checksum, created_seq, updated_seq
                FROM run_journal
                WHERE status IN ('planned', 'prepared')
                ORDER BY created_seq
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def _drop_orphan_tables(self, connection: sqlite3.Connection) -> list[str]:
        dropped: list[str] = []
        known_versions = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM index_versions")
        }
        pointer = connection.execute("SELECT version FROM index_pointer WHERE id=1").fetchone()
        if pointer is not None:
            known_versions.add(int(pointer["version"]))
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for row in rows:
            name = row["name"]
            if name.startswith("stage_entries_"):
                connection.execute(f'DROP TABLE "{name}"')
                dropped.append(name)
                continue
            if name.startswith("index_entries_v"):
                try:
                    version = int(name.rsplit("_v", 1)[1])
                except ValueError:
                    continue
                if version not in known_versions:
                    connection.execute(f'DROP TABLE "{name}"')
                    dropped.append(name)
        return dropped

    def recover_locked(self) -> dict[str, Any]:
        self.initialize()
        actions: list[dict[str, Any]] = []
        with self.connect() as connection:
            with self.transaction(connection):
                dropped = self._drop_orphan_tables(connection)
                if dropped:
                    actions.append({"action": "drop_orphan_tables", "tables": dropped})
                half_rows = connection.execute(
                    "SELECT run_id, COUNT(*) AS count FROM audit WHERE committed=0 GROUP BY run_id"
                ).fetchall()
                if half_rows:
                    actions.append(
                        {
                            "action": "detected_uncommitted_audit",
                            "runs": {
                                row["run_id"]: int(row["count"])
                                for row in half_rows
                            },
                        }
                    )
        for pending in self.pending_runs():
            if pending["status"] == "prepared":
                result = self.apply_prepared(pending["run_id"])
                actions.append(
                    {
                        "action": "replayed_prepared_run",
                        "run_id": pending["run_id"],
                        "index_version": result.get("planned_version"),
                    }
                )
            else:
                actions.append(
                    {
                        "action": "requires_replan",
                        "run_id": pending["run_id"],
                        "reason": "journal contains a plan but no prepared result",
                    }
                )
        with self.connect() as connection:
            pointer = connection.execute("SELECT * FROM index_pointer WHERE id=1").fetchone()
            if pointer is not None:
                table_name = index_table_name(int(pointer["version"]))
                if not self._version_table_exists(connection, table_name):
                    raise BusinessError(
                        ErrorCode.STATE_CORRUPT,
                        "recovery found a dangling index pointer",
                        details={"version": pointer["version"]},
                    )
            with self.transaction(connection):
                connection.execute(
                    """
                    UPDATE audit SET committed=1
                    WHERE committed=0 AND run_id IN (
                        SELECT run_id FROM run_journal WHERE status='committed'
                    )
                    """
                )
                connection.execute(
                    """
                    DELETE FROM audit
                    WHERE committed=0 AND run_id IN (
                        SELECT run_id FROM run_journal WHERE status IN ('aborted', 'failed')
                    )
                    """
                )
        snapshot = self.read_committed_snapshot()
        return {
            "actions": actions,
            "pending_runs": self.pending_runs(),
            "committed_version": snapshot["version"],
            "entry_count": len(snapshot["entries"]),
            "audit_count": len(snapshot["audits"]),
        }

    def recover(self) -> dict[str, Any]:
        with state_process_lock(self.state_dir):
            return self.recover_locked()

    def set_uncommitted_for_test(self, run_id: str, committed: int) -> None:
        with self.connect() as connection:
            with self.transaction(connection):
                connection.execute(
                    "UPDATE audit SET committed=? WHERE run_id=?",
                    (committed, run_id),
                )

    def inspect(self) -> dict[str, Any]:
        self.initialize()
        snapshot = self.read_committed_snapshot()
        with self.connect() as connection:
            pending = [
                dict(row)
                for row in connection.execute(
                    "SELECT run_id, status, base_version, planned_version FROM run_journal ORDER BY created_seq"
                )
            ]
            uncommitted_audit_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM audit WHERE committed=0"
                ).fetchone()["count"]
            )
            replacement_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM replacement_ledger"
                ).fetchone()["count"]
            )
        snapshot["runs"] = pending
        snapshot["uncommitted_audit_count"] = uncommitted_audit_count
        snapshot["replacement_ledger_count"] = replacement_count
        return snapshot

