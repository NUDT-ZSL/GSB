"""SQLite ledger, deterministic digests and checkpoint recovery primitives."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import LEDGER_LAYOUT_VERSION
from .codes import (
    E_CHECKPOINT_MISMATCH,
    E_CHECKPOINT_RESIDUE,
    E_HALF_WRITTEN_FILE,
    E_LEDGER_LAYOUT_MISMATCH,
    E_LEDGER_PK_CONFLICT,
    E_SNAPSHOT_VERSION_MISMATCH,
    PreconditionError,
)
from .jsonio import atomic_write_json, canonical_dumps, list_stale_temporary_files, sha256_text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    user_id TEXT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    batch_watermark INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    decision TEXT NOT NULL,
    codes_json TEXT NOT NULL,
    policy_key TEXT,
    policy_generation INTEGER,
    canonical_fingerprint_key TEXT,
    judgement_watermark INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, attempt_seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ledger_watermark ON ledger(batch_watermark);
CREATE TABLE IF NOT EXISTS checkpoints (
    watermark INTEGER PRIMARY KEY,
    ledger_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    snapshot_version TEXT NOT NULL,
    sync_marker TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostic_artifacts (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
"""


class LedgerStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "ledger.sqlite3"
        self.checkpoint_dir = self.state_dir / "checkpoints"
        self.report_dir = self.state_dir / "reports"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        self.ensure_dirs()
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(
        self,
        snapshot_version: str,
        observation_clock_ms: int,
        layout_version: int = LEDGER_LAYOUT_VERSION,
    ) -> dict[str, str]:
        self.ensure_dirs()
        with self.connect() as connection:
            existing_meta: dict[str, str] = {}
            meta_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if meta_table:
                existing_meta = {
                    row["key"]: row["value"]
                    for row in connection.execute("SELECT key, value FROM meta").fetchall()
                }
            if existing_meta.get("layout_version") and int(existing_meta["layout_version"]) != int(layout_version):
                raise PreconditionError(
                    E_LEDGER_LAYOUT_MISMATCH,
                    "ledger layout version changed; rebuild required",
                    {"existing": int(existing_meta["layout_version"]), "required": layout_version},
                )
            if existing_meta.get("snapshot_version") and existing_meta["snapshot_version"] != snapshot_version:
                raise PreconditionError(
                    E_SNAPSHOT_VERSION_MISMATCH,
                    "snapshot version changed; refusing to mix old conclusions",
                    {"existing": existing_meta["snapshot_version"], "incoming": snapshot_version},
                )
            connection.executescript(SCHEMA_SQL)
            meta = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta").fetchall()
            }
            values = {
                "layout_version": str(layout_version),
                "snapshot_version": snapshot_version,
                "factor_whitelist_version": "3",
                "binding_canon_version": "1",
                "initialized_at_ms": str(observation_clock_ms),
            }
            connection.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(values.items()),
            )
            return values

    def meta(self) -> dict[str, str]:
        if not self.db_path.exists():
            return {}
        with self.connect() as connection:
            return {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key,value FROM meta").fetchall()
            }

    def last_checkpoint(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM checkpoints ORDER BY watermark DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def ledger_digest(
        self, connection: sqlite3.Connection, max_watermark: int | None = None
    ) -> tuple[str, int]:
        if max_watermark is None:
            rows = connection.execute(
                "SELECT user_id, attempt_seq, batch_watermark, result_json "
                "FROM ledger ORDER BY user_id, attempt_seq"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT user_id, attempt_seq, batch_watermark, result_json "
                "FROM ledger WHERE batch_watermark <= ? ORDER BY user_id, attempt_seq",
                (max_watermark,),
            ).fetchall()
        digest = sha256_text(
            [
                {
                    "user_id": row["user_id"],
                    "attempt_seq": row["attempt_seq"],
                    "batch_watermark": row["batch_watermark"],
                    "result": json.loads(row["result_json"]),
                }
                for row in rows
            ]
        )
        return digest, len(rows)

    def commit_adjudication(
        self,
        results: list[dict[str, Any]],
        watermark: int,
        snapshot_version: str,
        observation_clock_ms: int,
        extra_checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for result in results:
                    existing = connection.execute(
                        "SELECT input_hash, result_hash FROM ledger WHERE user_id=? AND attempt_seq=?",
                        (result["user_id"], result["attempt_seq"]),
                    ).fetchone()
                    if existing:
                        if (
                            existing["input_hash"] != result["input_hash"]
                            or existing["result_hash"] != result["result_hash"]
                        ):
                            raise PreconditionError(
                                E_LEDGER_PK_CONFLICT,
                                "user_id/attempt_seq already has a different conclusion",
                                {
                                    "user_id": result["user_id"],
                                    "attempt_seq": result["attempt_seq"],
                                },
                            )
                        continue
                    connection.execute(
                        """
                        INSERT INTO ledger(
                            user_id, attempt_seq, batch_watermark, logical_clock,
                            observed_at_ms, decision, codes_json, policy_key,
                            policy_generation, canonical_fingerprint_key,
                            judgement_watermark, result_json, input_hash,
                            result_hash, created_at_ms
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            result["user_id"],
                            result["attempt_seq"],
                            result["batch_watermark"],
                            result["logical_clock"],
                            result["observed_at_ms"],
                            result["decision"],
                            canonical_dumps(result["codes"]),
                            result["policy_key"],
                            result["policy_generation"],
                            result["canonical_fingerprint_key"],
                            result["judgement_watermark"],
                            canonical_dumps(result),
                            result["input_hash"],
                            result["result_hash"],
                            observation_clock_ms,
                        ),
                    )

                digest, row_count = self.ledger_digest(connection, watermark)
                sync_marker = sha256_text(
                    {"watermark": watermark, "digest": digest, "snapshot": snapshot_version}
                )[:16]
                detail = {
                    "watermark": watermark,
                    "ledger_digest": digest,
                    "row_count": row_count,
                    "snapshot_version": snapshot_version,
                    "sync_marker": sync_marker,
                }
                if extra_checkpoint:
                    detail.update(extra_checkpoint)
                connection.execute(
                    """
                    INSERT INTO checkpoints(
                        watermark, ledger_digest, row_count, snapshot_version,
                        sync_marker, detail_json, created_at_ms
                    ) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(watermark) DO UPDATE SET
                        ledger_digest=excluded.ledger_digest,
                        row_count=excluded.row_count,
                        snapshot_version=excluded.snapshot_version,
                        sync_marker=excluded.sync_marker,
                        detail_json=excluded.detail_json,
                        created_at_ms=excluded.created_at_ms
                    """,
                    (
                        watermark,
                        digest,
                        row_count,
                        snapshot_version,
                        sync_marker,
                        canonical_dumps(detail),
                        observation_clock_ms,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        checkpoint = self.write_checkpoint_file(detail)
        return checkpoint

    def write_checkpoint_file(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        watermark = int(checkpoint["watermark"])
        self.ensure_dirs()
        numbered = self.checkpoint_dir / f"checkpoint-{watermark:06d}.json"
        latest = self.checkpoint_dir / "checkpoint-latest.json"
        atomic_write_json(numbered, checkpoint)
        atomic_write_json(latest, checkpoint)
        return checkpoint

    def read_complete_file_checkpoints(self) -> list[dict[str, Any]]:
        complete: list[dict[str, Any]] = []
        if not self.checkpoint_dir.exists():
            return complete
        pattern = re.compile(r"checkpoint-(\d+)\.json$")
        for path in sorted(self.checkpoint_dir.glob("checkpoint-*.json")):
            if path.name == "checkpoint-latest.json" or not pattern.search(path.name):
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if int(payload["watermark"]) >= 0 and payload["ledger_digest"]:
                    payload["_file"] = str(path)
                    complete.append(payload)
            except Exception:
                continue
        return sorted(complete, key=lambda item: int(item["watermark"]))

    def verify_checkpoint(self) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        temp_files = list_stale_temporary_files(self.state_dir)
        for path in temp_files:
            issues.append({"code": E_HALF_WRITTEN_FILE, "path": path})

        if not self.db_path.exists():
            file_checkpoints = self.read_complete_file_checkpoints()
            if file_checkpoints:
                latest_file = file_checkpoints[-1]
                issues.append(
                    {
                        "code": E_CHECKPOINT_MISMATCH,
                        "message": "checkpoint file exists without SQLite ledger",
                        "watermark": str(latest_file["watermark"]),
                    }
                )
            return {
                "ok": not issues,
                "empty": True,
                "sqlite_checkpoint": None,
                "file_checkpoint": file_checkpoints[-1] if file_checkpoints else None,
                "ledger_digest": None,
                "ledger_row_count": 0,
                "residue_rows": [],
                "stale_temp_files": temp_files,
                "issues": issues,
            }

        with self.connect() as connection:
            meta = self.meta()
            sqlite_checkpoint = self.last_checkpoint(connection)
            watermark = sqlite_checkpoint["watermark"] if sqlite_checkpoint else None
            digest, count = self.ledger_digest(connection, watermark)
            all_digest, all_count = self.ledger_digest(connection)
            residue: list[dict[str, Any]] = []
            if watermark is None:
                residue = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT user_id, attempt_seq, batch_watermark, decision "
                        "FROM ledger ORDER BY user_id, attempt_seq"
                    ).fetchall()
                ]
            else:
                if sqlite_checkpoint["ledger_digest"] != digest:
                    issues.append(
                        {
                            "code": E_CHECKPOINT_MISMATCH,
                            "scope": "sqlite_ledger",
                            "message": "SQLite checkpoint digest does not match ledger",
                            "watermark": str(watermark),
                        }
                    )
                residue = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT user_id, attempt_seq, batch_watermark, decision "
                        "FROM ledger WHERE batch_watermark > ? ORDER BY user_id, attempt_seq",
                        (watermark,),
                    ).fetchall()
                ]
            if residue:
                issues.append(
                    {
                        "code": E_CHECKPOINT_RESIDUE,
                        "message": "ledger rows are ahead of the last complete checkpoint",
                        "count": str(len(residue)),
                    }
                )

        file_checkpoints = self.read_complete_file_checkpoints()
        latest_file = file_checkpoints[-1] if file_checkpoints else None
        if sqlite_checkpoint and latest_file:
            if int(latest_file["watermark"]) != int(sqlite_checkpoint["watermark"]):
                issues.append({"code": E_CHECKPOINT_MISMATCH, "scope": "file", "message": "file/SQLite checkpoint watermark differ"})
            elif latest_file["ledger_digest"] != sqlite_checkpoint["ledger_digest"]:
                issues.append({"code": E_CHECKPOINT_MISMATCH, "scope": "file", "message": "file/SQLite checkpoint digest differ"})
        elif sqlite_checkpoint and not latest_file:
            # DB commit is authoritative; file can be regenerated by recovery.
            pass
        elif latest_file and not sqlite_checkpoint:
            issues.append({"code": E_CHECKPOINT_MISMATCH, "scope": "file", "message": "file checkpoint exists without SQLite checkpoint"})

        return {
            "ok": not issues,
            "empty": False,
            "meta": meta,
            "sqlite_checkpoint": sqlite_checkpoint,
            "file_checkpoint": latest_file,
            "ledger_digest": digest if watermark is not None else all_digest,
            "ledger_row_count": count if watermark is not None else all_count,
            "all_ledger_row_count": all_count,
            "residue_rows": residue,
            "stale_temp_files": temp_files,
            "issues": issues,
        }

    def recover_checkpoint(self) -> dict[str, Any]:
        """Return the last safe watermark, rolling back obvious orphan residue."""

        verification = self.verify_checkpoint()
        fatal_mismatch = any(
            issue.get("code") == E_CHECKPOINT_MISMATCH
            and issue.get("scope") == "sqlite_ledger"
            for issue in verification["issues"]
        )
        if verification.get("sqlite_checkpoint") and not fatal_mismatch:
            checkpoint = verification["sqlite_checkpoint"]
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "DELETE FROM ledger WHERE batch_watermark > ?",
                        (checkpoint["watermark"],),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            detail = json.loads(checkpoint["detail_json"])
            self.write_checkpoint_file(detail)
            return {"recovered_to_watermark": checkpoint["watermark"], "action": "resume_sqlite_checkpoint"}

        complete_files = self.read_complete_file_checkpoints()
        if complete_files:
            latest = complete_files[-1]
            return {"recovered_to_watermark": latest["watermark"], "action": "resume_file_checkpoint_compare_ledger"}
        return {"recovered_to_watermark": 0, "action": "start_empty"}

    def list_ledger(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT result_json FROM ledger ORDER BY user_id, attempt_seq"
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def save_artifact(self, name: str, payload: dict[str, Any], observation_clock_ms: int) -> None:
        self.ensure_dirs()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO diagnostic_artifacts(name,payload_json,updated_at_ms) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json, updated_at_ms=excluded.updated_at_ms",
                (name, canonical_dumps(payload), observation_clock_ms),
            )
            connection.commit()

    def load_artifact(self, name: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM diagnostic_artifacts WHERE name=?", (name,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None


def reset_state_dir(state_dir: str | Path) -> None:
    """Remove all mutable ledger/checkpoint state before a full rebuild."""

    path = Path(state_dir)
    if not path.exists():
        path.mkdir(parents=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
