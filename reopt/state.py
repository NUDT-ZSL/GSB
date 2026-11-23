"""持久化状态：SQLite 事务原子写入 + 文件锁的多进程一致性。

批次水位、已提交批次摘要、当前执行形态版本、漂移触发记录、精度敏感标记
与逻辑时钟水位都在单事务内原子提交；形态切换以版本号整体替换形态指针并
拒绝版本回退；同一批次同一形态版本只提交一次，重复提交幂等；读取方只能
看到完整已提交批次。
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager

from .errors import E_BATCH_CONFLICT, E_FORM_VERSION_REGRESSION, E_STATE_NOT_LOADED, err

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    seq INTEGER NOT NULL,
    form_version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    table_digests TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    committed INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (seq, form_version)
);
CREATE TABLE IF NOT EXISTS observations (
    seq INTEGER NOT NULL,
    step_idx INTEGER NOT NULL,
    form_version INTEGER NOT NULL,
    left_rows INTEGER NOT NULL,
    right_rows INTEGER NOT NULL,
    output_rows INTEGER NOT NULL,
    sel_fp INTEGER NOT NULL,
    PRIMARY KEY (seq, step_idx, form_version)
);
CREATE TABLE IF NOT EXISTS drift_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    step_idx INTEGER NOT NULL,
    source_table TEXT NOT NULL,
    join_key TEXT NOT NULL,
    ratio_fp INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    threshold_fp INTEGER NOT NULL,
    margin_fp INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    form_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    chosen_json TEXT NOT NULL,
    rejected_json TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS precision_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    tables_json TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    steps_json TEXT NOT NULL,
    error_bound_fp INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    context TEXT NOT NULL,
    op TEXT NOT NULL,
    scale INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    discarded INTEGER NOT NULL,
    error_bound INTEGER NOT NULL
);
"""


class StateStore:
    """单个状态目录对应一个 SQLite 库与一把文件锁。"""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.db_path = os.path.join(state_dir, "state.db")
        self.lock_path = os.path.join(state_dir, "state.lock")
        os.makedirs(state_dir, exist_ok=True)
        self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()
        os.close(self._lock_fd)

    @contextmanager
    def transaction(self):
        """文件锁 + BEGIN IMMEDIATE：跨进程互斥的原子写单元。"""
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    # ---- 元信息 ----
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, conn, key: str, value: str) -> None:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    def require_loaded(self) -> None:
        if self.get_meta("loaded") != "1":
            raise err(E_STATE_NOT_LOADED, "状态目录尚未 load，拒绝运行")

    # ---- 水位与形态 ----
    def watermark(self) -> int:
        return int(self.get_meta("watermark", "0"))

    def logical_clock(self) -> int:
        return int(self.get_meta("logical_clock", "0"))

    def form_version(self) -> int:
        return int(self.get_meta("form_version", "0"))

    def current_form(self) -> dict:
        return json.loads(self.get_meta("form_json", "{}"))

    # ---- 批次提交（幂等） ----
    def commit_batch(self, conn, seq: int, form_version: int, digest: str,
                     table_digests: dict, rows: dict, result_digest: str,
                     clock_ms: int) -> bool:
        """提交批次；同 (seq, form_version) 重复提交且内容一致时幂等返回 False。"""
        existing = conn.execute(
            "SELECT digest, table_digests, rows_json, result_digest FROM batches "
            "WHERE seq=? AND form_version=?", (seq, form_version)).fetchone()
        payload = (digest, json.dumps(table_digests, sort_keys=True),
                   json.dumps(rows, sort_keys=True), result_digest)
        if existing is not None:
            if tuple(existing) == payload:
                return False
            raise err(E_BATCH_CONFLICT,
                      f"批次 {seq} 形态版本 {form_version} 重复提交但内容不一致")
        conn.execute(
            "INSERT INTO batches(seq, form_version, digest, table_digests, rows_json,"
            " result_digest, clock_ms, committed) VALUES(?,?,?,?,?,?,?,1)",
            (seq, form_version, *payload, clock_ms))
        # 水位 = 自首个剧本批次起的连续已提交前缀，保证并发下不出现空洞
        first = int(self.get_meta("first_seq", "1"))
        seqs = [r[0] for r in conn.execute(
            "SELECT DISTINCT seq FROM batches WHERE committed=1 ORDER BY seq")]
        wm = first - 1
        for committed_seq in seqs:
            if committed_seq == wm + 1:
                wm = committed_seq
            else:
                break
        self.set_meta(conn, "watermark", str(wm))
        if clock_ms > self.logical_clock():
            self.set_meta(conn, "logical_clock", str(clock_ms))
        return True

    def committed_batches(self) -> list[dict]:
        """读取方只能看到完整已提交批次。"""
        rows = self.conn.execute(
            "SELECT seq, form_version, digest, table_digests, rows_json,"
            " result_digest, clock_ms FROM batches WHERE committed=1"
            " ORDER BY seq").fetchall()
        return [{"seq": r[0], "form_version": r[1], "digest": r[2],
                 "table_digests": json.loads(r[3]), "rows": json.loads(r[4]),
                 "result_digest": r[5], "clock_ms": r[6]} for r in rows]

    # ---- 观测（幂等） ----
    def record_observation(self, conn, seq: int, step_idx: int, form_version: int,
                           left_rows: int, right_rows: int, output_rows: int,
                           sel_fp: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO observations(seq, step_idx, form_version,"
            " left_rows, right_rows, output_rows, sel_fp)"
            " VALUES(?,?,?,?,?,?,?)",
            (seq, step_idx, form_version, left_rows, right_rows, output_rows, sel_fp))

    def observations(self, form_version: int | None = None) -> list[dict]:
        sql = ("SELECT seq, step_idx, form_version, left_rows, right_rows,"
               " output_rows, sel_fp FROM observations")
        args: tuple = ()
        if form_version is not None:
            sql += " WHERE form_version=?"
            args = (form_version,)
        sql += " ORDER BY seq, step_idx"
        return [{"seq": r[0], "step_idx": r[1], "form_version": r[2],
                 "left_rows": r[3], "right_rows": r[4], "output_rows": r[5],
                 "sel_fp": r[6]}
                for r in self.conn.execute(sql, args).fetchall()]

    # ---- 漂移触发 ----
    def record_trigger(self, conn, seq: int, verdict, clock_ms: int,
                       form_version: int) -> None:
        conn.execute(
            "INSERT INTO drift_triggers(seq, step_idx, source_table, join_key,"
            " ratio_fp, window_start, window_end, threshold_fp, margin_fp,"
            " clock_ms, form_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (seq, verdict.step_idx, verdict.source_table, verdict.join_key,
             verdict.ratio_fp, verdict.window_start, verdict.window_end,
             verdict.threshold_fp, verdict.margin_fp, clock_ms, form_version))

    def triggers(self) -> list[dict]:
        cols = ("id", "seq", "step_idx", "source_table", "join_key", "ratio_fp",
                "window_start", "window_end", "threshold_fp", "margin_fp",
                "clock_ms", "form_version")
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM drift_triggers ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def last_trigger_clock(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(clock_ms) FROM drift_triggers").fetchone()
        return row[0] if row and row[0] is not None else None

    # ---- 形态切换 ----
    def switch_form(self, conn, new_version: int, form_json: dict, seq: int,
                    chosen: dict, rejected: list, clock_ms: int) -> None:
        """以版本号整体替换当前形态指针；拒绝版本回退。"""
        current = self.form_version()
        if new_version <= current:
            raise err(E_FORM_VERSION_REGRESSION,
                      f"形态版本回退被拒绝: {new_version} <= {current}")
        conn.execute(
            "INSERT INTO switches(from_version, to_version, seq, chosen_json,"
            " rejected_json, clock_ms) VALUES(?,?,?,?,?,?)",
            (current, new_version, seq, json.dumps(chosen, sort_keys=True),
             json.dumps(rejected, sort_keys=True), clock_ms))
        self.set_meta(conn, "form_version", str(new_version))
        self.set_meta(conn, "form_json", json.dumps(form_json, sort_keys=True))

    def switches(self) -> list[dict]:
        cols = ("id", "from_version", "to_version", "seq", "chosen_json",
                "rejected_json", "clock_ms")
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM switches ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ---- 精度敏感标记 ----
    def record_precision_flag(self, conn, seq: int, tables: list[str],
                              window_start: int, window_end: int,
                              steps: list[dict], error_bound_fp: int) -> None:
        conn.execute(
            "INSERT INTO precision_flags(seq, tables_json, window_start, window_end,"
            " steps_json, error_bound_fp) VALUES(?,?,?,?,?,?)",
            (seq, json.dumps(tables), window_start, window_end,
             json.dumps(steps), error_bound_fp))

    def precision_flags(self) -> list[dict]:
        cols = ("id", "seq", "tables_json", "window_start", "window_end",
                "steps_json", "error_bound_fp")
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM precision_flags ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ---- 误差账本 ----
    def record_ledger(self, conn, seq: int, entries) -> None:
        conn.executemany(
            "INSERT INTO ledger_entries(seq, context, op, scale, rounding,"
            " discarded, error_bound) VALUES(?,?,?,?,?,?,?)",
            [(seq, e.context, e.op, e.scale, e.rounding, e.discarded, e.error_bound)
             for e in entries])

    def ledger_entries(self) -> list[dict]:
        cols = ("id", "seq", "context", "op", "scale", "rounding", "discarded",
                "error_bound")
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM ledger_entries ORDER BY id").fetchall()
        return [dict(zip(cols, r)) for r in rows]
