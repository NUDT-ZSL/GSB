from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .hashing import atomic_write_json, read_json, sha256_json


STAGE_ORDER = ("input", "reference", "plan", "chunk", "commit")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "batch"


class CheckpointManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.root / "latest.json"

    def stage_path(self, batch_id: str, stage: str) -> Path:
        return self.root / f"{safe_name(batch_id)}-{stage}.json"

    def save_stage(
        self,
        *,
        batch_id: str,
        target_version: int,
        stage: str,
        completed_jobs: list[str],
        job_checksums: dict[str, str],
    ) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise ValueError(f"unknown stage: {stage}")
        payload = {
            "schema": "opverify-checkpoint-v1",
            "batch_id": batch_id,
            "target_version": int(target_version),
            "stage": stage,
            "completed_jobs": sorted(completed_jobs),
            "job_checksums": {key: job_checksums[key] for key in sorted(job_checksums)},
        }
        envelope = {**payload, "checksum": sha256_json(payload)}
        path = self.stage_path(batch_id, stage)
        atomic_write_json(path, envelope)
        latest_payload = {
            "schema": "opverify-checkpoint-latest-v1",
            "batch_id": batch_id,
            "target_version": int(target_version),
            "stage": stage,
            "stage_index": STAGE_ORDER.index(stage),
            "stage_file": path.name,
            "checksum": envelope["checksum"],
        }
        latest = {**latest_payload, "latest_checksum": sha256_json(latest_payload)}
        atomic_write_json(self.latest_path, latest)
        return envelope

    def load_latest(self, batch_id: str | None = None) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        latest = read_json(self.latest_path)
        expected_latest_checksum = latest.pop("latest_checksum", None)
        if expected_latest_checksum != sha256_json(latest):
            return None
        if batch_id is not None and latest.get("batch_id") != batch_id:
            return None
        stage_path = self.root / latest["stage_file"]
        if not stage_path.exists():
            return None
        envelope = read_json(stage_path)
        checksum = envelope.pop("checksum", None)
        if checksum != sha256_json(envelope):
            return None
        if envelope.get("checksum") is not None:
            envelope["checksum"] = checksum
        else:
            envelope["checksum"] = checksum
        return envelope
