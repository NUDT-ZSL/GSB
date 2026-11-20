"""SQLite persistence with atomic per-round commits.

Every round (window) is committed in a single transaction that writes the
round watermark, request stages, replica placements, VRAM/concurrency
occupancy, window token-budget consumption and the logical-clock
watermark, then atomically moves the current-snapshot pointer in `meta`.
Readers only ever see fully committed rounds. Recovery purges anything
beyond the committed pointer (half-committed residue) before the engine
replays uncommitted rounds.
"""
import json
import os
import sqlite3

from .flock import FileLock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    version INTEGER PRIMARY KEY,
    committed INTEGER NOT NULL,
    round_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    service TEXT NOT NULL,
    seq INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (version, request_id)
);
CREATE TABLE IF NOT EXISTS placements (
    version INTEGER NOT NULL,
    service TEXT NOT NULL,
    replica INTEGER NOT NULL,
    node TEXT NOT NULL,
    card INTEGER NOT NULL,
    vram_mib INTEGER NOT NULL,
    PRIMARY KEY (version, service, replica)
);
CREATE TABLE IF NOT EXISTS budgets (
    version INTEGER NOT NULL,
    service TEXT NOT NULL,
    consumed_tokens INTEGER NOT NULL,
    PRIMARY KEY (version, service)
);
CREATE TABLE IF NOT EXISTS clock (
    version INTEGER PRIMARY KEY,
    logical_ms INTEGER NOT NULL
);
"""


def _dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock_path = path + ".lock"
        with FileLock(self._lock_path):
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        self.recover()

    def close(self):
        self._conn.close()

    def current_version(self):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='current_version'").fetchone()
        return int(row[0]) if row else -1

    def recover(self):
        """Drop half-committed residue: any row beyond the committed
        snapshot pointer can only come from an interrupted write and must
        not influence recovery decisions."""
        current = self.current_version()
        with FileLock(self._lock_path):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in ("requests", "placements", "budgets", "clock"):
                    self._conn.execute(
                        f"DELETE FROM {table} WHERE version > ?",
                        (current,))
                self._conn.execute(
                    "DELETE FROM rounds WHERE committed != 1"
                    " OR version > ?", (current,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def is_committed(self, version):
        row = self._conn.execute(
            "SELECT committed FROM rounds WHERE version = ?",
            (version,)).fetchone()
        return bool(row and row[0] == 1)

    def commit_round(self, version, round_data):
        """Atomically commit a round. Returns False when the round was
        already committed by another gateway instance (idempotent skip).
        """
        with FileLock(self._lock_path):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT committed FROM rounds WHERE version = ?",
                    (version,)).fetchone()
                if row and row[0] == 1:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "INSERT INTO rounds(version, committed, round_json)"
                    " VALUES (?, 1, ?)",
                    (version, _dumps(round_data)))
                for req in round_data["requests"]:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO requests"
                        "(version, request_id, service, seq, outcome)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (version, req["request_id"], req["service"],
                         req["seq"], req["outcome"]))
                for placement in round_data["placements"]:
                    for rep in placement["replicas"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO placements"
                            "(version, service, replica, node, card,"
                            " vram_mib) VALUES (?, ?, ?, ?, ?, ?)",
                            (version, placement["service"], rep["replica"],
                             rep["node"], rep["card"], rep["vram_mib"]))
                for service, consumed in round_data["budgets"].items():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO budgets"
                        "(version, service, consumed_tokens)"
                        " VALUES (?, ?, ?)",
                        (version, service, consumed))
                self._conn.execute(
                    "INSERT INTO clock(version, logical_ms) VALUES (?, ?)",
                    (version, round_data["clock_ms"]))
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES"
                    "('current_version', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),))
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def committed_rounds(self):
        rows = self._conn.execute(
            "SELECT round_json FROM rounds WHERE committed = 1"
            " ORDER BY version").fetchall()
        return [json.loads(r[0]) for r in rows]

    def placement_nodes_by_service(self, version):
        rows = self._conn.execute(
            "SELECT service, node FROM placements WHERE version = ?",
            (version,)).fetchall()
        result = {}
        for service, node in rows:
            result.setdefault(service, set()).add(node)
        return result

    def request_outcome(self, version, request_id):
        row = self._conn.execute(
            "SELECT outcome FROM requests WHERE version = ?"
            " AND request_id = ?", (version, request_id)).fetchone()
        return row[0] if row else None
