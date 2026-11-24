"""Atomic SQLite + JSON checkpointing and recovery validation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import Code
from .models import CHECKPOINT_STRUCTURE_VERSION, MERGE_SEMANTICS_VERSION
from .util import (
    FileLock,
    atomic_write_json,
    digest_bytes,
    digest_json,
    digest_file,
)


@dataclass
class CheckpointLoadResult:
    payload: dict | None = None
    row: dict | None = None
    rebuild_required: bool = False
    rebuild_reason: str | None = None
    residuals: list[dict] = field(default_factory=list)
    orphan_temp_files: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.payload is not None


class CheckpointManager:
    def __init__(
        self,
        *,
        db_path: Path,
        checkpoint_dir: Path,
        temp_dir: Path,
        lock_path: Path,
    ):
        self.db_path = Path(db_path)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.temp_dir = Path(temp_dir)
        self.lock_path = Path(lock_path)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                input_fingerprint TEXT NOT NULL,
                structure_version INTEGER NOT NULL,
                merge_semantics_version INTEGER NOT NULL,
                applied_watermark_ms INTEGER,
                cluster_digest TEXT NOT NULL,
                snapshot_name TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                complete INTEGER NOT NULL,
                logical_ts_ms INTEGER NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def clear(self) -> None:
        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("DELETE FROM checkpoints")
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            for path in self.checkpoint_dir.glob("checkpoint-*.json"):
                path.unlink(missing_ok=True)
            for path in self.temp_dir.glob("*.tmp"):
                path.unlink(missing_ok=True)

    def save(
        self,
        *,
        state: dict,
        input_fingerprint: str,
        applied_batch_ids: list[str],
        applied_watermark_ms: int | None,
        logical_ts_ms: int,
        rule_catalog_digest: str,
    ) -> dict:
        state_digest = digest_json(state)
        cluster_digest = digest_json(state.get("clusters", []))
        payload = {
            "structure_version": CHECKPOINT_STRUCTURE_VERSION,
            "merge_semantics_version": MERGE_SEMANTICS_VERSION,
            "input_fingerprint": input_fingerprint,
            "applied_batch_ids": sorted(applied_batch_ids),
            "applied_watermark_ms": applied_watermark_ms,
            "logical_ts_ms": logical_ts_ms,
            "rule_catalog_digest": rule_catalog_digest,
            "state_digest": state_digest,
            "cluster_digest": cluster_digest,
            "state": state,
        }
        with FileLock(self.lock_path):
            row = self.connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM checkpoints"
            ).fetchone()
            next_seq = int(row["next_seq"])
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            file_sha = digest_bytes(encoded)
            snapshot_name = (
                f"checkpoint-{next_seq:08d}-{logical_ts_ms}-{file_sha[:16]}.json"
            )
            snapshot_path = self.checkpoint_dir / snapshot_name
            atomic_write_json(
                snapshot_path,
                payload,
                lock_path=self.lock_path,
                acquire_lock=False,
                temp_dir=self.temp_dir,
                validator=_validate_payload,
            )
            actual_sha = digest_file(snapshot_path)
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self.connection.execute(
                    """
                    INSERT INTO checkpoints(
                        input_fingerprint, structure_version,
                        merge_semantics_version, applied_watermark_ms,
                        cluster_digest, snapshot_name, snapshot_sha256,
                        complete, logical_ts_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        input_fingerprint,
                        CHECKPOINT_STRUCTURE_VERSION,
                        MERGE_SEMANTICS_VERSION,
                        applied_watermark_ms,
                        cluster_digest,
                        snapshot_name,
                        actual_sha,
                        logical_ts_ms,
                    ),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                snapshot_path.unlink(missing_ok=True)
                raise
            return {
                "seq": cursor.lastrowid,
                "snapshot_name": snapshot_name,
                "snapshot_sha256": actual_sha,
                "cluster_digest": cluster_digest,
                "state_digest": state_digest,
                "applied_watermark_ms": applied_watermark_ms,
                "logical_ts_ms": logical_ts_ms,
            }

    def load_latest(self, *, input_fingerprint: str) -> CheckpointLoadResult:
        result = CheckpointLoadResult()
        result.orphan_temp_files = sorted(
            str(path) for path in self.temp_dir.glob("*.tmp")
        )
        rows = self.connection.execute(
            "SELECT * FROM checkpoints ORDER BY seq DESC"
        ).fetchall()
        if not rows:
            return result

        latest = rows[0]
        if (
            latest["structure_version"] != CHECKPOINT_STRUCTURE_VERSION
            or latest["merge_semantics_version"] != MERGE_SEMANTICS_VERSION
        ):
            result.rebuild_required = True
            result.rebuild_reason = Code.E_CHECKPOINT_REBUILD
            result.residuals.append(
                {
                    "code": Code.E_CHECKPOINT_REBUILD,
                    "seq": latest["seq"],
                    "message": (
                        "checkpoint structure or merge semantics version changed"
                    ),
                    "structure_version": latest["structure_version"],
                    "merge_semantics_version": latest[
                        "merge_semantics_version"
                    ],
                }
            )
            return result
        if latest["input_fingerprint"] != input_fingerprint:
            result.rebuild_required = True
            result.rebuild_reason = "INPUT_FINGERPRINT_CHANGED"
            result.residuals.append(
                {
                    "code": Code.E_CHECKPOINT_REBUILD,
                    "seq": latest["seq"],
                    "message": "checkpoint belongs to a different input batch",
                }
            )
            return result

        for row in rows:
            if not row["complete"]:
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint row is marked incomplete",
                    }
                )
                continue
            snapshot_path = self.checkpoint_dir / row["snapshot_name"]
            if not snapshot_path.exists():
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint snapshot is missing",
                        "snapshot": row["snapshot_name"],
                    }
                )
                continue
            file_sha = digest_file(snapshot_path)
            if file_sha != row["snapshot_sha256"]:
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint snapshot checksum mismatch",
                        "snapshot": row["snapshot_name"],
                    }
                )
                continue
            try:
                with snapshot_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                _validate_payload(payload)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint snapshot is not parseable",
                        "snapshot": row["snapshot_name"],
                        "error": str(exc),
                    }
                )
                continue
            if (
                payload.get("structure_version")
                != CHECKPOINT_STRUCTURE_VERSION
                or payload.get("merge_semantics_version")
                != MERGE_SEMANTICS_VERSION
            ):
                result.rebuild_required = True
                result.rebuild_reason = Code.E_CHECKPOINT_REBUILD
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_REBUILD,
                        "seq": row["seq"],
                        "message": "snapshot structure version changed",
                    }
                )
                return result
            if payload.get("input_fingerprint") != input_fingerprint:
                result.rebuild_required = True
                result.rebuild_reason = "INPUT_FINGERPRINT_CHANGED"
                return result
            state = payload.get("state")
            if not isinstance(state, dict):
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint does not contain a state object",
                    }
                )
                continue
            state_digest = digest_json(state)
            cluster_digest = digest_json(state.get("clusters", []))
            if state_digest != payload.get("state_digest"):
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint state digest mismatch",
                    }
                )
                continue
            if (
                cluster_digest != payload.get("cluster_digest")
                or cluster_digest != row["cluster_digest"]
            ):
                result.residuals.append(
                    {
                        "code": Code.E_CHECKPOINT_CORRUPT,
                        "seq": row["seq"],
                        "message": "checkpoint cluster digest mismatch",
                    }
                )
                continue
            result.payload = payload
            result.row = dict(row)
            return result

        result.rebuild_required = True
        result.rebuild_reason = "NO_COMPLETE_CHECKPOINT"
        return result

    def checkpoint_rows(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM checkpoints ORDER BY seq"
            ).fetchall()
        ]


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload must be an object")
    required = (
        "structure_version",
        "merge_semantics_version",
        "input_fingerprint",
        "applied_batch_ids",
        "state_digest",
        "cluster_digest",
        "state",
    )
    for key in required:
        if key not in payload:
            raise ValueError(f"checkpoint payload is missing {key}")
    if not isinstance(payload["state"], dict):
        raise ValueError("checkpoint state must be an object")
    if not isinstance(payload["applied_batch_ids"], list):
        raise ValueError("checkpoint applied_batch_ids must be a list")
