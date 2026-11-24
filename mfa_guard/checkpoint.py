"""Checkpoint evolution, atomic snapshots, and crash recovery boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, read_json
from .errors import Codes, MFAError
from .ledger import Ledger
from .locking import FileLock
from .util import canonical_json_bytes

CHECKPOINT_FORMAT = "mfa-checkpoint-v1"
SNAPSHOT_FORMAT = "mfa-ledger-snapshot-v1"


def _checksum(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("checksum", None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class CheckpointManager:
    def __init__(self, ledger: Ledger, work_dir: str | Path) -> None:
        self.ledger = ledger
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.work_dir / ".checkpoint.lock"

    def write_checkpoint(
        self,
        *,
        batch_seq: int,
        watermark: int,
        policy_version: str,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path):
            snapshot_export = self.ledger.export_snapshot()
            ledger_digest = snapshot_export["ledger_digest"]
            snapshot_name = f"ledger_snapshot_{batch_seq:08d}.json"
            snapshot_path = self.work_dir / snapshot_name
            snapshot_payload = {
                "format": SNAPSHOT_FORMAT,
                "checkpoint_seq": batch_seq,
                "watermark": watermark,
                "policy_version": policy_version,
                "ledger_digest": ledger_digest,
                "created_wall_ms": _wall_ms(),
                "state": snapshot_export["state"],
            }
            snapshot_payload["checksum"] = _checksum(snapshot_payload)
            atomic_write_json(snapshot_path, snapshot_payload)
            snapshot_digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()

            checkpoint_id = uuid.uuid4().hex
            checkpoint_name = f"checkpoint_{batch_seq:08d}.json"
            checkpoint_path = self.work_dir / checkpoint_name
            checkpoint_payload = {
                "format": CHECKPOINT_FORMAT,
                "checkpoint_seq": batch_seq,
                "checkpoint_id": checkpoint_id,
                "watermark": watermark,
                "policy_version": policy_version,
                "ledger_digest": ledger_digest,
                "snapshot_path": snapshot_name,
                "snapshot_digest": snapshot_digest,
                "created_wall_ms": _wall_ms(),
            }
            checkpoint_payload["checksum"] = _checksum(checkpoint_payload)
            atomic_write_json(checkpoint_path, checkpoint_payload)

            self.ledger.put_checkpoint(
                seq=batch_seq,
                checkpoint_id=checkpoint_id,
                watermark=watermark,
                ledger_digest=ledger_digest,
                snapshot_path=snapshot_name,
                snapshot_digest=snapshot_digest,
                checkpoint_path=checkpoint_name,
                payload=checkpoint_payload,
                created_wall_ms=_wall_ms(),
            )
            return {
                "status": "WRITTEN",
                "checkpoint_seq": batch_seq,
                "checkpoint_id": checkpoint_id,
                "watermark": watermark,
                "ledger_digest": ledger_digest,
                "snapshot_path": str(snapshot_path),
                "checkpoint_path": str(checkpoint_path),
            }

    def _validated_checkpoint(
        self, row: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        try:
            checkpoint_path = self.work_dir / row["checkpoint_path"]
            snapshot_path = self.work_dir / row["snapshot_path"]
            checkpoint = read_json(checkpoint_path)
            if not isinstance(checkpoint, dict):
                return None
            if checkpoint.get("format") != CHECKPOINT_FORMAT:
                return None
            if checkpoint.get("checksum") != _checksum(checkpoint):
                return None
            for field in (
                "checkpoint_id",
                "watermark",
                "ledger_digest",
                "snapshot_path",
                "snapshot_digest",
            ):
                if checkpoint.get(field) != row.get(field):
                    return None
            if checkpoint.get("checkpoint_seq") != row.get("seq"):
                return None
            actual_snapshot_digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            if actual_snapshot_digest != row["snapshot_digest"]:
                return None
            snapshot = read_json(snapshot_path)
            if not isinstance(snapshot, dict):
                return None
            if snapshot.get("format") != SNAPSHOT_FORMAT:
                return None
            if snapshot.get("checksum") != _checksum(snapshot):
                return None
            if snapshot.get("checkpoint_seq") != row["seq"]:
                return None
            if snapshot.get("watermark") != row["watermark"]:
                return None
            if snapshot.get("ledger_digest") != row["ledger_digest"]:
                return None
            return checkpoint, snapshot
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            return None

    def latest_complete(self) -> dict[str, Any] | None:
        rows = self.ledger.list_checkpoints()
        for row in reversed(rows):
            validated = self._validated_checkpoint(row)
            if validated is None:
                continue
            checkpoint, snapshot = validated
            return {
                "db_row": row,
                "checkpoint": checkpoint,
                "snapshot": snapshot,
                "checkpoint_seq": row["seq"],
                "watermark": row["watermark"],
                "ledger_digest": row["ledger_digest"],
            }
        return None

    def recover(self) -> dict[str, Any]:
        with FileLock(self.lock_path):
            latest = self.latest_complete()
            if latest is None:
                return {
                    "status": "NO_COMPLETE_CHECKPOINT",
                    "half_write_detected": False,
                    "restored": False,
                }
            current_digest = self.ledger.ledger_digest()
            if current_digest == latest["ledger_digest"]:
                return {
                    "status": "CONSISTENT",
                    "checkpoint_seq": latest["checkpoint_seq"],
                    "watermark": latest["watermark"],
                    "ledger_digest": current_digest,
                    "half_write_detected": False,
                    "restored": False,
                }

            snapshot = latest["snapshot"]
            if snapshot["ledger_digest"] != latest["ledger_digest"]:
                raise MFAError(
                    Codes.LEDGER_MISMATCH,
                    "checkpoint and snapshot ledger digests disagree",
                )
            self.ledger.restore_snapshot(snapshot["state"])
            restored_digest = self.ledger.ledger_digest()
            if restored_digest != latest["ledger_digest"]:
                raise MFAError(
                    Codes.LEDGER_MISMATCH,
                    "ledger digest still differs after restoring the snapshot",
                    {
                        "expected": latest["ledger_digest"],
                        "actual": restored_digest,
                    },
                )
            return {
                "status": "RECOVERED_HALF_WRITE",
                "checkpoint_seq": latest["checkpoint_seq"],
                "watermark": latest["watermark"],
                "ledger_digest": restored_digest,
                "discarded_ledger_digest": current_digest,
                "half_write_detected": True,
                "restored": True,
            }

    def clear_files(self) -> dict[str, int]:
        with FileLock(self.lock_path):
            removed = 0
            for pattern in ("checkpoint_*.json", "ledger_snapshot_*.json"):
                for path in self.work_dir.glob(pattern):
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
            return {"removed_files": removed}

