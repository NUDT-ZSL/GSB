from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .atomicio import atomic_write_json
from .errors import CheckpointError
from .ledger import Ledger


class CheckpointStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "checkpoint.json"
        self.temp_path = self.state_dir / "checkpoint.json.tmp"

    def write_snapshot(self, payload: Dict[str, Any]) -> Path:
        required = ["checkpoint_version", "watermark", "batch_watermark", "ledger_digest"]
        return atomic_write_json(self.path, payload, required_keys=required)

    def read_snapshot(self) -> Dict[str, Any]:
        with open(self.path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not all(key in value for key in ("watermark", "batch_watermark", "ledger_digest")):
            raise CheckpointError("checkpoint snapshot is missing required keys")
        return value

    def _quarantine(self, path: Path, suffix: str) -> str:
        target = self.state_dir / f"{path.name}.{suffix}"
        try:
            os.replace(path, target)
            return f"quarantined:{target.name}"
        except OSError:
            try:
                path.unlink()
                return "removed"
            except OSError:
                return "left-in-place"

    def recover(self, ledger: Ledger) -> Dict[str, Any]:
        warnings: List[Dict[str, Any]] = []
        residue: List[Dict[str, Any]] = []

        if self.temp_path.exists():
            valid_temp = False
            try:
                with open(self.temp_path, "rb") as handle:
                    json.loads(handle.read().decode("utf-8"))
                valid_temp = True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                valid_temp = False
            action = self._quarantine(self.temp_path, "half-write-residue")
            item = {"path": str(self.temp_path), "parseable": valid_temp, "action": action}
            residue.append(item)
            warnings.append({"code": "W_CHECKPOINT_TEMP_RESIDUE", **item})

        ledger.delete_incomplete_checkpoints()
        checkpoint = ledger.latest_checkpoint()
        if checkpoint is None:
            if self.path.exists():
                try:
                    snapshot = self.read_snapshot()
                    warnings.append(
                        {
                            "code": "W_ORPHAN_CHECKPOINT_SNAPSHOT",
                            "watermark": snapshot.get("watermark"),
                            "action": "ignored until a new checkpoint is written",
                        }
                    )
                except (OSError, json.JSONDecodeError, CheckpointError):
                    action = self._quarantine(self.path, "corrupt")
                    warnings.append({"code": "W_CORRUPT_CHECKPOINT_SNAPSHOT", "action": action})
            return {
                "recovered": False,
                "resumed_from_batch_watermark": 0,
                "watermark": 0,
                "ledger_digest": ledger.compute_digest(),
                "warnings": warnings,
                "residue": residue,
            }

        verification = ledger.verify_digest(checkpoint["ledger_digest"])
        snapshot = None
        snapshot_status = "ok"
        if self.path.exists():
            try:
                snapshot = self.read_snapshot()
                if (
                    snapshot.get("watermark") != checkpoint["watermark"]
                    or snapshot.get("batch_watermark") != checkpoint["batch_watermark"]
                    or snapshot.get("ledger_digest") != checkpoint["ledger_digest"]
                ):
                    snapshot_status = "mismatch"
                    warnings.append(
                        {
                            "code": "W_CHECKPOINT_SNAPSHOT_MISMATCH",
                            "action": "restored authoritative snapshot from SQLite",
                            "sqlite": {
                                "watermark": checkpoint["watermark"],
                                "batch_watermark": checkpoint["batch_watermark"],
                            },
                            "file": {
                                "watermark": (snapshot or {}).get("watermark"),
                                "batch_watermark": (snapshot or {}).get("batch_watermark"),
                            },
                        }
                    )
                    snapshot = None
            except (OSError, json.JSONDecodeError, CheckpointError) as exc:
                snapshot_status = "corrupt"
                warnings.append(
                    {
                        "code": "W_CORRUPT_CHECKPOINT_SNAPSHOT",
                        "error": str(exc),
                        "action": "restored authoritative snapshot from SQLite",
                    }
                )
                self._quarantine(self.path, "corrupt")
        else:
            snapshot_status = "missing"
            warnings.append(
                {
                    "code": "W_CHECKPOINT_SNAPSHOT_MISSING",
                    "action": "restored authoritative snapshot from SQLite",
                }
            )

        if snapshot is None:
            detail = json.loads(checkpoint["detail_json"] or "{}")
            payload = {
                "checkpoint_version": 1,
                "watermark": checkpoint["watermark"],
                "batch_watermark": checkpoint["batch_watermark"],
                "ledger_digest": checkpoint["ledger_digest"],
                "batch_size": checkpoint["batch_size"],
                "detail": detail,
            }
            self.write_snapshot(payload)

        return {
            "recovered": True,
            "resumed_from_batch_watermark": checkpoint["batch_watermark"],
            "watermark": checkpoint["watermark"],
            "batch_size": checkpoint["batch_size"],
            "ledger_digest": verification["ledger_digest"],
            "snapshot_status": snapshot_status,
            "warnings": warnings,
            "residue": residue,
        }

    def snapshot_for_checkpoint(self, checkpoint: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "checkpoint_version": 1,
            "watermark": checkpoint["watermark"],
            "batch_watermark": checkpoint["batch_watermark"],
            "ledger_digest": checkpoint["ledger_digest"],
            "batch_size": checkpoint["batch_size"],
            "detail": detail,
        }
