"""SQLite 持久化层：任务状态机、幂等入库、检查点与分页游标。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  config_json TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  note TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  task_id INTEGER NOT NULL,
  ticket_id TEXT NOT NULL,
  version TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  normalized_json TEXT,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY(task_id, ticket_id, version)
);
CREATE TABLE IF NOT EXISTS checkpoints(
  task_id INTEGER NOT NULL,
  ticket_id TEXT NOT NULL,
  version TEXT NOT NULL,
  done_at TEXT NOT NULL,
  PRIMARY KEY(task_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS cursors(
  task_id INTEGER PRIMARY KEY,
  page INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_failures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  ticket_id TEXT,
  code TEXT NOT NULL,
  message TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  at TEXT NOT NULL
);
"""

TRANSITIONS = {
    "pending": {"running"},
    "running": {"succeeded", "failed", "paused"},
    "paused": {"running"},
    "failed": {"running"},
    "succeeded": set(),
}


class StateError(Exception):
    code = "E_STATE"

    def __init__(self, task_id: int, from_state: str, to_state: str):
        super().__init__(f"illegal transition {from_state} -> {to_state} for task {task_id}")
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # ---- 任务与状态机 ----
    def create_task(self, config: dict) -> int:
        now = utcnow()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO tasks(status, config_json, created_at, updated_at) VALUES ('pending',?,?,?)",
                (json.dumps(config, ensure_ascii=False), now, now),
            )
            task_id = cur.lastrowid
            c.execute(
                "INSERT INTO task_events(task_id, from_state, to_state, note, at) VALUES (?,?,?,?,?)",
                (task_id, None, "pending", "created", now),
            )
            c.commit()
        return task_id

    def get_task(self, task_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    def list_tasks(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.pop("config_json"))
            out.append(d)
        return out

    def transition(self, task_id: int, to: str, note: str | None = None) -> None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"task {task_id} not found")
            frm = row["status"]
            if to not in TRANSITIONS.get(frm, set()):
                raise StateError(task_id, frm, to)
            now = utcnow()
            c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (to, now, task_id))
            c.execute(
                "INSERT INTO task_events(task_id, from_state, to_state, note, at) VALUES (?,?,?,?,?)",
                (task_id, frm, to, note, now),
            )
            c.commit()

    def set_error(self, task_id: int, code: str | None, message: str | None) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE tasks SET error_code=?, error_message=? WHERE id=?",
                      (code, message, task_id))
            c.commit()

    def recover_orphans(self) -> list[int]:
        """进程重启后，将仍处于 running 的遗留任务置为 paused（合法迁移）。"""
        recovered = []
        with self._conn() as c:
            rows = c.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
        for r in rows:
            self.transition(r["id"], "paused", note="recovered after process restart")
            recovered.append(r["id"])
        return recovered

    # ---- 幂等入库 ----
    def upsert_item(self, task_id: int, ticket_id: str, version: str, parse_status: str,
                    raw: dict, normalized: dict | None, source: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO items(task_id, ticket_id, version, parse_status, raw_json,
                                     normalized_json, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id, ticket_id, version) DO UPDATE SET
                     parse_status=excluded.parse_status,
                     raw_json=excluded.raw_json,
                     normalized_json=excluded.normalized_json,
                     source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (task_id, ticket_id, version, parse_status,
                 json.dumps(raw, ensure_ascii=False),
                 json.dumps(normalized, ensure_ascii=False) if normalized is not None else None,
                 source, utcnow()),
            )
            c.commit()

    def add_checkpoint(self, task_id: int, ticket_id: str, version: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO checkpoints(task_id, ticket_id, version, done_at) VALUES (?,?,?,?)",
                (task_id, ticket_id, version, utcnow()),
            )
            c.commit()

    def load_checkpoints(self, task_id: int) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT ticket_id FROM checkpoints WHERE task_id=?", (task_id,)).fetchall()
        return {r["ticket_id"] for r in rows}

    def get_cursor(self, task_id: int) -> int:
        with self._conn() as c:
            row = c.execute("SELECT page FROM cursors WHERE task_id=?", (task_id,)).fetchone()
        return row["page"] if row else 1

    def set_cursor(self, task_id: int, page: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO cursors(task_id, page, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET page=excluded.page, updated_at=excluded.updated_at",
                (task_id, page, utcnow()),
            )
            c.commit()

    # ---- 失败与快照 ----
    def record_item_failure(self, task_id: int, ticket_id: str | None, code: str, message: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO item_failures(task_id, ticket_id, code, message, at) VALUES (?,?,?,?,?)",
                (task_id, ticket_id, code, message, utcnow()),
            )
            c.commit()

    def save_snapshot(self, task_id: int, kind: str, content: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO snapshots(task_id, kind, content, at) VALUES (?,?,?,?)",
                (task_id, kind, content, utcnow()),
            )
            c.commit()

    # ---- 汇总 ----
    def summary(self, task_id: int) -> dict:
        with self._conn() as c:
            task = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"task {task_id} not found")
            stored = c.execute("SELECT COUNT(*) n FROM items WHERE task_id=?", (task_id,)).fetchone()["n"]
            distinct = c.execute(
                "SELECT COUNT(DISTINCT ticket_id) n FROM items WHERE task_id=?", (task_id,)).fetchone()["n"]
            checkpoints = c.execute(
                "SELECT COUNT(*) n FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()["n"]
            parse_rows = c.execute(
                "SELECT parse_status, COUNT(*) n FROM items WHERE task_id=? GROUP BY parse_status",
                (task_id,)).fetchall()
            failures = [dict(r) for r in c.execute(
                "SELECT ticket_id, code, message, at FROM item_failures WHERE task_id=? ORDER BY id",
                (task_id,)).fetchall()]
            events = [dict(r) for r in c.execute(
                "SELECT from_state, to_state, note, at FROM task_events WHERE task_id=? ORDER BY id",
                (task_id,)).fetchall()]
            snapshots = c.execute(
                "SELECT COUNT(*) n FROM snapshots WHERE task_id=?", (task_id,)).fetchone()["n"]
            cursor = c.execute("SELECT page FROM cursors WHERE task_id=?", (task_id,)).fetchone()
        t = dict(task)
        t["config"] = json.loads(t.pop("config_json"))
        return {
            "task": t,
            "stored_items": stored,
            "distinct_items": distinct,
            "checkpoints": checkpoints,
            "cursor_page": cursor["page"] if cursor else 1,
            "parse_status": {r["parse_status"]: r["n"] for r in parse_rows},
            "item_failures": failures,
            "item_failure_count": len(failures),
            "events": events,
            "snapshots": snapshots,
        }
