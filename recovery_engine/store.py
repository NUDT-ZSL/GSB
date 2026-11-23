"""SQLite 持久化层。

设计要点：
- 所有状态变更（服务阶段、已提交动作水位、流量阶梯、迁移决策、
  逻辑时钟水位）在单个事务内原子写入；
- BEGIN IMMEDIATE 保证多进程写串行化；committed_actions 主键约束
  + INSERT OR IGNORE 保证同一服务同一动作版本只被提交一次（幂等）；
- 读取方使用普通事务，只能看到完整已提交阶段；
- 文件锁（fcntl/msvcrt 双实现）保护计划指针整体切换。
"""

import contextlib
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_decisions (
    service TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    fallback_seconds INTEGER NOT NULL,
    fallback_abs_ms INTEGER NOT NULL,
    scale INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    time_source TEXT NOT NULL,
    PRIMARY KEY (service, clock_ms)
);
CREATE TABLE IF NOT EXISTS service_state (
    service TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    action_waterline INTEGER NOT NULL DEFAULT -1,
    ladder_rung INTEGER NOT NULL DEFAULT -1,
    updated_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    service TEXT NOT NULL,
    action_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (service, action_id)
);
CREATE TABLE IF NOT EXISTS committed_actions (
    service TEXT NOT NULL,
    action_id TEXT NOT NULL,
    version TEXT NOT NULL,
    seq INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (service, action_id, version)
);
CREATE TABLE IF NOT EXISTS conclusions (
    service TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (service, kind)
);
CREATE TABLE IF NOT EXISTS plan_versions (
    version INTEGER PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    plan_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
"""


class FileLock:
    """跨平台文件锁：Unix 用 fcntl，Windows 用 msvcrt。"""

    def __init__(self, path):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "a+b")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
        return False


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock_path = db_path + ".lock"
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def transaction(self):
        """原子写事务：BEGIN IMMEDIATE，异常即整体回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._active_tx = conn
            try:
                yield conn
            finally:
                self._active_tx = None
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def read(self):
        """只读事务：只能看到完整已提交阶段。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write_conn(self):
        """若已在外层事务中则复用该连接，否则自开事务。"""
        tx = getattr(self, "_active_tx", None)
        if tx is not None:
            yield tx
            return
        with self.transaction() as conn:
            yield conn

    # ---- 逻辑时钟水位 ----
    def get_clock_waterline(self):
        with self.read() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='clock_waterline'").fetchone()
            return None if row is None else json.loads(row["value"])

    def set_clock_waterline(self, conn, value):
        conn.execute(
            "INSERT INTO kv(key, value) VALUES('clock_waterline', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(value),))

    # ---- 迁移决策 ----
    def get_migration(self, service, clock_ms):
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM migration_decisions WHERE service=? AND clock_ms=?",
                (service, clock_ms)).fetchone()
            return None if row is None else dict(row)

    def put_migration(self, service, clock_ms, fallback_seconds, fallback_abs_ms,
                      scale, rounding, time_source):
        with self._write_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO migration_decisions"
                "(service, clock_ms, fallback_seconds, fallback_abs_ms, scale, rounding, time_source)"
                " VALUES(?,?,?,?,?,?,?)",
                (service, clock_ms, fallback_seconds, fallback_abs_ms,
                 scale, rounding, time_source))

    # ---- 服务阶段与水位 ----
    def get_service_state(self, service):
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM service_state WHERE service=?", (service,)).fetchone()
            return None if row is None else dict(row)

    def upsert_service_state(self, conn, service, phase, action_waterline,
                             ladder_rung, clock_ms):
        conn.execute(
            "INSERT INTO service_state(service, phase, action_waterline, ladder_rung, updated_clock_ms)"
            " VALUES(?,?,?,?,?) "
            "ON CONFLICT(service) DO UPDATE SET phase=excluded.phase,"
            " action_waterline=excluded.action_waterline,"
            " ladder_rung=excluded.ladder_rung,"
            " updated_clock_ms=excluded.updated_clock_ms",
            (service, phase, action_waterline, ladder_rung, clock_ms))

    # ---- 动作提交（幂等） ----
    def mark_pending(self, conn, service, action_id, seq):
        conn.execute(
            "INSERT OR IGNORE INTO pending_actions(service, action_id, seq) VALUES(?,?,?)",
            (service, action_id, seq))

    def commit_action(self, conn, service, action_id, version, seq, clock_ms, result):
        """提交动作；返回 True 表示本次新提交，False 表示幂等命中。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO committed_actions"
            "(service, action_id, version, seq, clock_ms, result) VALUES(?,?,?,?,?,?)",
            (service, action_id, version, seq, clock_ms, result))
        conn.execute(
            "DELETE FROM pending_actions WHERE service=? AND action_id=?",
            (service, action_id))
        return cur.rowcount == 1

    def committed_actions(self, service=None):
        with self.read() as conn:
            if service is None:
                rows = conn.execute(
                    "SELECT * FROM committed_actions ORDER BY service, seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM committed_actions WHERE service=? ORDER BY seq",
                    (service,)).fetchall()
            return [dict(r) for r in rows]

    def pending_actions(self):
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM pending_actions ORDER BY service, seq").fetchall()
            return [dict(r) for r in rows]

    def clear_pending(self, conn, service=None):
        if service is None:
            conn.execute("DELETE FROM pending_actions")
        else:
            conn.execute("DELETE FROM pending_actions WHERE service=?", (service,))

    # ---- 结论（切换/回切裁决结果，供复用） ----
    def save_conclusion(self, conn, service, kind, payload):
        conn.execute(
            "INSERT INTO conclusions(service, kind, payload_json) VALUES(?,?,?) "
            "ON CONFLICT(service, kind) DO UPDATE SET payload_json=excluded.payload_json",
            (service, kind, json.dumps(payload, sort_keys=True, ensure_ascii=False)))

    def get_conclusion(self, service, kind):
        with self.read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM conclusions WHERE service=? AND kind=?",
                (service, kind)).fetchone()
            return None if row is None else row["payload_json"]

    def all_conclusions(self):
        with self.read() as conn:
            rows = conn.execute(
                "SELECT service, kind, payload_json FROM conclusions ORDER BY service, kind"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_conclusions(self, conn, services):
        for name in services:
            conn.execute("DELETE FROM conclusions WHERE service=?", (name,))

    # ---- 计划版本指针 ----
    def get_plan_pointer(self):
        with self.read() as conn:
            row = conn.execute("SELECT version FROM plan_pointer WHERE id=1").fetchone()
            return None if row is None else row["version"]

    def get_plan_version(self, version):
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM plan_versions WHERE version=?", (version,)).fetchone()
            return None if row is None else dict(row)

    def current_plan(self):
        version = self.get_plan_pointer()
        if version is None:
            return None
        return self.get_plan_version(version)

    def stage_and_switch_plan(self, version, manifest_json, plan_json):
        """整体替换：暂存新版本行后原子切换计划指针，而非逐条覆盖。"""
        with FileLock(self.lock_path):
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO plan_versions(version, manifest_json, plan_json) VALUES(?,?,?)",
                    (version, manifest_json, plan_json))
                conn.execute(
                    "INSERT INTO plan_pointer(id, version) VALUES(1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                    (version,))
