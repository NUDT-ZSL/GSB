"""SQLite 元数据层。

已提交对象水位、分片提交状态、元数据版本、缓存条目版本与逻辑时钟水位
都在单事务内原子写入。WAL + BEGIN IMMEDIATE 保证多进程串行化提交。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import E_BUDGET_EXHAUSTED, E_CLOCK_REGRESSION, StoreError

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  digest TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  state TEXT NOT NULL,
  clock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
  bucket TEXT NOT NULL,
  object_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  state TEXT NOT NULL,
  commit_clock INTEGER,
  chunk_count INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  PRIMARY KEY (bucket, object_key, version)
);
CREATE TABLE IF NOT EXISTS object_chunks (
  bucket TEXT NOT NULL,
  object_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  digest TEXT NOT NULL,
  size INTEGER NOT NULL,
  PRIMARY KEY (bucket, object_key, version, seq)
);
CREATE TABLE IF NOT EXISTS bucket_meta (
  bucket TEXT NOT NULL,
  mkey TEXT NOT NULL,
  mvalue TEXT NOT NULL,
  mtype TEXT NOT NULL,
  clock INTEGER NOT NULL,
  meta_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (bucket, mkey)
);
CREATE TABLE IF NOT EXISTS cache_entries (
  bucket TEXT NOT NULL,
  mkey TEXT NOT NULL,
  occupancy_fixed INTEGER NOT NULL,
  occupancy_decimal TEXT NOT NULL,
  last_clock INTEGER NOT NULL,
  entry_version INTEGER NOT NULL,
  PRIMARY KEY (bucket, mkey)
);
CREATE TABLE IF NOT EXISTS clock_watermark (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  clock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window INTEGER NOT NULL,
  amount_fixed INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path, clock_step_ms: int, regression_threshold_ms: int):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock_step_ms = clock_step_ms
        self.regression_threshold_ms = regression_threshold_ms
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self._init_with_retry()

    def _init_with_retry(self, attempts: int = 200, interval: float = 0.05):
        """多进程同时建库时 journal_mode 切换可能立即返回 BUSY，退避重试。"""
        for attempt in range(attempts):
            try:
                mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA busy_timeout=30000")
                self.conn.executescript(SCHEMA)
                self.conn.execute(
                    "INSERT OR IGNORE INTO clock_watermark(id, clock) VALUES (1, 0)")
                return
            except sqlite3.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(interval)

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE 事务：提交串行化，异常回滚。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def watermark(self) -> int:
        row = self.conn.execute(
            "SELECT clock FROM clock_watermark WHERE id = 1").fetchone()
        return row[0]

    def tick_clock(self, provided_ms=None) -> int:
        """推进逻辑时钟（须在事务内调用）。回拨超阈值抛 E_CLOCK_REGRESSION。"""
        current = self.watermark()
        if provided_ms is not None and provided_ms < current - self.regression_threshold_ms:
            raise StoreError(
                E_CLOCK_REGRESSION, "logical clock regression beyond threshold",
                provided_ms=provided_ms, watermark=current,
                threshold_ms=self.regression_threshold_ms)
        new = max(current + self.clock_step_ms, provided_ms or 0)
        self.conn.execute("UPDATE clock_watermark SET clock = ? WHERE id = 1", (new,))
        return new

    def reserve_budget(self, window: int, amount_fixed: int, cap_fixed: int):
        """在事务内为读取预留延迟预算；并发窗口内总量超上限则拒绝。"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_fixed), 0) FROM budget_leases WHERE window = ?",
            (window,)).fetchone()
        used = row[0]
        if used + amount_fixed > cap_fixed:
            raise StoreError(
                E_BUDGET_EXHAUSTED, "latency budget exhausted in current window",
                window=window, used_fixed=used,
                requested_fixed=amount_fixed, cap_fixed=cap_fixed)
        cur = self.conn.execute(
            "INSERT INTO budget_leases(window, amount_fixed) VALUES (?, ?)",
            (window, amount_fixed))
        return cur.lastrowid

    def release_budget(self, lease_id: int):
        with self.tx() as conn:
            conn.execute("DELETE FROM budget_leases WHERE id = ?", (lease_id,))
