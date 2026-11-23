"""Versioned SQLite persistence with atomic commits, locks and crash recovery.

All durable state lives in one SQLite database guarded by an advisory file
lock.  Snapshot application uses temporary staging tables and a version pointer
swap inside a single transaction so readers always see a fully consistent
generation.  Unique constraints make identical patch / verification replays
idempotent, and an in-progress jobs table lets ``resume`` replay only the work
that never reached a committed terminal state.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .errors import DiagError

SCHEMA_VERSION = 1


class FileLock:
    """Portable whole-file advisory lock using ``fcntl`` when available."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except ModuleNotFoundError:  # pragma: no cover - Windows fallback
            # MSVC lacks fcntl; rely on SQLite busy handling plus a lock marker.
            while os.path.exists(self.path.with_suffix(".lockmarker")):
                pass
            self.path.with_suffix(".lockmarker").write_text(str(os.getpid()))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except ModuleNotFoundError:  # pragma: no cover
            marker = self.path.with_suffix(".lockmarker")
            if os.path.exists(marker):
                marker.unlink()
        self._fh.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    version INTEGER PRIMARY KEY,
    patch_hash TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    logical_clock_tick INTEGER NOT NULL,
    clock_high_water_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('committed', 'superseded')),
    created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS anchors (
    version INTEGER NOT NULL,
    alias TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (version, alias)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'committed')),
    result TEXT,
    UNIQUE (job_type, request_hash)
);
CREATE TABLE IF NOT EXISTS chain_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    purpose TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (hostname, purpose, generation)
);
CREATE TABLE IF NOT EXISTS revocation_conclusions (
    fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL,
    tombstone_signature TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (fingerprint, generation, tombstone_signature)
);
CREATE TABLE IF NOT EXISTS precision_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS precision_sensitive (
    comparison_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (comparison_id, version)
);
"""


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_suffix(".lock")
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Another process is switching journal mode; the shared database
            # still works and busy_timeout governs the actual write contention.
            pass
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        stored = None
        if exists:
            stored = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if stored is None:
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('current_version', '')")
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('clock_high_water_ms', '')")
            stored = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        elif int(stored[0]) != SCHEMA_VERSION:
            raise DiagError("E3003", f"schema {stored[0]} != supported {SCHEMA_VERSION}")

    def lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def close(self) -> None:
        self.conn.close()

    # ----- version pointer / snapshots ------------------------------------
    def current_version(self) -> int | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='current_version'").fetchone()
        return int(row[0]) if row and row[0] != "" else None

    def find_committed_patch(self, patch_hash: str) -> int | None:
        row = self.conn.execute("SELECT version FROM snapshots WHERE patch_hash=? AND status='committed'", (patch_hash,)).fetchone()
        return row[0] if row else None

    def commit_snapshot(
        self,
        version: int,
        patch_hash: str,
        content_hash: str,
        logical_clock_tick: int,
        clock_high_water_ms: int,
        anchor_payloads: dict[str, dict],
        ledger_entries: list[dict],
    ) -> dict:
        """Atomically stage and publish a snapshot; reject rollback/replay.

        SQLite ``BEGIN IMMEDIATE`` plus ``busy_timeout`` serialises concurrent
        writers.  After a busy timeout the loop retries and re-detects an
        already committed identical patch, turning the race into idempotency.
        """
        for attempt in range(30):
            current = self.current_version()
            if current is not None and version < current:
                raise DiagError("E3001", f"proposed {version} < current {current}")
            existing = self.find_committed_patch(patch_hash)
            if existing is not None:
                return {"action": "idempotent_replay", "version": existing}
            if self.conn.execute("SELECT 1 FROM snapshots WHERE version=?", (version,)).fetchone():
                raise DiagError("E3001", f"version {version} already exists with a different patch")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                seq_row = self.conn.execute("SELECT COALESCE(MAX(created_seq), 0) + 1 FROM snapshots").fetchone()
                self.conn.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, 'committed', ?)",
                    (version, patch_hash, content_hash, logical_clock_tick, clock_high_water_ms, seq_row[0]),
                )
                if current is not None:
                    self.conn.execute("UPDATE snapshots SET status='superseded' WHERE version=?", (current,))
                self.conn.execute("CREATE TEMP TABLE stage_anchors(alias TEXT, payload TEXT)")
                self.conn.executemany(
                    "INSERT INTO stage_anchors VALUES (?, ?)",
                    [(alias, json.dumps(payload, sort_keys=True)) for alias, payload in sorted(anchor_payloads.items())],
                )
                self.conn.execute(
                    "INSERT INTO anchors(version, alias, payload) SELECT ?, alias, payload FROM stage_anchors",
                    (version,),
                )
                self.conn.execute("DROP TABLE stage_anchors")
                for entry in ledger_entries:
                    entry_hash = canonical_hash(entry)
                    self.conn.execute(
                        "INSERT OR IGNORE INTO precision_ledger(version, entry_hash, payload) VALUES (?, ?, ?)",
                        (version, entry_hash, json.dumps(entry, sort_keys=True)),
                    )
                self.conn.execute("UPDATE meta SET value=? WHERE key='current_version'", (str(version),))
                self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='clock_high_water_ms'",
                    (str(clock_high_water_ms),),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('clock_high_water_ms', ?)",
                    (str(clock_high_water_ms),),
                )
                self.conn.execute("COMMIT")
                return {"action": "committed", "version": version}
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                with contextlib.suppress(sqlite3.OperationalError):
                    self.conn.execute("ROLLBACK")
                with contextlib.suppress(sqlite3.OperationalError):
                    self.conn.execute("DROP TABLE IF EXISTS stage_anchors")
                already = self.find_committed_patch(patch_hash)
                if already is not None:
                    return {"action": "idempotent_replay", "version": already}
                if "locked" in str(exc).lower() and attempt < 29:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        raise DiagError("E3003", "snapshot commit exhausted retries under contention")

    def load_anchors(self, version: int | None = None) -> dict[str, dict]:
        version = version if version is not None else self.current_version()
        if version is None:
            return {}
        rows = self.conn.execute("SELECT alias, payload FROM anchors WHERE version=? ORDER BY alias", (version,)).fetchall()
        return {alias: json.loads(payload) for alias, payload in rows}

    # ----- jobs for crash recovery ----------------------------------------
    def start_job(self, job_id: str, job_type: str, version: int, request_hash: str, request: dict) -> bool:
        """Return True if this caller owns the job, False if already committed."""
        with self.lock():
            committed = self.conn.execute(
                "SELECT result FROM jobs WHERE job_type=? AND request_hash=? AND status='committed'",
                (job_type, request_hash),
            ).fetchone()
            if committed:
                return False
            started = self.conn.execute(
                "SELECT 1 FROM jobs WHERE job_type=? AND request_hash=? AND status='started'",
                (job_type, request_hash),
            ).fetchone()
            if started:
                # A previous process was killed before completion; this caller
                # takes ownership and must recompute and finish the job.
                return True
            self.conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, job_type, version, request_hash, status) VALUES (?, ?, ?, ?, 'started')",
                (job_id, job_type, version, request_hash),
            )
            return True

    def complete_job(self, job_id: str, result: dict) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='committed', result=? WHERE job_id=?",
            (json.dumps(result, sort_keys=True), job_id),
        )

    def complete_request(self, job_type: str, request_hash: str, result: dict) -> None:
        """Finish any row matching a request, including one orphaned by a crash."""
        self.conn.execute(
            "UPDATE jobs SET status='committed', result=? WHERE job_type=? AND request_hash=?",
            (json.dumps(result, sort_keys=True), job_type, request_hash),
        )

    def unfinished_jobs(self, job_type: str | None = None) -> list[dict]:
        sql = "SELECT job_id, job_type, version, request_hash FROM jobs WHERE status='started'"
        params: tuple = ()
        if job_type:
            sql += " AND job_type=?"
            params = (job_type,)
        return [
            {"job_id": r[0], "job_type": r[1], "version": r[2], "request_hash": r[3]}
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def job_result(self, request_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT result FROM jobs WHERE request_hash=? AND status='committed'", (request_hash,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    # ----- conclusions -----------------------------------------------------
    def save_chain_conclusion(self, version: int, hostname: str, purpose: str, generation: int, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO chain_conclusions(version, hostname, purpose, generation, payload) VALUES (?, ?, ?, ?, ?)",
            (version, hostname, purpose, generation, json.dumps(payload, sort_keys=True)),
        )

    def chain_conclusions(self, version: int | None = None) -> list[dict]:
        version = version if version is not None else self.current_version()
        rows = self.conn.execute(
            "SELECT hostname, purpose, generation, payload FROM chain_conclusions WHERE version=? "
            "ORDER BY hostname, purpose, generation",
            (version,),
        ).fetchall()
        return [
            {"hostname": h, "purpose": p, "generation": g, **json.loads(payload)}
            for h, p, g, payload in rows
        ]

    def save_revocation(self, verdict: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO revocation_conclusions VALUES (?, ?, ?, ?)",
            (
                verdict["fingerprint_sha256"],
                verdict["generation"],
                verdict["tombstone_signature"],
                json.dumps(verdict, sort_keys=True),
            ),
        )

    def revocation_conclusions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload FROM revocation_conclusions ORDER BY fingerprint, generation, tombstone_signature"
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_sensitive(self, version: int, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO precision_sensitive VALUES (?, ?, ?)",
            (payload["comparison_id"], version, json.dumps(payload, sort_keys=True)),
        )

    def sensitive_records(self, version: int | None = None) -> list[dict]:
        version = version if version is not None else self.current_version()
        rows = self.conn.execute(
            "SELECT payload FROM precision_sensitive WHERE version=? ORDER BY comparison_id", (version,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def ledger_entries(self, version: int | None = None) -> list[dict]:
        version = version if version is not None else self.current_version()
        rows = self.conn.execute(
            "SELECT payload FROM precision_ledger WHERE version=? ORDER BY id", (version,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def clock_high_water(self) -> int | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='clock_high_water_ms'").fetchone()
        return int(row[0]) if row else None
