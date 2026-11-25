"""Durable batch/stage checkpoints for kill-and-resume runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .codes import EngineError, ErrorCode
from .hashing import canonical_hash, read_json, write_atomic_json


class CheckpointStore:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.directory = Path(root) / "checkpoints"
        self.path = self.directory / f"{run_id}.json"
        self.batch_directory = self.directory.parent / "checkpoint_batches" / run_id
        self.data: Dict[str, Any] = {
            "run_id": run_id,
            "sequence": 0,
            "stage": "initialized",
            "completed_stages": [],
            "completed_batches": {},
        }

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            envelope = read_json(self.path)
        except Exception as error:  # JSON or IO failure is a corrupt checkpoint.
            raise EngineError(
                ErrorCode.E_CHECKPOINT_CORRUPT,
                f"cannot read checkpoint: {error}",
                {"path": str(self.path)},
            )
        checksum = canonical_hash(envelope.get("data"))
        if envelope.get("checksum") != checksum:
            raise EngineError(
                ErrorCode.E_CHECKPOINT_CORRUPT,
                "checkpoint checksum mismatch",
                {"path": str(self.path)},
            )
        data = envelope["data"]
        if data.get("run_id") != self.run_id:
            raise EngineError(
                ErrorCode.E_CHECKPOINT_CORRUPT,
                "checkpoint run_id does not match current run",
                {"expected": self.run_id, "actual": data.get("run_id")},
            )
        self.data = data
        self.data.setdefault("completed_stages", [])
        self.data.setdefault("completed_batches", {})
        return self.data

    def save(self) -> None:
        self.data["completed_stages"] = sorted(set(self.data["completed_stages"]))
        envelope = {"checksum": canonical_hash(self.data), "data": self.data}
        write_atomic_json(self.path, envelope)

    def is_stage_committed(self, stage: str) -> bool:
        return stage in self.data.get("completed_stages", [])

    def commit_stage(self, stage: str, checksum: Optional[str] = None) -> None:
        stages = set(self.data.get("completed_stages", []))
        if stage in stages:
            return
        stages.add(stage)
        self.data["completed_stages"] = sorted(stages)
        self.data["stage"] = stage
        self.data["sequence"] = int(self.data.get("sequence", 0)) + 1
        self.data.setdefault("stage_checksums", {})[stage] = checksum
        self.save()

    def has_batch(self, batch_id: str) -> bool:
        return batch_id in self.data.get("completed_batches", {})

    def batch_record(self, batch_id: str) -> Optional[Dict[str, Any]]:
        record = self.data.get("completed_batches", {}).get(batch_id)
        if record is None:
            return None
        artifact_path = self.batch_directory / f"{batch_id}.json"
        if not artifact_path.exists():
            raise EngineError(
                ErrorCode.E_CHECKPOINT_CORRUPT,
                "committed batch artifact is missing",
                {"batch_id": batch_id, "path": str(artifact_path)},
            )
        artifact = read_json(artifact_path)
        checksum = canonical_hash(artifact)
        if checksum != record.get("checksum"):
            raise EngineError(
                ErrorCode.E_CHECKPOINT_CORRUPT,
                "committed batch artifact checksum mismatch",
                {"batch_id": batch_id},
            )
        return artifact

    def commit_batch(self, batch_id: str, artifact: Mapping[str, Any]) -> str:
        self.batch_directory.mkdir(parents=True, exist_ok=True)
        artifact_path = self.batch_directory / f"{batch_id}.json"
        checksum = canonical_hash(artifact)
        write_atomic_json(artifact_path, artifact)
        batches = self.data.setdefault("completed_batches", {})
        if batch_id not in batches:
            self.data["sequence"] = int(self.data.get("sequence", 0)) + 1
        batches[batch_id] = {
            "checksum": checksum,
            "row_count": len(artifact.get("row_results", [])),
            "row_ids": list(artifact.get("row_ids", [])),
            "relative_path": str(artifact_path.relative_to(self.batch_directory.parents[1])),
        }
        self.data["stage"] = "encoding_generation"
        self.save()
        return checksum

    def completed_batch_ids(self) -> List[str]:
        return sorted(self.data.get("completed_batches", {}).keys())
