"""SQLite 持久化：主表 / 批次表 / 冲突审计表 / 进度表 / 写审计表。

关键事务约束：
- 游标推进与批次数据写入在同一 SQLite 事务内提交；
- 主表写入、批次表、冲突审计表同事务，失败整体回滚，无半写数据；
- 主表以全局业务流水号 serial_no 为唯一主键，source 仅为来源追踪字段；
- 冲突按业务字段逐项登记，固定源提交顺序决定保留值。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from .generator import canonical_hash, merge_row

SOURCE_ORDER = ("rest", "ts", "csv")
MAIN_COLUMNS = ("serial_no", "amount", "customer", "event_ts", "detail_code", "source")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recon_main (
                serial_no   TEXT PRIMARY KEY,
                amount      REAL,
                customer    TEXT,
                event_ts    INTEGER,
                detail_code TEXT,
                source      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_audit (
                run_id      TEXT NOT NULL,
                source      TEXT NOT NULL,
                batch_seq   INTEGER NOT NULL,
                cursor_type TEXT NOT NULL,
                cursor_from TEXT,
                cursor_to   TEXT,
                row_count   INTEGER NOT NULL,
                inserted    INTEGER NOT NULL,
                updated     INTEGER NOT NULL,
                duplicates  INTEGER NOT NULL,
                conflicts   INTEGER NOT NULL,
                committed_at TEXT NOT NULL,
                records_json TEXT,
                UNIQUE(run_id, source, batch_seq)
            );
            CREATE TABLE IF NOT EXISTS conflict_audit (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT NOT NULL,
                source        TEXT NOT NULL,
                batch_seq     INTEGER NOT NULL,
                serial_no     TEXT NOT NULL,
                field         TEXT NOT NULL,
                old_value     TEXT,
                new_value     TEXT,
                winner_source TEXT NOT NULL,
                loser_source  TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS write_audit (
                run_id      TEXT NOT NULL,
                source      TEXT NOT NULL,
                batch_seq   INTEGER NOT NULL,
                written     INTEGER NOT NULL,
                conflicts   INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_progress (
                source       TEXT PRIMARY KEY,
                cursor_type  TEXT NOT NULL,
                cursor_value TEXT NOT NULL,
                committed_batches INTEGER NOT NULL,
                last_success_at TEXT,
                run_id       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_meta (
                run_id       TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                ended_at     TEXT,
                run_status   TEXT,
                resumed_from_run_id TEXT
            );
            """
        )

    # ------------------------------------------------------------- progress
    def get_progress(self, source: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM source_progress WHERE source=?", (source,)
        ).fetchone()
        return dict(row) if row else None

    def all_progress(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM source_progress").fetchall()
        return {r["source"]: dict(r) for r in rows}

    def last_aborted_run(self) -> dict | None:
        """最近一次未正常结束的运行（aborted 或强杀残留的 running）。"""
        row = self.conn.execute(
            "SELECT * FROM run_meta WHERE run_status IN ('aborted','running') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def start_run(self, run_id: str, resumed_from: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO run_meta(run_id, started_at, run_status, "
            "resumed_from_run_id) VALUES(?,?,?,?)",
            (run_id, utc_now_iso(), "running", resumed_from),
        )

    def finish_run(self, run_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE run_meta SET ended_at=?, run_status=? WHERE run_id=?",
            (utc_now_iso(), status, run_id),
        )

    def mark_aborted(self, run_id: str) -> None:
        self.conn.execute(
            "UPDATE run_meta SET ended_at=?, run_status='aborted' WHERE run_id=?",
            (utc_now_iso(), run_id),
        )

    # ------------------------------------------------------- atomic batch tx
    def commit_batch(self, *, run_id: str, source: str, batch_seq: int,
                     cursor_type: str, cursor_from: Any, cursor_to: Any,
                     records: list[dict], duplicate_serials: set[str],
                     failure_hook: Callable[[], None] | None = None) -> dict:
        """单事务：主表合并 + 批次表 + 冲突审计 + 写审计 + 进度推进。

        duplicate_serials：本批次在当前源内被去重丢弃的自然键集合（不入库）。
        failure_hook：测试钩子，在事务中途抛异常以验证整体回滚。
        """
        ts = utc_now_iso()
        cur = self.conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            inserted = updated = conflicts_count = 0
            already_present: set[str] = set()
            conflict_rows: list[dict] = []
            for record in records:
                serial = record["serial_no"]
                incoming = {k: record.get(k) for k in MAIN_COLUMNS if k != "source"}
                incoming["source"] = source
                existing_row = cur.execute(
                    "SELECT * FROM recon_main WHERE serial_no=?", (serial,)
                ).fetchone()
                if existing_row is None:
                    cur.execute(
                        "INSERT INTO recon_main(serial_no, amount, customer, "
                        "event_ts, detail_code, source) VALUES(?,?,?,?,?,?)",
                        (serial, incoming.get("amount"), incoming.get("customer"),
                         incoming.get("event_ts"), incoming.get("detail_code"), source),
                    )
                    inserted += 1
                else:
                    existing = dict(existing_row)
                    # 同一来源再次出现 -> 源内跨批次/跨页/边界重复，丢弃不改数据
                    if source in existing["source"].split("|"):
                        already_present.add(serial)
                        continue
                    merged, conflicts = merge_row(existing, incoming, SOURCE_ORDER)
                    cur.execute(
                        "UPDATE recon_main SET amount=?, customer=?, event_ts=?, "
                        "detail_code=?, source=? WHERE serial_no=?",
                        (merged.get("amount"), merged.get("customer"),
                         merged.get("event_ts"), merged.get("detail_code"),
                         merged["source"], serial),
                    )
                    updated += 1
                    for c in conflicts:
                        conflict_rows.append((
                            run_id, source, batch_seq, serial, c["field"],
                            json.dumps(c["old_value"], ensure_ascii=False),
                            json.dumps(c["new_value"], ensure_ascii=False),
                            existing["source"].split("|")[0], source, ts,
                        ))
                        conflicts_count += 1

            if failure_hook is not None:
                failure_hook()  # 测试注入：必须导致整轮回滚

            cur.executemany(
                "INSERT INTO conflict_audit(run_id, source, batch_seq, serial_no, "
                "field, old_value, new_value, winner_source, loser_source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                conflict_rows,
            )
            cur.execute(
                "INSERT INTO batch_audit(run_id, source, batch_seq, cursor_type, "
                "cursor_from, cursor_to, row_count, inserted, updated, duplicates, "
                "conflicts, committed_at, records_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, source, batch_seq, cursor_type,
                 json.dumps(cursor_from, ensure_ascii=False),
                 json.dumps(cursor_to, ensure_ascii=False),
                 len(records), inserted, updated,
                 len(duplicate_serials) + len(already_present),
                 conflicts_count, ts,
                 json.dumps(records, ensure_ascii=False)),
            )
            cur.execute(
                "INSERT INTO write_audit(run_id, source, batch_seq, written, "
                "conflicts, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, source, batch_seq, len(records), conflicts_count, ts),
            )
            cur.execute(
                "INSERT INTO source_progress(source, cursor_type, cursor_value, "
                "committed_batches, last_success_at, run_id) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET cursor_type=excluded.cursor_type, "
                "cursor_value=excluded.cursor_value, "
                "committed_batches=excluded.committed_batches, "
                "last_success_at=excluded.last_success_at, run_id=excluded.run_id",
                (source, cursor_type, json.dumps(cursor_to, ensure_ascii=False),
                 batch_seq + 1, ts, run_id),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        total_duplicates = len(duplicate_serials) + len(already_present)
        return {"inserted": inserted, "updated": updated,
                "conflicts": conflicts_count,
                "duplicates": total_duplicates,
                "already_present": already_present}

    # ------------------------------------------------------------- queries
    def main_rows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT serial_no, amount, customer, event_ts, detail_code, source "
            "FROM recon_main ORDER BY serial_no"
        ).fetchall()
        return [dict(r) for r in rows]

    def main_hash(self) -> str:
        return canonical_hash(self.main_rows())

    def count_main(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM recon_main").fetchone()["c"]

    def conflicts(self, run_id: str | None = None) -> list[dict]:
        if run_id:
            rows = self.conn.execute(
                "SELECT * FROM conflict_audit WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM conflict_audit ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def batch_rows(self, run_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM batch_audit"
        params: tuple = ()
        if run_id:
            sql += " WHERE run_id=?"
            params = (run_id,)
        sql += " ORDER BY source, batch_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def write_audit_rows(self, run_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM write_audit"
        params: tuple = ()
        if run_id:
            sql += " WHERE run_id=?"
            params = (run_id,)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        self.conn.close()
