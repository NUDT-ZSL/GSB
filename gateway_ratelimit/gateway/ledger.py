"""SQLite request ledger: batch durability, crash recovery and replay support."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import E_UNKNOWN, BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_snapshots (
    policy_version TEXT PRIMARY KEY,
    routes_json TEXT NOT NULL,
    token_table_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id INTEGER PRIMARY KEY,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_counts_json TEXT NOT NULL,
    state_summary_json TEXT NOT NULL,
    cache_version_json TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS requests (
    batch_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);
CREATE TABLE IF NOT EXISTS structured_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    code TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    mismatch_json TEXT
);
"""


class Ledger:
    def __init__(self, db_path: str | Path, now_ms: int | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._now_ms = now_ms

    def clock_ms(self) -> int:
        if self._now_ms is not None:
            return self._now_ms
        from time import monotonic_ns

        return monotonic_ns() // 1_000_000

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # --------------------------------------------------------------- logging
    def log_event(self, code: str, event: str, detail: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO structured_log(ts_ms, code, event, detail_json) VALUES (?, ?, ?, ?)",
            (self.clock_ms(), code, event, json.dumps(detail, sort_keys=True)),
        )
        self.conn.commit()

    def read_log(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts_ms, code, event, detail_json FROM structured_log ORDER BY id"
        ).fetchall()
        return [
            {
                "ts_ms": row["ts_ms"],
                "code": row["code"],
                "event": row["event"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------ snapshots
    def save_policy_snapshot(
        self, policy_version: tuple[int, int], routes: list[Any], token_table: Any
    ) -> None:
        routes_payload = [route.__dict__ for route in routes]
        token_payload = {
            "version": token_table.version,
            "tokens": [record.__dict__ for record in token_table.tokens.values()],
            "signatures": [record.__dict__ for record in token_table.signatures.values()],
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO policy_snapshots VALUES (?, ?, ?)",
            (
                json.dumps(list(policy_version)),
                json.dumps(routes_payload, sort_keys=True),
                json.dumps(token_payload, sort_keys=True),
            ),
        )
        self.conn.commit()

    def policy_snapshots(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT policy_version, routes_json, token_table_json FROM policy_snapshots"
        ).fetchall()
        return {
            row["policy_version"]: {
                "routes": json.loads(row["routes_json"]),
                "token_table": json.loads(row["token_table_json"]),
            }
            for row in rows
        }

    # --------------------------------------------------------------- batches
    def begin_batch(self, policy_version: tuple[int, int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO batches(policy_version, status, decision_counts_json, "
            "state_summary_json, cache_version_json, started_at_ms) "
            "VALUES (?, 'open', '{}', '{}', '[]', ?)",
            (json.dumps(list(policy_version)), self.clock_ms()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def commit_batch(
        self,
        batch_id: int,
        policy_version: tuple[int, int],
        requests: list[Any],
        decisions: list[dict[str, Any]],
        state_summary: dict[str, Any],
        decision_counts: dict[str, int],
    ) -> None:
        for request, decision in zip(requests, decisions):
            self.conn.execute(
                "INSERT INTO requests(batch_id, seq, request_json, decision_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    batch_id,
                    request.seq,
                    json.dumps(request.__dict__, sort_keys=True),
                    json.dumps(decision, sort_keys=True),
                ),
            )
        self.conn.execute(
            "UPDATE batches SET status='complete', decision_counts_json=?, "
            "state_summary_json=?, cache_version_json=?, committed_at_ms=? "
            "WHERE batch_id=?",
            (
                json.dumps(decision_counts, sort_keys=True),
                json.dumps(state_summary, sort_keys=True),
                json.dumps(list(policy_version)),
                self.clock_ms(),
                batch_id,
            ),
        )
        self.conn.commit()

    def mark_open_batches_unknown(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT batch_id FROM batches WHERE status='open' ORDER BY batch_id"
        ).fetchall()
        ids = [int(row["batch_id"]) for row in rows]
        for batch_id in ids:
            self.conn.execute(
                "UPDATE batches SET status=? WHERE batch_id=?",
                (E_UNKNOWN, batch_id),
            )
        self.conn.commit()
        return ids

    def complete_batches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batches WHERE status='complete' ORDER BY batch_id"
        ).fetchall()

    def batch_requests(self, batch_id: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT request_json, decision_json FROM requests WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ).fetchall()
        return [(json.loads(row["request_json"]), json.loads(row["decision_json"])) for row in rows]

    def record_replay(
        self, batch_id: int, status: str, mismatch: dict[str, Any] | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO replay_results(batch_id, status, mismatch_json) VALUES (?, ?, ?)",
            (batch_id, status, json.dumps(mismatch, sort_keys=True) if mismatch else None),
        )
        self.conn.commit()

    def latest_state_summary(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT state_summary_json FROM batches WHERE status='complete' "
            "ORDER BY batch_id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["state_summary_json"]) if row else None

    def raise_if_unknown_batch(self) -> None:
        rows = self.conn.execute(
            "SELECT batch_id FROM batches WHERE status=? ORDER BY batch_id", (E_UNKNOWN,)
        ).fetchall()
        if rows:
            ids = [row["batch_id"] for row in rows]
            raise BusinessError(
                E_UNKNOWN,
                f"incomplete batch(es) {ids} detected after restart; rerun required, "
                "no exit report can be produced for them",
            )
