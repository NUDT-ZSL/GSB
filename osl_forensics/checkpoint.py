"""Complete-checkpoint selection and SQLite snapshot recovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import sha256_hex
from .ledger import Ledger
from .policy import Policy
from .storage import atomic_write_json, read_json, write_snapshot


CHECKPOINT_RE = re.compile(r"checkpoint-(\d{6,})\.json$")


@dataclass
class RecoveredState:
    ledger: Ledger
    checkpoint: dict[str, Any] | None
    recovered_from_sequence: int | None
    restored_snapshot: bool


class CheckpointManager:
    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.checkpoint_dir = self.workdir / "checkpoints"
        self.snapshot_path = self.workdir / "snapshots" / "ledger-snapshot.json"
        self.latest_pointer = self.checkpoint_dir / "latest.json"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        ledger: Ledger,
        completed_file_ids: list[str],
        sequence: int | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if sequence is None:
            latest = self.latest_complete()
            sequence = int(latest["sequence"]) + 1 if latest else 1
        snapshot = ledger.export_snapshot()
        write_snapshot(self.snapshot_path, snapshot)
        snapshot_hash = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()
        tip = ledger.event_tip()
        checkpoint = {
            "kind": "osl-checkpoint",
            "checkpoint_version": 1,
            "sequence": sequence,
            "complete": True,
            "completed_file_ids": sorted(completed_file_ids),
            "policy_fingerprint": ledger.policy.fingerprint(),
            "policy_generation": ledger.policy_generation(),
            "snapshot_file": str(self.snapshot_path.relative_to(self.workdir)),
            "snapshot_sha256": snapshot_hash,
            "ledger_event_count": tip[0] if tip else 0,
            "ledger_tip_hash": tip[1] if tip else None,
        }
        if extra:
            checkpoint["extra"] = extra

        path = self.checkpoint_path(sequence)
        atomic_write_json(path, checkpoint)
        atomic_write_json(self.latest_pointer, checkpoint)
        return checkpoint

    def checkpoint_path(self, sequence: int) -> Path:
        return self.checkpoint_dir / f"checkpoint-{sequence:06d}.json"

    def list_complete_checkpoints(self) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        for path in self.checkpoint_dir.glob("checkpoint-*.json"):
            match = CHECKPOINT_RE.search(path.name)
            if not match:
                continue
            try:
                data = read_json(path)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("kind") != "osl-checkpoint" or data.get("complete") is not True:
                continue
            if int(data.get("sequence", -1)) != int(match.group(1)):
                continue
            data["_path"] = str(path)
            valid.append(data)
        return sorted(valid, key=lambda item: int(item["sequence"]))

    def latest_complete(self) -> dict[str, Any] | None:
        checkpoints = self.list_complete_checkpoints()
        return checkpoints[-1] if checkpoints else None

    def recover(
        self,
        db_path: str | Path,
        policy: Policy | None = None,
    ) -> RecoveredState:
        used_policy = policy or Policy()
        checkpoint = self.latest_complete()
        ledger = Ledger(db_path, used_policy)
        if checkpoint is None:
            return RecoveredState(ledger, None, None, False)

        if checkpoint.get("policy_fingerprint") != used_policy.fingerprint():
            # A whitelist/policy change intentionally starts a rebuilt ledger.
            return RecoveredState(ledger, None, None, False)

        snapshot_path = self.workdir / str(checkpoint["snapshot_file"])
        snapshot = read_json(snapshot_path)
        actual_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        if actual_hash != checkpoint.get("snapshot_sha256"):
            raise ValueError("checkpoint snapshot hash mismatch")
        if snapshot.get("policy") != used_policy.fingerprint():
            raise ValueError("snapshot policy fingerprint mismatch")

        tip = ledger.event_tip()
        expected_count = int(checkpoint.get("ledger_event_count", 0))
        expected_hash = checkpoint.get("ledger_tip_hash")
        matches = (
            (expected_count == 0 and tip is None)
            or (tip is not None and tip[0] == expected_count and tip[1] == expected_hash)
        )
        restored = False
        if not matches:
            ledger.restore_snapshot(snapshot)
            restored = True

        replay = ledger.verify_replay()
        if not replay["ok"]:
            raise ValueError(f"recovered ledger failed replay verification: {replay}")

        return RecoveredState(
            ledger=ledger,
            checkpoint=checkpoint,
            recovered_from_sequence=int(checkpoint["sequence"]),
            restored_snapshot=restored,
        )


def checkpoint_digest(checkpoint: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(checkpoint, sort_keys=True).encode("utf-8"))
