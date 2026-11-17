"""SQLite persistence: sessions, state-machine transitions, plans, receipts,
committed versions, and meta. Also implements the version state machine with
idempotent commit."""
import json
import sqlite3
import threading
import time

from .errors import E_VERSION, PlatformError

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version INTEGER NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  version INTEGER NOT NULL,
  confirmed_nodes TEXT NOT NULL,
  ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plans(
  session_id INTEGER NOT NULL,
  node TEXT NOT NULL,
  slice TEXT NOT NULL,
  derive TEXT NOT NULL,
  expected_derived TEXT NOT NULL,
  version INTEGER NOT NULL,
  PRIMARY KEY(session_id, node));
CREATE TABLE IF NOT EXISTS receipts(
  session_id INTEGER NOT NULL,
  node TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  version INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  derived TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY(session_id, node, attempt));
CREATE TABLE IF NOT EXISTS committed(
  version INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL,
  ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL);
"""

STATES = ("prepared", "distributing", "committed", "failed")
ALLOWED = {("prepared", "distributing"), ("distributing", "committed"),
           ("distributing", "failed"), ("failed", "distributing")}


class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # -- sessions & state machine -------------------------------------------
    def create_session(self, version):
        with self._lock, self.conn:
            now = time.time()
            cur = self.conn.execute(
                "INSERT INTO sessions(version,state,created_at,updated_at)"
                " VALUES(?,?,?,?)", (version, "prepared", now, now))
            sid = cur.lastrowid
            self._transition_locked(sid, None, "prepared", version, [])
            return sid

    def _confirmed_nodes(self, session_id):
        rows = self.conn.execute(
            "SELECT DISTINCT node FROM receipts WHERE session_id=? ORDER BY node",
            (session_id,)).fetchall()
        return [r["node"] for r in rows]

    def _transition_locked(self, session_id, from_state, to_state, version,
                           confirmed):
        self.conn.execute(
            "INSERT INTO transitions(session_id,from_state,to_state,version,"
            "confirmed_nodes,ts) VALUES(?,?,?,?,?,?)",
            (session_id, from_state, to_state, version,
             json.dumps(sorted(confirmed)), time.time()))
        self.conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                          (to_state, time.time(), session_id))

    def transition(self, session_id, to_state):
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT state, version FROM sessions WHERE id=?",
                (session_id,)).fetchone()
            if row is None:
                raise PlatformError(E_VERSION, f"unknown session {session_id}")
            if (row["state"], to_state) not in ALLOWED:
                raise PlatformError(
                    E_VERSION, f"illegal transition {row['state']} -> {to_state}",
                    session=session_id)
            self._transition_locked(session_id, row["state"], to_state,
                                    row["version"],
                                    self._confirmed_nodes(session_id))

    def commit(self, session_id):
        """Idempotent: re-committing the same version reports already_committed.
        A version may not be committed while a lower version is uncommitted."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT version, state FROM sessions WHERE id=?",
                (session_id,)).fetchone()
            if row is None:
                raise PlatformError(E_VERSION, f"unknown session {session_id}")
            version, state = row["version"], row["state"]
            if self.conn.execute("SELECT 1 FROM committed WHERE version=?",
                                 (version,)).fetchone():
                return {"result": "already_committed", "version": version}
            pending = self.conn.execute(
                "SELECT id, version FROM sessions WHERE version<? AND"
                " state!='committed' ORDER BY version", (version,)).fetchall()
            if pending:
                raise PlatformError(
                    E_VERSION,
                    f"version {version} cannot skip uncommitted version(s) "
                    f"{[p['version'] for p in pending]}",
                    blocking=[p["version"] for p in pending])
            if state != "distributing":
                raise PlatformError(E_VERSION,
                                    f"cannot commit from state {state}",
                                    session=session_id)
            self.conn.execute(
                "INSERT INTO committed(version,session_id,ts) VALUES(?,?,?)",
                (version, session_id, time.time()))
            self._transition_locked(session_id, state, "committed", version,
                                    self._confirmed_nodes(session_id))
            return {"result": "committed", "version": version}

    def fail_session(self, session_id):
        self.transition(session_id, "failed")

    # -- plans & receipts ----------------------------------------------------
    def record_plan(self, session_id, plan):
        with self._lock, self.conn:
            for node in sorted(plan):
                slot = plan[node]
                self.conn.execute(
                    "INSERT OR REPLACE INTO plans VALUES(?,?,?,?,?,?)",
                    (session_id, node, json.dumps(slot["entries"],
                                                  sort_keys=True),
                     json.dumps(slot["derive"], sort_keys=True),
                     slot["expected_derived"], slot["version"]))

    def record_receipt(self, session_id, node, attempt, version, seq, derived):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,?,?)",
                (session_id, node, attempt, version, seq, derived, time.time()))

    # -- meta ----------------------------------------------------------------
    def set_meta(self, key, value):
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                              (key, value))

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                (key,)).fetchone()
        return row["value"] if row else None

    # -- queries ---------------------------------------------------------------
    def last_committed(self):
        row = self.conn.execute(
            "SELECT version, session_id FROM committed ORDER BY version DESC"
            " LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_plan(self, session_id):
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE session_id=? ORDER BY node",
            (session_id,)).fetchall()
        return {r["node"]: {"entries": json.loads(r["slice"]),
                            "derive": json.loads(r["derive"]),
                            "expected_derived": r["expected_derived"],
                            "version": r["version"]} for r in rows}

    def get_receipts(self, session_id):
        rows = self.conn.execute(
            "SELECT * FROM receipts WHERE session_id=? ORDER BY node, attempt",
            (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_transitions(self, session_id=None):
        sql = "SELECT * FROM transitions"
        args = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            args = (session_id,)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def sessions(self):
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM sessions ORDER BY id")]

    # -- snapshot wholesale replace -------------------------------------------
    def dump_all(self):
        plans = []
        for r in self.conn.execute("SELECT * FROM plans ORDER BY session_id, node"):
            plans.append({"session_id": r["session_id"], "node": r["node"],
                          "entries": json.loads(r["slice"]),
                          "derive": json.loads(r["derive"]),
                          "expected_derived": r["expected_derived"],
                          "version": r["version"]})
        committed = [dict(r) for r in
                     self.conn.execute("SELECT * FROM committed ORDER BY version")]
        meta = {r["key"]: r["value"] for r in
                self.conn.execute("SELECT * FROM meta ORDER BY key")}
        return {"sessions": self.sessions(),
                "transitions": self.get_transitions(),
                "plans": plans,
                "receipts": [dict(r) for r in self.conn.execute(
                    "SELECT * FROM receipts ORDER BY session_id, node, attempt")],
                "committed": committed,
                "meta": meta}

    def replace_all(self, sessions, transitions, plans, receipts, committed,
                    meta):
        with self._lock, self.conn:
            for table in ("sessions", "transitions", "plans", "receipts",
                          "committed", "meta"):
                self.conn.execute(f"DELETE FROM {table}")
            for s in sessions:
                self.conn.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?,?)",
                    (s["id"], s["version"], s["state"], s["created_at"],
                     s["updated_at"]))
            for t in transitions:
                self.conn.execute(
                    "INSERT INTO transitions(session_id,from_state,to_state,"
                    "version,confirmed_nodes,ts) VALUES(?,?,?,?,?,?)",
                    (t["session_id"], t["from_state"], t["to_state"],
                     t["version"], t["confirmed_nodes"], t["ts"]))
            for p in plans:
                self.conn.execute(
                    "INSERT INTO plans VALUES(?,?,?,?,?,?)",
                    (p["session_id"], p["node"],
                     json.dumps(p["entries"], sort_keys=True),
                     json.dumps(p["derive"], sort_keys=True),
                     p["expected_derived"], p["version"]))
            for r in receipts:
                self.conn.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?,?,?,?)",
                    (r["session_id"], r["node"], r["attempt"], r["version"],
                     r["seq"], r["derived"], r["ts"]))
            for c in committed:
                self.conn.execute(
                    "INSERT INTO committed VALUES(?,?,?)",
                    (c["version"], c["session_id"], c["ts"]))
            for k, v in meta.items():
                self.conn.execute("INSERT INTO meta VALUES(?,?)", (k, v))
