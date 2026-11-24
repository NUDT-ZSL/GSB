from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import canonical_text, digest_value
from .errors import LedgerError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    battle_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    start_round INTEGER NOT NULL,
    end_round INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_version_id TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    state_blob TEXT NOT NULL,
    result_blob TEXT NOT NULL,
    PRIMARY KEY (battle_id, chunk_index)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id TEXT NOT NULL,
    battle_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    changed_keys_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS battle_results (
    battle_id TEXT PRIMARY KEY,
    balanced INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    aggregate_digest TEXT NOT NULL,
    aggregate_blob TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_chunks_battle ON chunks(battle_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_invalidations_step ON invalidations(step_id, id);
"""


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA_SQL)
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_meta(key,value) VALUES ('schema_version','ledger/v1')"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def prune_invalid_chunks(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT battle_id, chunk_index, state_digest, content_digest, state_blob, result_blob FROM chunks"
        ).fetchall()
        invalid: list[dict[str, Any]] = []
        for row in rows:
            reason = self._row_digest_error(row)
            if reason is None:
                continue
            invalid.append(
                {
                    "battle_id": row["battle_id"],
                    "chunk_index": row["chunk_index"],
                    "reason": reason,
                }
            )
        if invalid:
            with self.connection:
                for item in invalid:
                    self.connection.execute(
                        "DELETE FROM chunks WHERE battle_id=? AND chunk_index=?",
                        (item["battle_id"], item["chunk_index"]),
                    )
                    self.connection.execute(
                        "DELETE FROM battle_results WHERE battle_id=?",
                        (item["battle_id"],),
                    )
        return invalid

    @staticmethod
    def _row_digest_error(row: sqlite3.Row) -> str | None:
        try:
            state = json.loads(row["state_blob"])
            result = json.loads(row["result_blob"])
        except (json.JSONDecodeError, TypeError) as exc:
            return f"invalid_json:{exc}"
        if digest_value(state) != row["state_digest"]:
            return "state_digest_mismatch"
        material = {
            key: value
            for key, value in result.items()
            if key != "content_digest"
        }
        if digest_value(material) != row["content_digest"]:
            return "content_digest_mismatch"
        if result.get("content_digest") != row["content_digest"]:
            return "embedded_content_digest_mismatch"
        if result.get("end_state_digest") != row["state_digest"]:
            return "embedded_state_digest_mismatch"
        return None

    def commit_chunk(
        self, result: dict[str, Any], input_version_id: str = "unknown"
    ) -> None:
        battle_id = result["battle_id"]
        chunk_index = int(result["chunk_index"])
        state_blob = canonical_text(result["end_state"])
        result_blob = canonical_text(result)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO chunks(
                    battle_id, chunk_index, start_round, end_round, status,
                    input_version_id, state_digest, content_digest,
                    state_blob, result_blob
                ) VALUES (?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?)
                ON CONFLICT(battle_id, chunk_index) DO UPDATE SET
                    start_round=excluded.start_round,
                    end_round=excluded.end_round,
                    status='committed',
                    input_version_id=excluded.input_version_id,
                    state_digest=excluded.state_digest,
                    content_digest=excluded.content_digest,
                    state_blob=excluded.state_blob,
                    result_blob=excluded.result_blob
                """,
                (
                    battle_id,
                    chunk_index,
                    int(result["start_round"]),
                    int(result["end_round"]),
                    input_version_id,
                    result["end_state_digest"],
                    result["content_digest"],
                    state_blob,
                    result_blob,
                ),
            )

    def get_chunk(self, battle_id: str, chunk_index: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_blob FROM chunks WHERE battle_id=? AND chunk_index=? AND status='committed'",
            (battle_id, chunk_index),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["result_blob"])

    def get_end_state(self, battle_id: str, chunk_index: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_blob, state_digest FROM chunks WHERE battle_id=? AND chunk_index=? AND status='committed'",
            (battle_id, chunk_index),
        ).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_blob"])
        if digest_value(state) != row["state_digest"]:
            raise LedgerError(
                f"ledger digest invalid for {battle_id}#{chunk_index}",
                code="E_LEDGER_DIGEST",
            )
        return state

    def committed_indices(self, battle_id: str) -> set[int]:
        rows = self.connection.execute(
            "SELECT chunk_index FROM chunks WHERE battle_id=? AND status='committed'",
            (battle_id,),
        ).fetchall()
        return {int(row["chunk_index"]) for row in rows}

    def all_chunks_committed(self, battle_id: str, chunk_count: int) -> bool:
        return self.committed_indices(battle_id) == set(range(chunk_count))

    def load_chunk_results(self, battle_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT result_blob FROM chunks WHERE battle_id=? AND status='committed' ORDER BY chunk_index",
            (battle_id,),
        ).fetchall()
        return [json.loads(row["result_blob"]) for row in rows]

    def load_dependency_manifests(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT battle_id, chunk_index, result_blob FROM chunks WHERE status='committed' ORDER BY battle_id, chunk_index"
        ).fetchall()
        manifests = []
        for row in rows:
            result = json.loads(row["result_blob"])
            manifests.append(
                {
                    "battle_id": row["battle_id"],
                    "chunk_index": row["chunk_index"],
                    "dependencies": result["dependencies"],
                }
            )
        return manifests

    def apply_invalidation_plan(
        self, step_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        invalid = plan.get("invalid_chunks", [])
        reasons_by_coordinate = {
            (item["battle_id"], item["chunk_index"]): item
            for item in plan.get("reasons", [])
        }
        affected_battles = {item["battle_id"] for item in invalid}
        with self.connection:
            for order, coordinate in enumerate(invalid):
                battle_id = coordinate["battle_id"]
                chunk_index = int(coordinate["chunk_index"])
                reason = reasons_by_coordinate.get((battle_id, chunk_index), {})
                self.connection.execute(
                    "DELETE FROM chunks WHERE battle_id=? AND chunk_index=?",
                    (battle_id, chunk_index),
                )
                self.connection.execute(
                    """
                    INSERT INTO invalidations(
                        step_id,battle_id,chunk_index,kind,
                        changed_keys_json,reason,created_order
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        str(step_id),
                        battle_id,
                        chunk_index,
                        reason.get("kind", "invalidated"),
                        canonical_text(reason.get("changed_keys", [])),
                        reason.get("reason", ""),
                        order,
                    ),
                )
            for battle_id in affected_battles:
                self.connection.execute(
                    "DELETE FROM battle_results WHERE battle_id=?", (battle_id,)
                )
        return {"deleted_chunk_count": len(invalid), "step_id": str(step_id)}

    def upsert_battle_result(self, aggregate: dict[str, Any]) -> None:
        digest = digest_value(aggregate)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO battle_results(
                    battle_id,balanced,chunk_count,aggregate_digest,aggregate_blob
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(battle_id) DO UPDATE SET
                    balanced=excluded.balanced,
                    chunk_count=excluded.chunk_count,
                    aggregate_digest=excluded.aggregate_digest,
                    aggregate_blob=excluded.aggregate_blob
                """,
                (
                    aggregate["battle_id"],
                    1 if aggregate["balanced"] else 0,
                    int(aggregate["chunk_count"]),
                    digest,
                    canonical_text(aggregate),
                ),
            )

    def completion_overview(
        self, expected: dict[str, int]
    ) -> dict[str, Any]:
        battles = []
        complete = True
        for battle_id, chunk_count in expected.items():
            committed = self.committed_indices(battle_id)
            missing = sorted(set(range(chunk_count)) - committed)
            if missing:
                complete = False
            battles.append(
                {
                    "battle_id": battle_id,
                    "expected_chunks": chunk_count,
                    "committed_chunks": len(committed),
                    "missing_chunks": missing,
                    "complete": not missing,
                }
            )
        return {"all_complete": complete, "battles": battles}

    def latest_invalidation_step(self) -> str | None:
        row = self.connection.execute(
            "SELECT MAX(step_id) AS step_id FROM invalidations"
        ).fetchone()
        return row["step_id"] if row and row["step_id"] is not None else None
