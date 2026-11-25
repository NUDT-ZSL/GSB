"""SQLite persistence: single-transaction atomic checkpointing.

Execution-plan watermark, operator states, memory accounting, fixed-point
error ledgers and the logical-clock watermark are all committed in one
transaction per operator completion, so a killed process always recovers
from a consistent checkpoint. A file lock plus BEGIN IMMEDIATE serializes
concurrent worker processes.
"""
import fcntl
import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_versions(
  version INTEGER PRIMARY KEY,
  graph_hash TEXT NOT NULL,
  graph_json TEXT NOT NULL,
  created_clock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  features_json TEXT NOT NULL,
  digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS op_units(
  node_id TEXT NOT NULL,
  graph_version INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  layer INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  memory_bytes INTEGER,
  abandon_reason TEXT,
  started_clock INTEGER,
  finished_clock INTEGER,
  reused INTEGER NOT NULL DEFAULT 0,
  worker TEXT,
  outputs_json TEXT,
  ledger_json TEXT,
  PRIMARY KEY(node_id, graph_version, window_id)
);
CREATE TABLE IF NOT EXISTS memory_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  graph_version INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  clock INTEGER NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS window_commits(
  window_id INTEGER NOT NULL,
  graph_version INTEGER NOT NULL,
  commit_hash TEXT NOT NULL,
  clock INTEGER NOT NULL,
  PRIMARY KEY(window_id, graph_version)
);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock_path = db_path + ".lock"
        self.conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    @contextmanager
    def file_lock(self):
        fh = open(self.lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    @contextmanager
    def txn(self):
        """Atomic critical section: file lock + BEGIN IMMEDIATE."""
        with self.file_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value, conn=None):
        c = conn or self.conn
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                  (key, str(value)))

    def get_clock(self) -> int:
        return int(self.get_meta("clock", "0"))

    def load_artifacts(self, graph, events, config, plan_summary):
        import hashlib
        ghash = hashlib.sha256(
            json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.txn() as c:
            c.execute("DELETE FROM events")
            c.executemany(
                "INSERT INTO events(seq, ts_ms, features_json, digest) VALUES(?,?,?,?)",
                [(e["seq"], e["ts_ms"], json.dumps(e["features"], sort_keys=True),
                  e["digest"]) for e in events])
            c.execute("INSERT OR REPLACE INTO graph_versions"
                      "(version, graph_hash, graph_json, created_clock)"
                      " VALUES(1, ?, ?, 0)", (ghash, json.dumps(graph, sort_keys=True)))
            self.set_meta("current_version", "1", c)
            self.set_meta("clock", "0", c)
            self.set_meta("config_json", json.dumps(config, sort_keys=True), c)
            self.set_meta("plan_summary", plan_summary, c)
            self.set_meta("loaded", "1", c)

    def is_loaded(self) -> bool:
        return self.get_meta("loaded") == "1"

    def current_version(self) -> int:
        return int(self.get_meta("current_version", "0"))

    def get_graph(self, version=None) -> dict:
        version = version or self.current_version()
        row = self.conn.execute(
            "SELECT graph_json FROM graph_versions WHERE version=?",
            (version,)).fetchone()
        return json.loads(row["graph_json"]) if row else None

    def get_config(self) -> dict:
        return json.loads(self.get_meta("config_json", "{}"))

    def get_events(self) -> list:
        rows = self.conn.execute(
            "SELECT seq, ts_ms, features_json, digest FROM events ORDER BY seq"
        ).fetchall()
        return [{"seq": r["seq"], "ts_ms": r["ts_ms"],
                 "features": json.loads(r["features_json"]),
                 "digest": r["digest"]} for r in rows]

    def ensure_units(self, units, version):
        with self.txn() as c:
            for u in units:
                c.execute(
                    "INSERT OR IGNORE INTO op_units"
                    "(node_id, graph_version, window_id, layer, status)"
                    " VALUES(?,?,?,?,'pending')",
                    (u["node_id"], version, u["window_id"], u["layer"]))

    def reset_running(self, version):
        """Half-committed operators (status=running at crash) become pending."""
        with self.txn() as c:
            cur = c.execute(
                "UPDATE op_units SET status='pending', worker=NULL"
                " WHERE status='running' AND graph_version=?", (version,))
            return cur.rowcount

    def memory_balance(self, conn=None) -> int:
        c = conn or self.conn
        row = c.execute(
            "SELECT COALESCE(SUM(delta),0) AS b FROM memory_ledger").fetchone()
        return int(row["b"])
