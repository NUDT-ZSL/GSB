"""SQLite persistence for one diagnostic session.

Stores the config version, the unified request/injection trace, hit records,
eviction events, consistency conclusions and named snapshots. Snapshots carry
the full edge/origin state so a later process can restore and replay.
"""

from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_version TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace (
    session_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    tick INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- request | injection
    node TEXT,
    action TEXT NOT NULL,          -- request path / injection type
    payload TEXT NOT NULL,         -- JSON: full deterministic input
    result TEXT NOT NULL,          -- JSON: outcome / injection record
    code TEXT,                     -- business code if any
    consistency TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS evictions (
    session_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    node TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    rule TEXT NOT NULL,
    tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    config_version TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    state_json TEXT NOT NULL       -- {origin, edges, tick, trace}
);
"""


class SessionStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.session_id = None
        self._seq = 0

    def open_session(self, config_version, config_dict):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions(config_version, config_json) VALUES (?, ?)",
                (config_version, json.dumps(config_dict, ensure_ascii=False, sort_keys=True)),
            )
            self.session_id = cur.lastrowid
            self._seq = 0
            self._conn.commit()
        return self.session_id

    def latest_session_id(self):
        row = self._conn.execute("SELECT MAX(id) FROM sessions").fetchone()
        return row[0]

    def attach_session(self, session_id):
        self.session_id = session_id
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM trace WHERE session_id=?", (session_id,)
        ).fetchone()
        self._seq = row[0]

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def log_request(self, tick, node, path, payload, result):
        with self._lock:
            seq = self._next_seq()
            self._conn.execute(
                "INSERT INTO trace(session_id, seq, tick, kind, node, action, payload, result, code, consistency)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    self.session_id, seq, tick, "request", node, path,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    result.get("code"), result.get("consistency"),
                ),
            )
            for ev in result.get("evictions", []):
                self._conn.execute(
                    "INSERT INTO evictions(session_id, seq, node, cache_key, rule, tick) VALUES (?,?,?,?,?,?)",
                    (self.session_id, seq, node, ev["key"], ev["rule"], ev["tick"]),
                )
            self._conn.commit()
            return seq

    def log_injection(self, tick, injection_type, payload, record):
        with self._lock:
            seq = self._next_seq()
            self._conn.execute(
                "INSERT INTO trace(session_id, seq, tick, kind, node, action, payload, result, code, consistency)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    self.session_id, seq, tick, "injection", None, injection_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    record.get("code"), None,
                ),
            )
            self._conn.commit()
            return seq

    def load_trace(self, session_id=None):
        sid = session_id or self.session_id
        rows = self._conn.execute(
            "SELECT seq, tick, kind, node, action, payload, result, code, consistency"
            " FROM trace WHERE session_id=? ORDER BY seq",
            (sid,),
        ).fetchall()
        return [
            {
                "seq": r[0], "tick": r[1], "kind": r[2], "node": r[3], "action": r[4],
                "payload": json.loads(r[5]), "result": json.loads(r[6]),
                "code": r[7], "consistency": r[8],
            }
            for r in rows
        ]

    def load_evictions(self, session_id=None):
        sid = session_id or self.session_id
        rows = self._conn.execute(
            "SELECT seq, node, cache_key, rule, tick FROM evictions WHERE session_id=? ORDER BY seq",
            (sid,),
        ).fetchall()
        return [
            {"seq": r[0], "node": r[1], "key": r[2], "rule": r[3], "tick": r[4]}
            for r in rows
        ]

    def save_snapshot(self, name, config_version, last_seq, state):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots(session_id, name, config_version, last_seq, state_json)"
                " VALUES (?,?,?,?,?)",
                (
                    self.session_id, name, config_version, last_seq,
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_snapshots(self):
        rows = self._conn.execute(
            "SELECT id, session_id, name, config_version, last_seq FROM snapshots ORDER BY id"
        ).fetchall()
        return [
            {"id": r[0], "session_id": r[1], "name": r[2],
             "config_version": r[3], "last_seq": r[4]}
            for r in rows
        ]

    def load_snapshot(self, snapshot_id):
        row = self._conn.execute(
            "SELECT session_id, name, config_version, last_seq, state_json FROM snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": snapshot_id, "session_id": row[0], "name": row[1],
            "config_version": row[2], "last_seq": row[3],
            "state": json.loads(row[4]),
        }

    def session_config_version(self, session_id):
        row = self._conn.execute(
            "SELECT config_version FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row[0] if row else None

    def close(self):
        with self._lock:
            self._conn.close()
