"""SQLite persistence, file locking and mode-version bookkeeping."""

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3

from .config import QueueConfig
from .errors import DQError, ErrorCode

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    label TEXT PRIMARY KEY,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    fire_ms INTEGER NOT NULL,
    policy_label TEXT NOT NULL,
    max_retries INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_fire_ms INTEGER NOT NULL,
    last_error_code TEXT,
    dead_reason TEXT,
    version INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due
    ON tasks(stage, next_fire_ms);
CREATE TABLE IF NOT EXISTS pulls (
    pull_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    consumer_id TEXT,
    mode_version INTEGER NOT NULL,
    incarnation INTEGER NOT NULL,
    fire_ms INTEGER NOT NULL,
    lease_deadline_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    error_code TEXT,
    created_ms INTEGER NOT NULL,
    committed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pulls_task ON pulls(task_id);
CREATE INDEX IF NOT EXISTS idx_pulls_open
    ON pulls(status, lease_deadline_ms);
CREATE TABLE IF NOT EXISTS mode_versions (
    version INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    config_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mode_switch_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    from_mode TEXT NOT NULL,
    to_mode TEXT NOT NULL,
    now_ms INTEGER NOT NULL,
    before_n INTEGER NOT NULL,
    after_n INTEGER NOT NULL,
    span_ms INTEGER NOT NULL,
    heap_cost REAL NOT NULL,
    wheel_cost REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS op_journal (
    journal_id TEXT PRIMARY KEY,
    op_type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    done_ms INTEGER
);
CREATE TABLE IF NOT EXISTS manifest_snapshot (
    task_id TEXT PRIMARY KEY,
    ord INTEGER NOT NULL,
    digest TEXT NOT NULL,
    fire_ms INTEGER NOT NULL
);
"""


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    """Owns the SQLite connection and the cross-process lock file."""

    def __init__(self, db_path, config=None):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.lock_path = self.db_path + ".lock"
        self.config = config or QueueConfig()
        self._lock_fh = open(self.lock_path, "a+")
        self.conn = sqlite3.connect(
            self.db_path, timeout=30, isolation_level=None,
            check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()
        self._lock_fh.close()

    @contextlib.contextmanager
    def process_lock(self):
        """Exclusive cross-process lock for scheduling/ack critical sections."""
        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- meta ----------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def get_int_meta(self, key, default=0):
        raw = self.get_meta(key)
        return int(raw) if raw is not None else default

    # ---- mode versions -------------------------------------------------
    def current_mode_version(self):
        row = self.conn.execute(
            "SELECT mv.* FROM mode_versions mv JOIN meta m "
            "ON m.value=mv.version WHERE m.key='current_mode_version'"
        ).fetchone()
        if row is None:
            return None
        return {
            "version": row["version"], "mode": row["mode"],
            "config": json.loads(row["config_json"]),
            "reason": row["reason"], "created_ms": row["created_ms"],
        }

    def install_mode(self, mode, now_ms, reason, explicit_version=None,
                     config=None):
        """Insert a new mode version and atomically swap the pointer."""
        current = self.current_mode_version()
        next_version = (current["version"] + 1) if current else 1
        if explicit_version is not None:
            if explicit_version < next_version:
                raise DQError(
                    ErrorCode.E_MODE_VERSION_ROLLBACK,
                    "requested mode version is older than the current one",
                    requested=explicit_version, current=next_version - 1)
            next_version = explicit_version
        cfg_dict = (config or self.config).to_dict()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO mode_versions(version, mode, config_json, "
                "reason, created_ms) VALUES(?, ?, ?, ?, ?)",
                (next_version, mode, json.dumps(cfg_dict, sort_keys=True),
                 reason, now_ms))
            self.set_meta("current_mode_version", next_version)
        return next_version

    def log_switch(self, entry):
        with self.transaction():
            self.conn.execute(
                "INSERT INTO mode_switch_log(version, from_mode, to_mode, "
                "now_ms, before_n, after_n, span_ms, heap_cost, wheel_cost, "
                "reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (entry["version"], entry["from_mode"], entry["to_mode"],
                 entry["now_ms"], entry["before_n"], entry["after_n"],
                 entry["span_ms"], entry["costs"]["heap"],
                 entry["costs"]["wheel"], entry["reason"]))

    def switch_log(self):
        rows = self.conn.execute(
            "SELECT * FROM mode_switch_log ORDER BY seq").fetchall()
        return [dict(row) for row in rows]

    # ---- clock high water ----------------------------------------------
    def high_water(self):
        return self.get_int_meta("high_water_ms", 0)

    def save_high_water(self, now_ms):
        with self.transaction():
            self.set_meta("high_water_ms", now_ms)

    # ---- journal --------------------------------------------------------
    def journal_pending(self, journal_id, op_type, detail, now_ms):
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO op_journal(journal_id, op_type, "
                "status, detail, created_ms) VALUES(?, ?, 'PENDING', ?, ?)",
                (journal_id, op_type, json.dumps(detail, sort_keys=True),
                 now_ms))

    def journal_done(self, journal_id, now_ms):
        self.conn.execute(
            "UPDATE op_journal SET status='DONE', done_ms=? "
            "WHERE journal_id=?", (now_ms, journal_id))

    def pending_journals(self):
        rows = self.conn.execute(
            "SELECT * FROM op_journal WHERE status='PENDING' "
            "ORDER BY created_ms, journal_id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out
