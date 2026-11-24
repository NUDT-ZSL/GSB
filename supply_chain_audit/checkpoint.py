"""Dual SQLite/JSON checkpoints and crash-safe recovery boundaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .codes import (
    ALGORITHM_WHITELIST_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_MISSING,
    E_CHECKPOINT_VERSION,
    MERGE_POLICY_VERSION,
)
from .io_utils import atomic_write_json, digest_value


class CheckpointManager:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.workspace / "checkpoints"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "checkpoints.sqlite3"
        self._initialize_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    watermark INTEGER NOT NULL,
                    checkpoint_id TEXT NOT NULL PRIMARY KEY,
                    batch_id TEXT,
                    view_digest TEXT NOT NULL,
                    checkpoint_schema_version INTEGER NOT NULL,
                    algorithm_whitelist_version TEXT NOT NULL,
                    merge_policy_version TEXT NOT NULL,
                    artifact_count INTEGER NOT NULL,
                    snapshot_file TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def write_snapshot_file(self, envelope: dict[str, Any]) -> Path:
        digest_prefix = envelope["view_digest"][:12]
        filename = f"checkpoint-{int(envelope['watermark']):06d}-{digest_prefix}.json"
        path = self.snapshot_dir / filename
        atomic_write_json(path, envelope)
        return path

    def save_checkpoint(self, view: dict[str, Any], applied_batches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        watermark = int(view.get("stats", {}).get("watermark", 0))
        view_digest = digest_value(view)
        checkpoint_id = f"cp-{watermark:06d}-{view_digest[:12]}"
        policy = view.get("policy_versions", {})
        envelope = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "policy_versions": {
                "checkpoint_schema_version": policy.get(
                    "checkpoint_schema_version", CHECKPOINT_SCHEMA_VERSION
                ),
                "algorithm_whitelist_version": policy.get(
                    "algorithm_whitelist_version", ALGORITHM_WHITELIST_VERSION
                ),
                "merge_policy_version": policy.get("merge_policy_version", MERGE_POLICY_VERSION),
            },
            "watermark": watermark,
            "applied_batches": applied_batches or [],
            "view_digest": view_digest,
            "view": view,
        }
        snapshot_path = self.write_snapshot_file(envelope)
        payload_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO checkpoints (
                    watermark, checkpoint_id, batch_id, view_digest,
                    checkpoint_schema_version, algorithm_whitelist_version,
                    merge_policy_version, artifact_count, snapshot_file,
                    payload_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
                """,
                (
                    watermark,
                    checkpoint_id,
                    applied_batches[-1].get("batch_id") if applied_batches else None,
                    view_digest,
                    envelope["policy_versions"]["checkpoint_schema_version"],
                    envelope["policy_versions"]["algorithm_whitelist_version"],
                    envelope["policy_versions"]["merge_policy_version"],
                    len(view.get("artifacts", {})),
                    str(snapshot_path),
                    payload_json,
                ),
            )
        return {
            "status": "complete",
            "checkpoint_id": checkpoint_id,
            "watermark": watermark,
            "view_digest": view_digest,
            "snapshot_file": str(snapshot_path),
            "sqlite_db": str(self.db_path),
        }

    def latest_complete_row(self) -> sqlite3.Row | None:
        if not self.db_path.exists():
            return None
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM checkpoints WHERE status = 'complete' ORDER BY watermark DESC, rowid DESC LIMIT 1"
            ).fetchone()

    def verify_latest(self) -> dict[str, Any]:
        row = self.latest_complete_row()
        if row is None:
            return {
                "status": "missing",
                "code": E_CHECKPOINT_MISSING,
                "rebuild_required": True,
                "message": "no complete checkpoint record exists",
            }

        try:
            envelope = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            return {
                "status": "corrupt",
                "code": E_CHECKPOINT_CORRUPT,
                "rebuild_required": True,
                "checkpoint_id": row["checkpoint_id"],
                "message": f"SQLite payload is not valid JSON: {exc}",
            }

        snapshot_path = Path(row["snapshot_file"])
        if not snapshot_path.exists():
            return {
                "status": "corrupt",
                "code": E_CHECKPOINT_CORRUPT,
                "rebuild_required": True,
                "checkpoint_id": row["checkpoint_id"],
                "message": f"snapshot file is missing: {snapshot_path}",
            }
        try:
            file_envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {
                "status": "corrupt",
                "code": E_CHECKPOINT_CORRUPT,
                "rebuild_required": True,
                "checkpoint_id": row["checkpoint_id"],
                "message": f"snapshot file cannot be read: {exc}",
            }

        if digest_value(envelope.get("view")) != row["view_digest"]:
            return self._corrupt(row, "SQLite view digest does not match payload")
        if digest_value(file_envelope.get("view")) != row["view_digest"]:
            return self._corrupt(row, "snapshot file view digest does not match SQLite")
        if file_envelope.get("view_digest") != row["view_digest"]:
            return self._corrupt(row, "snapshot envelope digest field differs")
        if file_envelope != envelope:
            return self._corrupt(row, "SQLite and JSON checkpoint envelopes differ")

        current_versions = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "algorithm_whitelist_version": ALGORITHM_WHITELIST_VERSION,
            "merge_policy_version": MERGE_POLICY_VERSION,
        }
        stored_versions = {
            "checkpoint_schema_version": row["checkpoint_schema_version"],
            "algorithm_whitelist_version": row["algorithm_whitelist_version"],
            "merge_policy_version": row["merge_policy_version"],
        }
        if stored_versions != current_versions:
            return {
                "status": "version_mismatch",
                "code": E_CHECKPOINT_VERSION,
                "rebuild_required": True,
                "checkpoint_id": row["checkpoint_id"],
                "watermark": row["watermark"],
                "stored_versions": stored_versions,
                "current_versions": current_versions,
                "message": "checkpoint policy version changed; full rebuild is required",
            }

        return {
            "status": "valid",
            "rebuild_required": False,
            "checkpoint_id": row["checkpoint_id"],
            "watermark": row["watermark"],
            "view_digest": row["view_digest"],
            "snapshot_file": str(snapshot_path),
            "sqlite_db": str(self.db_path),
            "policy_versions": current_versions,
            "view": envelope["view"],
            "applied_batches": envelope.get("applied_batches"),
        }

    def _corrupt(self, row: sqlite3.Row, message: str) -> dict[str, Any]:
        return {
            "status": "corrupt",
            "code": E_CHECKPOINT_CORRUPT,
            "rebuild_required": True,
            "checkpoint_id": row["checkpoint_id"],
            "watermark": row["watermark"],
            "message": message,
        }

    def recover_or_rebuild(self, full_rebuild: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        verification = self.verify_latest()
        if verification.get("status") == "valid":
            return {
                "recovered": True,
                "rebuilt": False,
                "checkpoint": verification,
                "view": verification["view"],
            }

        # Intentionally never pass an old view into the rebuilt engine. The
        # callback must construct a fresh view solely from authoritative inputs.
        view = full_rebuild()
        saved = self.save_checkpoint(view, view.get("applied_batches"))
        return {
            "recovered": False,
            "rebuilt": True,
            "previous_checkpoint": {
                key: value for key, value in verification.items() if key != "view"
            },
            "checkpoint": saved,
            "view": view,
        }


def atomic_write_report(path: str | Path, report: dict[str, Any]) -> Path:
    return atomic_write_json(path, report)
