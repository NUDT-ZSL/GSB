"""SQLite persistence for distribution session snapshots.

The snapshot holds: version manifests, distribution plans, node receipts,
state-machine transitions, dispatch observations, node health status and
the committed version number. Recovery is whole-snapshot replacement when
the incoming manifest version differs from the snapshot version; the
committed version never regresses.
"""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    version INTEGER PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    version INTEGER PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    keys TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frm TEXT NOT NULL,
    dst TEXT NOT NULL,
    version INTEGER NOT NULL,
    acked TEXT NOT NULL,
    ts REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    version INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS node_status (
    node TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
"""

SESSION_TABLES = ("manifests", "plans", "receipts", "transitions", "observations",
                  "node_status")


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(SCHEMA)
        if self.get_meta("committed_version") is None:
            self.set_meta("committed_version", "0")
        if self.get_meta("snapshot_version") is None:
            self.set_meta("snapshot_version", "0")

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def committed_version(self):
        return int(self.get_meta("committed_version") or 0)

    def snapshot_version(self):
        return int(self.get_meta("snapshot_version") or 0)

    def set_committed_version(self, version):
        """Monotonic: a lower or equal committed version is never stored."""
        current = self.committed_version()
        if version > current:
            self.set_meta("committed_version", str(version))
            return True
        return False

    def save_manifest(self, manifest_dict):
        version = manifest_dict["version"]
        body = json.dumps(manifest_dict, sort_keys=True)
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT body FROM manifests WHERE version=?", (version,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO manifests(version, body) VALUES(?,?)", (version, body)
                )
            elif row["body"] != body:
                raise ValueError("conflicting manifest body for version %d" % version)

    def get_manifest(self, version):
        row = self.db.execute(
            "SELECT body FROM manifests WHERE version=?", (version,)
        ).fetchone()
        return json.loads(row["body"]) if row else None

    def manifest_versions(self):
        rows = self.db.execute("SELECT version FROM manifests ORDER BY version").fetchall()
        return [r["version"] for r in rows]

    def save_plan(self, plan):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO plans(version, body) VALUES(?,?) "
                "ON CONFLICT(version) DO UPDATE SET body=excluded.body",
                (plan["version"], json.dumps(plan, sort_keys=True)),
            )

    def get_plan(self, version):
        row = self.db.execute(
            "SELECT body FROM plans WHERE version=?", (version,)
        ).fetchone()
        return json.loads(row["body"]) if row else None

    def add_receipt(self, node, version, seq, attempt, kind, digest, keys):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO receipts(node,version,seq,attempt,kind,digest,keys,ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (node, version, seq, attempt, kind, digest, json.dumps(keys), time.time()),
            )

    def receipts(self, version=None):
        sql = "SELECT * FROM receipts"
        args = ()
        if version is not None:
            sql += " WHERE version=?"
            args = (version,)
        sql += " ORDER BY id"
        rows = self.db.execute(sql, args).fetchall()
        return [
            {
                "node": r["node"],
                "version": r["version"],
                "seq": r["seq"],
                "attempt": r["attempt"],
                "kind": r["kind"],
                "digest": r["digest"],
                "keys": json.loads(r["keys"]),
                "ts": r["ts"],
            }
            for r in rows
        ]

    def add_transition(self, frm, dst, version, acked, reason=""):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO transitions(frm,dst,version,acked,ts,reason) "
                "VALUES(?,?,?,?,?,?)",
                (frm, dst, version, json.dumps(sorted(acked)), time.time(), reason),
            )

    def transitions(self):
        rows = self.db.execute("SELECT * FROM transitions ORDER BY id").fetchall()
        return [
            {
                "from": r["frm"],
                "to": r["dst"],
                "version": r["version"],
                "acked": json.loads(r["acked"]),
                "ts": r["ts"],
                "reason": r["reason"],
            }
            for r in rows
        ]

    def add_observation(self, node, version, event, detail=""):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO observations(node,version,event,detail,ts) VALUES(?,?,?,?,?)",
                (node, version, event, detail, time.time()),
            )

    def observations(self, version=None):
        sql = "SELECT * FROM observations"
        args = ()
        if version is not None:
            sql += " WHERE version=?"
            args = (version,)
        sql += " ORDER BY id"
        rows = self.db.execute(sql, args).fetchall()
        return [
            {
                "node": r["node"],
                "version": r["version"],
                "event": r["event"],
                "detail": r["detail"],
                "ts": r["ts"],
            }
            for r in rows
        ]

    def set_node_status(self, node, status):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO node_status(node,status) VALUES(?,?) "
                "ON CONFLICT(node) DO UPDATE SET status=excluded.status",
                (node, status),
            )

    def node_statuses(self):
        rows = self.db.execute("SELECT * FROM node_status ORDER BY node").fetchall()
        return {r["node"]: r["status"] for r in rows}

    def replace_snapshot(self, manifest_dict, plan):
        """Whole-snapshot replacement: drop the previous session state and
        install the new manifest + plan. committed_version is preserved and
        never regresses."""
        with self._lock, self.db:
            for table in SESSION_TABLES:
                self.db.execute("DELETE FROM %s" % table)
        self.save_manifest(manifest_dict)
        self.save_plan(plan)
        self.set_meta("snapshot_version", str(manifest_dict["version"]))

    def close(self):
        self.db.close()
