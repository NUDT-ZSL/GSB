from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import OpVerifyError
from .hashing import canonical_bytes, sha256_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    trigger_revision TEXT NOT NULL,
    change_plan_json TEXT NOT NULL,
    ledger_checksum TEXT NOT NULL,
    outputs_checksum TEXT NOT NULL,
    parameters_checksum TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'opverify'
);
CREATE TABLE IF NOT EXISTS ledger_jobs (
    ledger_version INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    row_checksum TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (ledger_version, job_id)
);
CREATE TABLE IF NOT EXISTS job_heads (
    job_id TEXT PRIMARY KEY,
    ledger_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    row_checksum TEXT NOT NULL,
    row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_commits (
    batch_id TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, target_version, job_id, stage)
);
"""


class CommitLock:
    def __init__(self, fd: int, path: Path) -> None:
        self.fd = fd
        self.path = path
        self.released = False

    @classmethod
    def acquire(cls, path: str | Path) -> "CommitLock":
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise OpVerifyError(
                "E_STAGE_BUSY",
                "another ledger commit is active; late writer rejected without waiting",
                field="$.ledger_commit",
                details={"lock_path": str(lock_path)},
            ) from exc
        return cls(fd, lock_path)

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


class LedgerStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "ledger.sqlite3"
        self.lock_path = self.root / "ledger.commit.lock"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=0.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('current_version', '0')"
            )

    @contextmanager
    def exclusive_commit(self) -> Iterator[CommitLock]:
        lock = CommitLock.acquire(self.lock_path)
        try:
            yield lock
        finally:
            lock.release()

    def current_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='current_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def set_data_dir(self, data_dir: str | Path | None) -> None:
        if data_dir is None:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('data_dir',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(data_dir),),
            )

    def data_dir(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='data_dir'").fetchone()
        return row["value"] if row else None

    def get_head(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT row_json FROM job_heads WHERE job_id=?", (job_id,)
            ).fetchone()
        return json.loads(row["row_json"]) if row else None

    def heads(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT row_json FROM job_heads ORDER BY job_id").fetchall()
        return {row_obj["job_id"]: row_obj for row_obj in (json.loads(row["row_json"]) for row in rows)}

    def stage_commit(
        self, batch_id: str, target_version: int, job_id: str, stage: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json,digest FROM stage_commits "
                "WHERE batch_id=? AND target_version=? AND job_id=? AND stage=?",
                (batch_id, target_version, job_id, stage),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["digest"] = row["digest"]
        return payload

    def record_stage_commit(
        self,
        *,
        batch_id: str,
        target_version: int,
        job_id: str,
        stage: str,
        digest: str,
        payload: dict[str, Any],
    ) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO stage_commits"
                    "(batch_id,target_version,job_id,stage,digest,payload_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, target_version, job_id, stage, digest, payload_json),
                )
                conn.commit()
        except sqlite3.OperationalError as exc:
            raise OpVerifyError(
                "E_STAGE_BUSY", "stage commit ledger is busy; commit rejected"
            ) from exc

    def commit_version(
        self,
        *,
        version: int,
        parent_version: int,
        batch_id: str,
        trigger_revision: dict[str, Any],
        change_plan: dict[str, Any],
        snapshot_checksums: dict[str, str],
        job_rows: list[dict[str, Any]],
    ) -> None:
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise OpVerifyError(
                    "E_STAGE_BUSY", "SQLite ledger is busy; commit rejected"
                ) from exc
            current = int(
                conn.execute("SELECT value FROM meta WHERE key='current_version'").fetchone()["value"]
            )
            if current != parent_version:
                raise OpVerifyError(
                    "E_SNAPSHOT_ROLLBACK",
                    "ledger parent version is no longer current",
                    details={"expected_parent": parent_version, "current_version": current},
                )
            ledger_checksum = snapshot_checksums["ledger"]
            conn.execute(
                "INSERT INTO ledger_versions(version,parent_version,batch_id,trigger_revision,"
                "change_plan_json,ledger_checksum,outputs_checksum,parameters_checksum) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    version,
                    parent_version,
                    batch_id,
                    canonical_bytes(trigger_revision).decode("utf-8"),
                    canonical_bytes(change_plan).decode("utf-8"),
                    ledger_checksum,
                    snapshot_checksums["outputs"],
                    snapshot_checksums["parameters"],
                ),
            )
            for job_row in job_rows:
                job_id = job_row["job_id"]
                revision = int(job_row["revision"])
                row_checksum = job_row["row_checksum"]
                row_json = canonical_bytes(job_row).decode("utf-8")
                conn.execute(
                    "INSERT INTO ledger_jobs(ledger_version,job_id,revision,row_checksum,row_json) "
                    "VALUES(?,?,?,?,?)",
                    (version, job_id, revision, row_checksum, row_json),
                )
                conn.execute(
                    "INSERT INTO job_heads(job_id,ledger_version,revision,row_checksum,row_json) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                    "ledger_version=excluded.ledger_version,revision=excluded.revision,"
                    "row_checksum=excluded.row_checksum,row_json=excluded.row_json",
                    (job_id, version, revision, row_checksum, row_json),
                )
            conn.execute(
                "UPDATE meta SET value=? WHERE key='current_version'", (str(version),)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def version_record(self, version: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger_versions WHERE version=?", (version,)
            ).fetchone()
        if row is None:
            raise OpVerifyError("E_ARTIFACT_MISSING", f"ledger version {version} not found")
        result = dict(row)
        result["trigger_revision"] = json.loads(result["trigger_revision"])
        result["change_plan"] = json.loads(result["change_plan_json"])
        return result

    def latest_version_record(self) -> dict[str, Any] | None:
        version = self.current_version()
        if version == 0:
            return None
        return self.version_record(version)

    def rows_for_version(self, version: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT row_json FROM ledger_jobs WHERE ledger_version=? ORDER BY job_id",
                (version,),
            ).fetchall()
        return [json.loads(row["row_json"]) for row in rows]

    def verify_row_checksum(self, row: dict[str, Any]) -> bool:
        expected = row.get("row_checksum")
        payload = {key: value for key, value in row.items() if key != "row_checksum"}
        return expected == sha256_json(payload)
