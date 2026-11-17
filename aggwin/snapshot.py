"""SQLite-backed state snapshots with versioned whole-state replacement.

Each snapshot is one self-contained SQLite file holding:

* ``meta``        - state format version, fixed-point version, params digest;
* ``cursors``     - per-table processed row cursor;
* ``agg_states``  - per-query, per-group aggregate state;
* ``win_states``  - per-query, per-partition ring buffer and watermark;
* ``emissions``   - per-query per-row window emissions (for resume output);
* ``trace``       - canonical identifiers written into the execution trail.

When the on-disk parameter digest differs from the freshly compiled query
digest, the engine never merges incrementally: the whole group/window state is
replaced and processing restarts from cursor zero.
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import STATE_FORMAT_VERSION, FIXED_POINT_VERSION, FIXED_POINT_SCALE
from .errors import BusinessError, E_SNAPSHOT

POINTER_FILE = "latest.snapshot"


class SnapshotStore:
    def __init__(self, snapshot_dir: str):
        self.snapshot_dir = snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)

    def path_for(self, name: str) -> str:
        return os.path.join(self.snapshot_dir, name)

    def latest_path(self) -> str | None:
        pointer = os.path.join(self.snapshot_dir, POINTER_FILE)
        if not os.path.exists(pointer):
            return None
        with open(pointer, "r", encoding="utf-8") as handle:
            name = handle.read().strip()
        path = self.path_for(name)
        return path if name and os.path.exists(path) else None

    def save(self, name: str, payload: dict) -> str:
        path = self.path_for(name)
        connection = sqlite3.connect(path)
        try:
            self._create_tables(connection)
            with connection:
                self._write_payload(connection, payload)
        finally:
            connection.close()
        with open(os.path.join(self.snapshot_dir, POINTER_FILE),
                  "w", encoding="utf-8") as handle:
            handle.write(name)
        return path

    def load(self, path: str) -> dict:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise BusinessError(
                E_SNAPSHOT, "snapshot file is missing or truncated",
                {"path": path},
            )
        try:
            connection = sqlite3.connect(path)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"meta", "cursors", "agg_states",
                        "win_states", "emissions", "trace"}
            if not required.issubset(tables):
                raise sqlite3.DatabaseError("missing snapshot tables")
            payload = self._read_payload(connection)
            connection.close()
        except (sqlite3.DatabaseError, KeyError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT, "snapshot content is corrupt and cannot be read",
                {"path": path, "detail": str(exc)},
            )
        return payload

    @staticmethod
    def corrupt(path: str, keep_bytes: int = 64) -> None:
        """Fault injection: truncate the snapshot while leaving it on disk."""
        with open(path, "r+b") as handle:
            handle.truncate(keep_bytes)

    def _create_tables(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                table_name TEXT PRIMARY KEY,
                cursor_value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agg_states (
                query_id TEXT NOT NULL,
                group_key TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY (query_id, group_key)
            );
            CREATE TABLE IF NOT EXISTS win_states (
                query_id TEXT NOT NULL,
                partition_key TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY (query_id, partition_key)
            );
            CREATE TABLE IF NOT EXISTS emissions (
                query_id TEXT NOT NULL,
                row_id INTEGER NOT NULL,
                emission_json TEXT NOT NULL,
                PRIMARY KEY (query_id, row_id)
            );
            CREATE TABLE IF NOT EXISTS trace (
                query_id TEXT PRIMARY KEY,
                canonical TEXT NOT NULL,
                digest TEXT NOT NULL
            );
            """
        )

    def _write_payload(self, connection: sqlite3.Connection, payload: dict) -> None:
        meta = {
            "state_format_version": str(STATE_FORMAT_VERSION),
            "fixed_point_version": FIXED_POINT_VERSION,
            "fixed_point_scale": str(FIXED_POINT_SCALE),
            "params_digest": payload["params_digest"],
            "interval_closure": "inclusive_both_bounds",
        }
        connection.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            meta.items(),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO cursors(table_name, cursor_value) VALUES (?, ?)",
            payload["cursors"].items(),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO agg_states(query_id, group_key, state_json)"
            " VALUES (?, ?, ?)",
            [
                (qid, key, json.dumps(state, sort_keys=True, separators=(",", ":")))
                for qid, groups in payload["agg_states"].items()
                for key, state in sorted(groups.items())
            ],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO win_states(query_id, partition_key, state_json)"
            " VALUES (?, ?, ?)",
            [
                (qid, key, json.dumps(state, sort_keys=True, separators=(",", ":")))
                for qid, parts in payload["win_states"].items()
                for key, state in sorted(parts.items())
            ],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO emissions(query_id, row_id, emission_json)"
            " VALUES (?, ?, ?)",
            [
                (qid, row_id,
                 json.dumps(emission, sort_keys=True, separators=(",", ":")))
                for qid, rows in payload["emissions"].items()
                for row_id, emission in sorted(rows.items())
            ],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO trace(query_id, canonical, digest) "
            "VALUES (?, ?, ?)",
            [
                (entry["query_id"], entry["canonical"], entry["digest"])
                for entry in sorted(payload["trace"], key=lambda item: item["query_id"])
            ],
        )

    def _read_payload(self, connection: sqlite3.Connection) -> dict:
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        cursors = dict(connection.execute(
            "SELECT table_name, cursor_value FROM cursors").fetchall())
        agg_states: dict[str, dict] = {}
        for qid, key, state_json in connection.execute(
                "SELECT query_id, group_key, state_json FROM agg_states"):
            agg_states.setdefault(qid, {})[key] = json.loads(state_json)
        win_states: dict[str, dict] = {}
        for qid, key, state_json in connection.execute(
                "SELECT query_id, partition_key, state_json FROM win_states"):
            win_states.setdefault(qid, {})[key] = json.loads(state_json)
        emissions: dict[str, dict] = {}
        for qid, row_id, emission_json in connection.execute(
                "SELECT query_id, row_id, emission_json FROM emissions"):
            emissions.setdefault(qid, {})[row_id] = json.loads(emission_json)
        trace = [
            {"query_id": qid, "canonical": canonical, "digest": digest}
            for qid, canonical, digest in connection.execute(
                "SELECT query_id, canonical, digest FROM trace ORDER BY query_id")
        ]
        return {
            "meta": meta,
            "cursors": cursors,
            "agg_states": agg_states,
            "win_states": win_states,
            "emissions": emissions,
            "trace": trace,
        }
