"""SQLite persistence for transactions, events, version chains, and history."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .errors import E_CHAIN, E_INPUT, E_TXN, E_VERSION, BusinessError
from .util import canonical_json


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tables_meta (
    table_name TEXT PRIMARY KEY,
    columns_json TEXT NOT NULL,
    created_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    txid INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'committed', 'rolled_back')),
    snapshot_xmin INTEGER NOT NULL,
    snapshot_xmax INTEGER NOT NULL,
    active_json TEXT NOT NULL,
    begin_version INTEGER NOT NULL,
    end_version INTEGER,
    begun_ns INTEGER NOT NULL,
    ended_ns INTEGER
);

CREATE TABLE IF NOT EXISTS txn_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    txid INTEGER NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('begin', 'commit', 'rollback')),
    state_version INTEGER NOT NULL,
    at_ns INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    xmin INTEGER NOT NULL,
    xmax INTEGER,
    value_json TEXT,
    is_tombstone INTEGER NOT NULL CHECK (is_tombstone IN (0, 1)),
    prev_version_id INTEGER,
    next_version_id INTEGER,
    created_version INTEGER NOT NULL,
    xmax_set_version INTEGER,
    UNIQUE (table_name, row_key, xmin)
);

CREATE TABLE IF NOT EXISTS row_heads (
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    head_version_id INTEGER NOT NULL,
    tail_version_id INTEGER NOT NULL,
    chain_len INTEGER NOT NULL,
    PRIMARY KEY (table_name, row_key)
);

CREATE TABLE IF NOT EXISTS history (
    query_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    txid INTEGER NOT NULL,
    mode TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    snapshot_xmin INTEGER NOT NULL,
    snapshot_xmax INTEGER NOT NULL,
    active_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    canonical TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    latency_us INTEGER,
    degradation_reason TEXT,
    created_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_version ON txn_events(state_version, event_id);
CREATE INDEX IF NOT EXISTS idx_versions_lookup ON versions(table_name, row_key, created_version);
CREATE INDEX IF NOT EXISTS idx_versions_xmin ON versions(xmin);

CREATE VIEW IF NOT EXISTS version_rows_with_status AS
SELECT
    v.*,
    creator.state AS xmin_commit_state,
    deleter.state AS xmax_commit_state
FROM versions AS v
LEFT JOIN transactions AS creator ON creator.txid = v.xmin
LEFT JOIN transactions AS deleter ON deleter.txid = v.xmax;
"""


def _connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


class Storage:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("BEGIN IMMEDIATE")
            defaults = {
                "schema_version": str(SCHEMA_VERSION),
                "next_txid": "1",
                "state_version": "0",
                "last_recovery": "none",
            }
            for key, value in defaults.items():
                conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value))
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ meta
    def meta(self, conn: sqlite3.Connection | None = None) -> dict[str, str]:
        if conn is None:
            with self.read_transaction() as own:
                return self.meta(own)
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}

    def state_version(self) -> int:
        return int(self.meta()["state_version"])

    def _bump_version(self, conn: sqlite3.Connection, current_version: int | None = None) -> int:
        if current_version is None:
            current_version = int(
                conn.execute("SELECT value FROM meta WHERE key='state_version'").fetchone()["value"]
            )
        new_version = current_version + 1
        conn.execute("UPDATE meta SET value=? WHERE key='state_version'", (str(new_version),))
        return new_version

    def set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --------------------------------------------------------------- tables
    def create_table(self, table_name: str, columns: Sequence[dict[str, Any]]) -> dict[str, Any]:
        if not table_name or any(ch in table_name for ch in "\x00\n\r"):
            raise BusinessError(E_INPUT, "invalid table name", {"table": table_name})
        columns_json = canonical_json(list(columns))
        with self.write_transaction() as conn:
            existing = conn.execute(
                "SELECT columns_json FROM tables_meta WHERE table_name=?", (table_name,)
            ).fetchone()
            if existing is not None:
                if existing["columns_json"] == columns_json:
                    return {"table": table_name, "columns": json.loads(columns_json), "created": False}
                raise BusinessError(
                    E_VERSION,
                    "table already exists with a different schema",
                    {"table": table_name, "chain_position": None},
                )
            version = self._bump_version(conn)
            conn.execute(
                "INSERT INTO tables_meta(table_name, columns_json, created_version) VALUES (?, ?, ?)",
                (table_name, columns_json, version),
            )
            return {"table": table_name, "columns": json.loads(columns_json), "created": True,
                    "state_version": version}

    def table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM tables_meta WHERE table_name=?", (table_name,)
        ).fetchone() is not None

    # -------------------------------------------------------------- tx lifecycle
    def begin_transaction(self, label: str, expected_txid: int | None = None) -> dict[str, Any]:
        with self.write_transaction() as conn:
            meta = self.meta(conn)
            next_txid = int(meta["next_txid"])
            if expected_txid is not None and int(expected_txid) != next_txid:
                raise BusinessError(
                    E_TXN,
                    "allocated transaction id does not match the requested monotonic id",
                    {
                        "expected_txid": int(expected_txid),
                        "allocated_txid": next_txid,
                        "transaction_state_unchanged": True,
                    },
                )
            active_rows = conn.execute(
                "SELECT txid FROM transactions WHERE state='active' ORDER BY txid"
            ).fetchall()
            active = [int(row["txid"]) for row in active_rows]
            txid = next_txid
            held = int(meta["state_version"])
            snapshot_xmax = txid + 1
            snapshot_xmin = min(active) if active else snapshot_xmax
            version = self._bump_version(conn, held)
            now = time.time_ns()
            conn.execute(
                "INSERT INTO transactions(txid,label,state,snapshot_xmin,snapshot_xmax,active_json,"
                "begin_version,end_version,begun_ns,ended_ns) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (txid, label, "active", snapshot_xmin, snapshot_xmax, canonical_json(active),
                 version, None, now, None),
            )
            conn.execute(
                "INSERT INTO txn_events(txid,event,state_version,at_ns) VALUES (?,?,?,?)",
                (txid, "begin", version, now),
            )
            self.set_meta(conn, "next_txid", str(txid + 1))
            return {
                "txid": txid,
                "label": label,
                "state": "active",
                "snapshot_xmin": snapshot_xmin,
                "snapshot_xmax": snapshot_xmax,
                "active": active,
                "begin_version": version,
            }

    def end_transaction(self, txid: int, target_state: str) -> dict[str, Any]:
        if target_state not in {"committed", "rolled_back"}:
            raise BusinessError(E_INPUT, "invalid terminal transaction state", {"state": target_state})
        event = "commit" if target_state == "committed" else "rollback"
        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE txid=?", (txid,)).fetchone()
            if row is None:
                raise BusinessError(E_TXN, "transaction does not exist", {"txid": txid})
            if row["state"] != "active":
                raise BusinessError(
                    E_TXN,
                    "illegal transaction state transition",
                    {
                        "txid": txid,
                        "from": row["state"],
                        "to": target_state,
                        "transaction_state_unchanged": True,
                    },
                )
            version = self._bump_version(conn)
            now = time.time_ns()
            conn.execute(
                "UPDATE transactions SET state=?, end_version=?, ended_ns=? WHERE txid=?",
                (target_state, version, now, txid),
            )
            conn.execute(
                "INSERT INTO txn_events(txid,event,state_version,at_ns) VALUES (?,?,?,?)",
                (txid, event, version, now),
            )
            return {"txid": txid, "state": target_state, "end_version": version}

    def get_transaction(self, txid: int) -> dict[str, Any]:
        with self.read_transaction() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE txid=?", (txid,)).fetchone()
        if row is None:
            raise BusinessError(E_TXN, "transaction does not exist", {"txid": txid})
        result = dict(row)
        result["active"] = json.loads(result.pop("active_json"))
        return result

    def list_transactions(self) -> list[dict[str, Any]]:
        with self.read_transaction() as conn:
            rows = conn.execute("SELECT * FROM transactions ORDER BY txid").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["active"] = json.loads(item.pop("active_json"))
            result.append(item)
        return result

    # ------------------------------------------------------------ version chain
    def _validate_chain_conn(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        row_key: str,
        *,
        allow_empty: bool = True,
    ) -> list[dict[str, Any]]:
        head_row = conn.execute(
            "SELECT * FROM row_heads WHERE table_name=? AND row_key=?",
            (table_name, row_key),
        ).fetchone()
        version_rows = conn.execute(
            "SELECT * FROM versions WHERE table_name=? AND row_key=? ORDER BY version_id",
            (table_name, row_key),
        ).fetchall()
        if head_row is None:
            if version_rows and not allow_empty:
                raise BusinessError(E_CHAIN, "missing row head", {"table": table_name, "key": row_key})
            if version_rows:
                raise BusinessError(E_CHAIN, "versions exist without a row head", {
                    "table": table_name, "key": row_key
                })
            return []
        if not version_rows:
            raise BusinessError(E_CHAIN, "row head points to an empty chain", {
                "table": table_name, "key": row_key
            })

        by_id = {int(row["version_id"]): dict(row) for row in version_rows}
        head_id = int(head_row["head_version_id"])
        current = head_id
        visited: list[int] = []
        seen: set[int] = set()
        position = 0
        while current is not None:
            position += 1
            if current in seen:
                raise BusinessError(
                    E_CHAIN,
                    "cycle detected in version chain",
                    {"table": table_name, "key": row_key, "version_id": current,
                     "chain_position": position},
                )
            if current not in by_id:
                raise BusinessError(
                    E_CHAIN,
                    "version chain points to a missing version",
                    {"table": table_name, "key": row_key, "version_id": current,
                     "chain_position": position},
                )
            seen.add(current)
            visited.append(current)
            row = by_id[current]
            prev_id = row["prev_version_id"]
            if prev_id is not None:
                prev = by_id.get(int(prev_id))
                if prev is None:
                    raise BusinessError(
                        E_CHAIN,
                        "previous-version pointer is missing",
                        {"table": table_name, "key": row_key, "version_id": current,
                         "missing_version_id": int(prev_id), "chain_position": position},
                    )
                if prev["next_version_id"] != current:
                    raise BusinessError(
                        E_CHAIN,
                        "previous/next version pointers disagree",
                        {"table": table_name, "key": row_key, "version_id": current,
                         "prev_version_id": int(prev_id), "chain_position": position},
                    )
            current = int(prev_id) if prev_id is not None else None

        chain = [by_id[version_id] for version_id in reversed(visited)]
        actual_tail = int(chain[0]["version_id"])
        if int(head_row["tail_version_id"]) != actual_tail or int(head_row["chain_len"]) != len(chain):
            raise BusinessError(
                E_CHAIN,
                "row head metadata does not match the traversed chain",
                {
                    "table": table_name,
                    "key": row_key,
                    "head_expected": int(head_row["head_version_id"]),
                    "tail_expected": int(head_row["tail_version_id"]),
                    "chain_len_expected": int(head_row["chain_len"]),
                    "tail_actual": actual_tail,
                    "chain_len_actual": len(chain),
                },
            )
        if len(chain) != len(by_id):
            raise BusinessError(
                E_CHAIN,
                "version row is detached from the traversed chain",
                {"table": table_name, "key": row_key, "traversed": len(chain), "stored": len(by_id)},
            )
        for index, row in enumerate(chain):
            if index < len(chain) - 1 and row["xmax"] is None:
                raise BusinessError(
                    E_CHAIN,
                    "non-head version has no xmax",
                    {"table": table_name, "key": row_key, "version_id": row["version_id"],
                     "chain_position": index + 1},
                )
            if index < len(chain) - 1:
                next_row = chain[index + 1]
                if row["next_version_id"] != next_row["version_id"] or row["xmax"] != next_row["xmin"]:
                    raise BusinessError(
                        E_CHAIN,
                        "version xmax does not match the next version transaction",
                        {
                            "table": table_name,
                            "key": row_key,
                            "version_id": row["version_id"],
                            "chain_position": index + 1,
                            "xmax": row["xmax"],
                            "next_version_id": row["next_version_id"],
                            "expected_next_version_id": next_row["version_id"],
                            "expected_xmax": next_row["xmin"],
                        },
                    )
            if index == len(chain) - 1 and row["xmax"] is not None:
                raise BusinessError(
                    E_CHAIN,
                    "chain head already has an xmax",
                    {"table": table_name, "key": row_key, "version_id": row["version_id"],
                     "chain_position": index + 1},
                )
        for index in range(1, len(chain)):
            previous = chain[index - 1]
            current_row = chain[index]
            if int(previous["xmin"]) >= int(current_row["xmin"]):
                raise BusinessError(
                    E_VERSION,
                    "transaction ids are not strictly increasing along the version chain",
                    {
                        "table": table_name,
                        "key": row_key,
                        "chain_position": index + 1,
                        "previous_version_id": previous["version_id"],
                        "version_id": current_row["version_id"],
                        "previous_xmin": previous["xmin"],
                        "xmin": current_row["xmin"],
                    },
                )
        return chain

    def validate_chain(self, table_name: str, row_key: str) -> list[dict[str, Any]]:
        with self.read_transaction() as conn:
            return self._validate_chain_conn(conn, table_name, row_key)

    def _insert_version_conn(
        self,
        conn: sqlite3.Connection,
        *,
        txid: int,
        table_name: str,
        row_key: str,
        value: Any,
        tombstone: bool,
        state_version: int,
    ) -> dict[str, Any]:
        if not self.table_exists(conn, table_name):
            raise BusinessError(
                E_VERSION,
                "table is not declared",
                {"table": table_name, "key": row_key, "chain_position": None},
            )
        chain = self._validate_chain_conn(conn, table_name, row_key)
        for position, version in enumerate(chain, start=1):
            if int(version["xmin"]) == txid:
                raise BusinessError(
                    E_VERSION,
                    "one transaction may create only one final version per key",
                    {"table": table_name, "key": row_key, "txid": txid,
                     "chain_position": position, "version_id": version["version_id"]},
                )
        if chain and txid <= int(chain[-1]["xmin"]):
            raise BusinessError(
                E_VERSION,
                "new transaction id must be greater than the chain head transaction id",
                {"table": table_name, "key": row_key, "txid": txid,
                 "chain_position": len(chain) + 1,
                 "head_version_id": chain[-1]["version_id"],
                 "head_xmin": chain[-1]["xmin"]},
            )
        value_json = None if tombstone else canonical_json(value)
        cursor = conn.execute(
            "INSERT INTO versions(table_name,row_key,xmin,xmax,value_json,is_tombstone,"
            "prev_version_id,next_version_id,created_version,xmax_set_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (table_name, row_key, txid, None, value_json, 1 if tombstone else 0,
             chain[-1]["version_id"] if chain else None, None, state_version, None),
        )
        version_id = int(cursor.lastrowid)
        if chain:
            head = chain[-1]
            conn.execute(
                "UPDATE versions SET xmax=?, next_version_id=?, xmax_set_version=? "
                "WHERE version_id=?",
                (txid, version_id, state_version, head["version_id"]),
            )
            conn.execute(
                "UPDATE row_heads SET head_version_id=?, chain_len=chain_len+1 "
                "WHERE table_name=? AND row_key=?",
                (version_id, table_name, row_key),
            )
        else:
            conn.execute(
                "INSERT INTO row_heads(table_name,row_key,head_version_id,tail_version_id,chain_len) "
                "VALUES (?,?,?,?,1)",
                (table_name, row_key, version_id, version_id),
            )
        return {
            "version_id": version_id,
            "table": table_name,
            "key": row_key,
            "xmin": txid,
            "xmax": None,
            "is_tombstone": tombstone,
            "value": None if tombstone else value,
            "prev_version_id": chain[-1]["version_id"] if chain else None,
            "next_version_id": None,
            "chain_position": len(chain) + 1,
            "created_version": state_version,
        }

    def write_version(
        self,
        *,
        txid: int,
        table_name: str,
        row_key: str,
        value: Any = None,
        tombstone: bool = False,
    ) -> dict[str, Any]:
        with self.write_transaction() as conn:
            tx = conn.execute("SELECT state FROM transactions WHERE txid=?", (txid,)).fetchone()
            if tx is None:
                raise BusinessError(E_TXN, "transaction does not exist", {"txid": txid})
            if tx["state"] != "active":
                raise BusinessError(
                    E_TXN,
                    "only an active transaction may write",
                    {"txid": txid, "state": tx["state"], "transaction_state_unchanged": True},
                )
            version = self._bump_version(conn)
            inserted = self._insert_version_conn(
                conn,
                txid=txid,
                table_name=table_name,
                row_key=row_key,
                value=value,
                tombstone=tombstone,
                state_version=version,
            )
            inserted["state_version"] = version
            return inserted

    def auto_commit_write(
        self,
        *,
        table_name: str,
        row_key: str,
        value: Any,
        label: str,
    ) -> dict[str, Any]:
        """Atomically begin, write, and commit one background transaction."""

        with self.write_transaction() as conn:
            meta = self.meta(conn)
            txid = int(meta["next_txid"])
            active = [
                int(row["txid"])
                for row in conn.execute(
                    "SELECT txid FROM transactions WHERE state='active' ORDER BY txid"
                )
            ]
            version = self._bump_version(conn, int(meta["state_version"]))
            now = time.time_ns()
            snapshot_xmax = txid + 1
            snapshot_xmin = min(active) if active else snapshot_xmax
            conn.execute(
                "INSERT INTO transactions(txid,label,state,snapshot_xmin,snapshot_xmax,active_json,"
                "begin_version,end_version,begun_ns,ended_ns) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (txid, label, "committed", snapshot_xmin, snapshot_xmax, canonical_json(active),
                 version, version, now, now),
            )
            conn.execute(
                "INSERT INTO txn_events(txid,event,state_version,at_ns) VALUES (?,?,?,?)",
                (txid, "begin", version, now),
            )
            inserted = self._insert_version_conn(
                conn,
                txid=txid,
                table_name=table_name,
                row_key=row_key,
                value=value,
                tombstone=False,
                state_version=version,
            )
            conn.execute(
                "INSERT INTO txn_events(txid,event,state_version,at_ns) VALUES (?,?,?,?)",
                (txid, "commit", version, now),
            )
            self.set_meta(conn, "next_txid", str(txid + 1))
            return {
                "txid": txid,
                "state": "committed",
                "version": inserted,
                "state_version": version,
                "snapshot_xmax": snapshot_xmax,
                "active": active,
            }

    # --------------------------------------------------------- replay / views
    def tx_states_at(self, conn: sqlite3.Connection, view_version: int) -> dict[int, str]:
        states: dict[int, str] = {}
        rows = conn.execute(
            "SELECT txid, event FROM txn_events WHERE state_version<=? ORDER BY state_version, event_id",
            (view_version,),
        ).fetchall()
        for row in rows:
            event = row["event"]
            states[int(row["txid"])] = {
                "begin": "active",
                "commit": "committed",
                "rollback": "rolled_back",
            }[event]
        return states

    def next_txid_at(self, conn: sqlite3.Connection, view_version: int) -> int:
        row = conn.execute(
            "SELECT MAX(txid) AS max_txid FROM txn_events WHERE state_version<=?",
            (view_version,),
        ).fetchone()
        return int(row["max_txid"] or 0) + 1

    def chain_at(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        row_key: str,
        view_version: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM versions WHERE table_name=? AND row_key=? AND created_version<=? "
            "ORDER BY version_id",
            (table_name, row_key, view_version),
        ).fetchall()
        if not rows:
            return []
        by_id = {int(row["version_id"]): dict(row) for row in rows}
        children: dict[int, int] = {}
        for row in by_id.values():
            prev_id = row["prev_version_id"]
            if prev_id is None:
                continue
            prev_int = int(prev_id)
            if prev_int not in by_id:
                raise BusinessError(
                    E_CHAIN,
                    "historical chain references a version outside the view",
                    {"table": table_name, "key": row_key, "version_id": row["version_id"],
                     "missing_version_id": prev_int, "view_version": view_version},
                )
            if prev_int in children:
                raise BusinessError(
                    E_CHAIN,
                    "two versions claim the same predecessor",
                    {"table": table_name, "key": row_key, "prev_version_id": prev_int,
                     "view_version": view_version},
                )
            children[prev_int] = int(row["version_id"])
        heads = [version_id for version_id in by_id if version_id not in children]
        if len(heads) != 1:
            raise BusinessError(
                E_CHAIN,
                "historical view does not contain exactly one head",
                {"table": table_name, "key": row_key, "heads": heads,
                 "view_version": view_version},
            )
        current: int | None = heads[0]
        visited: list[int] = []
        seen: set[int] = set()
        while current is not None:
            if current in seen:
                raise BusinessError(
                    E_CHAIN,
                    "cycle detected in historical version chain",
                    {"table": table_name, "key": row_key, "version_id": current,
                     "view_version": view_version},
                )
            seen.add(current)
            visited.append(current)
            row = by_id[current]
            expected_next = children.get(current)
            stored_next = row["next_version_id"]
            if expected_next is not None and stored_next != expected_next:
                raise BusinessError(
                    E_CHAIN,
                    "historical pointer disagrees with predecessor relation",
                    {"table": table_name, "key": row_key, "version_id": current,
                     "stored_next_version_id": stored_next,
                     "expected_next_version_id": expected_next,
                     "view_version": view_version},
                )
            prev_id = row["prev_version_id"]
            current = int(prev_id) if prev_id is not None else None

        if len(visited) != len(by_id):
            raise BusinessError(
                E_CHAIN,
                "historical chain traversal did not cover every version",
                {"table": table_name, "key": row_key, "view_version": view_version,
                 "traversed": len(visited), "stored": len(by_id)},
            )
        chain = [by_id[version_id] for version_id in reversed(visited)]
        for index, row in enumerate(chain):
            if row["xmax_set_version"] is not None and int(row["xmax_set_version"]) > view_version:
                row["xmax"] = None
                row["xmax_set_version"] = None
            row["next_version_id"] = (
                int(chain[index + 1]["version_id"]) if index + 1 < len(chain) else None
            )
            row["chain_position"] = index + 1
            if index + 1 < len(chain):
                next_row = chain[index + 1]
                if row["xmax"] != next_row["xmin"]:
                    raise BusinessError(
                        E_CHAIN,
                        "historical xmax does not match the next version transaction",
                        {
                            "table": table_name,
                            "key": row_key,
                            "version_id": row["version_id"],
                            "chain_position": index + 1,
                            "xmax": row["xmax"],
                            "expected_xmax": next_row["xmin"],
                            "view_version": view_version,
                        },
                    )
        for index in range(1, len(chain)):
            left = int(chain[index - 1]["xmin"])
            right = int(chain[index]["xmin"])
            if left >= right:
                raise BusinessError(
                    E_VERSION,
                    "transaction ids are not strictly increasing in the historical chain",
                    {"table": table_name, "key": row_key, "chain_position": index + 1,
                     "previous_xmin": left, "xmin": right, "view_version": view_version},
                )
        return chain

    def load_replay_view(
        self,
        table_name: str,
        row_key: str,
        view_version: int,
    ) -> dict[str, Any]:
        with self.read_transaction() as conn:
            current_version = int(self.meta(conn)["state_version"])
            if view_version > current_version:
                raise BusinessError(
                    E_CACHE,
                    "requested view is newer than the persisted session",
                    {"view_version": view_version, "current_version": current_version},
                )
            chain = self.chain_at(conn, table_name, row_key, view_version)
            states = self.tx_states_at(conn, view_version)
            next_txid = self.next_txid_at(conn, view_version)
            tx_rows = {
                int(row["txid"]): dict(row)
                for row in conn.execute("SELECT * FROM transactions ORDER BY txid")
            }
        active = sorted(txid for txid, state in states.items() if state == "active")
        return {
            "view_version": view_version,
            "chain": chain,
            "tx_states": states,
            "next_txid": next_txid,
            "active": active,
            "transactions": tx_rows,
        }

    # ---------------------------------------------------------------- history
    def append_history(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.write_transaction() as conn:
            seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM history").fetchone()
            seq = int(seq_row["seq"])
            query_id = f"q{seq:06d}"
            conn.execute(
                "INSERT INTO history(query_id,seq,txid,mode,table_name,row_key,view_version,"
                "snapshot_xmin,snapshot_xmax,active_json,result_json,canonical,decision_source,"
                "latency_us,degradation_reason,created_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    query_id, seq, int(record["txid"]), record["mode"], record["table"],
                    record["key"], int(record["view_version"]), int(record["snapshot_xmin"]),
                    int(record["snapshot_xmax"]), canonical_json(record["active"]),
                    canonical_json(record["result"]), record["canonical"],
                    record.get("decision_source", "fast"), record.get("latency_us"),
                    record.get("degradation_reason"), time.time_ns(),
                ),
            )
        saved = dict(record)
        saved["query_id"] = query_id
        saved["seq"] = seq
        return saved

    def list_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM history ORDER BY seq"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql = "SELECT * FROM (SELECT * FROM history ORDER BY seq DESC LIMIT ?) ORDER BY seq"
            params = (int(limit),)
        with self.read_transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["active"] = json.loads(item.pop("active_json"))
            item["result"] = json.loads(item.pop("result_json"))
            result.append(item)
        return result

    # --------------------------------------------------------- snapshot helpers
    def export_state(self) -> dict[str, Any]:
        with self.read_transaction() as conn:
            def rows(table: str, order_by: str) -> list[dict[str, Any]]:
                return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]

            return {
                "meta": self.meta(conn),
                "tables": rows("tables_meta", "table_name"),
                "transactions": rows("transactions", "txid"),
                "txn_events": rows("txn_events", "event_id"),
                "versions": rows("versions", "version_id"),
                "row_heads": rows("row_heads", "table_name, row_key"),
                "history": rows("history", "seq"),
            }

    def mark_all_active_rolled_back(self) -> dict[str, Any]:
        """Recovery path used only after an unclean process start."""

        with self.write_transaction() as conn:
            active = [
                int(row["txid"])
                for row in conn.execute(
                    "SELECT txid FROM transactions WHERE state='active' ORDER BY txid"
                )
            ]
            if not active:
                return {"recovered": False, "txids": [], "state_version": int(self.meta(conn)["state_version"])}
            version = self._bump_version(conn)
            now = time.time_ns()
            for txid in active:
                conn.execute(
                    "UPDATE transactions SET state='rolled_back', end_version=?, ended_ns=? "
                    "WHERE txid=?",
                    (version, now, txid),
                )
                conn.execute(
                    "INSERT INTO txn_events(txid,event,state_version,at_ns) VALUES (?,?,?,?)",
                    (txid, "rollback", version, now),
                )
            self.set_meta(conn, "last_recovery", f"forced_rollback@{version}")
            return {"recovered": True, "txids": active, "state_version": version}

    def replace_state(self, data: dict[str, Any], session_version: int) -> None:
        """Whole-session replacement used by named snapshot restore.

        The caller validates the snapshot first.  This method performs no
        merge and runs in one SQLite write transaction.
        """

        with self.write_transaction() as conn:
            for table in ("history", "row_heads", "versions", "txn_events", "transactions",
                          "tables_meta"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM meta")
            source_keys = {
                "tables_meta": "tables",
                "transactions": "transactions",
                "txn_events": "txn_events",
                "versions": "versions",
                "row_heads": "row_heads",
                "history": "history",
            }
            for table, source_key in source_keys.items():
                source = data[source_key]
                if not source:
                    continue
                columns = list(source[0].keys())
                placeholders = ",".join("?" for _ in columns)
                column_sql = ",".join(columns)
                conn.executemany(
                    f"INSERT INTO {table}({column_sql}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in source],
                )
            meta = dict(data["meta"])
            meta["state_version"] = str(session_version)
            meta["schema_version"] = str(SCHEMA_VERSION)
            for key, value in meta.items():
                self.set_meta(conn, key, str(value))
            # Explicit ids were inserted; keep AUTOINCREMENT counters above them.
            for table in ("transactions", "txn_events", "versions", "history"):
                max_id = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
                if max_id is not None:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                    conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, max_id))
