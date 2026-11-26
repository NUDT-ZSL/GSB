"""Persistent stage snapshots and crash-recovery helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

from .canonical import atomic_write_json, canonical_bytes, read_json, sha256_hex
from .errors import DiagnosticError, ErrorCode


STAGES = (
    "feature_extraction",
    "state_anomaly",
    "ledger_migration",
    "report_export",
)


class SnapshotStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.snapshot_dir = self.state_dir / "snapshots"
        self.work_dir = self.state_dir / "work"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def latest_path(self, stage: str) -> Path:
        return self.state_dir / f"{stage}.latest.json"

    def work_path(self, stage: str) -> Path:
        return self.work_dir / f"{stage}.work.json"

    def load_latest(self, stage: str) -> Dict[str, Any] | None:
        path = self.latest_path(stage)
        if not path.exists():
            return None
        snapshot = read_json(path)
        expected = sha256_hex({k: v for k, v in snapshot.items() if k != "snapshot_checksum"})
        if snapshot.get("snapshot_checksum") != expected:
            raise DiagnosticError(
                ErrorCode.CHECKSUM_MISMATCH,
                "stage snapshot checksum mismatch",
                {"stage": stage, "path": str(path)},
            )
        return snapshot

    def list_latest(self) -> Dict[str, Dict[str, Any]]:
        result = {}
        for stage in STAGES:
            snapshot = self.load_latest(stage)
            if snapshot is not None:
                result[stage] = snapshot
        return result

    def current_version(self) -> int:
        versions = [0]
        for path in self.snapshot_dir.glob("*.json"):
            try:
                payload = read_json(path)
                versions.append(int(payload.get("version", 0)))
            except Exception:
                continue
        for stage in STAGES:
            snapshot = self.load_latest(stage)
            if snapshot:
                versions.append(int(snapshot.get("version", 0)))
        return max(versions)

    def begin_work(self, stage: str, payload: Dict[str, Any]) -> Path:
        work = {
            "stage": stage,
            "status": "uncommitted",
            "committed": False,
            **payload,
        }
        path = self.work_path(stage)
        atomic_write_json(path, work)
        return path

    def update_work(self, stage: str, payload: Dict[str, Any]) -> Path:
        existing = {}
        path = self.work_path(stage)
        if path.exists():
            existing = read_json(path)
        existing.update(payload)
        existing["status"] = "uncommitted"
        existing["committed"] = False
        return self.begin_work(stage, existing)

    def discard_uncommitted(self) -> List[str]:
        discarded: List[str] = []
        for path in sorted(self.work_dir.glob("*.json")):
            discarded.append(str(path))
            path.unlink(missing_ok=True)
        for path in self.state_dir.glob("*.tmp"):
            discarded.append(str(path))
            path.unlink(missing_ok=True)
        for path in self.state_dir.glob(".*.tmp"):
            discarded.append(str(path))
            path.unlink(missing_ok=True)
        return discarded

    def commit(
        self,
        stage: str,
        data: Any,
        completed_ranges: Sequence[Sequence[int]],
        *,
        frame_count: int,
        artifact_checksums: Dict[str, str] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if stage not in STAGES:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG, "unknown pipeline stage", {"stage": stage}
            )
        previous = self.load_latest(stage)
        version = self.current_version() + 1
        stage_version = int(previous.get("stage_version", 0)) + 1 if previous else 1
        content_checksum = sha256_hex(data)
        snapshot = {
            "stage": stage,
            "stage_index": STAGES.index(stage) + 1,
            "version": version,
            "stage_version": stage_version,
            "status": "committed",
            "completed": True,
            "frame_count": int(frame_count),
            "completed_ranges": [[int(a), int(b)] for a, b in completed_ranges if b > a],
            "content_checksum": content_checksum,
            "artifact_checksums": artifact_checksums or {},
            "metadata": metadata or {},
        }
        snapshot["snapshot_checksum"] = sha256_hex(
            {k: v for k, v in snapshot.items() if k != "snapshot_checksum"}
        )
        history_path = self.snapshot_dir / f"{stage}-{version:06d}.json"
        atomic_write_json(history_path, snapshot)
        atomic_write_json(self.latest_path(stage), snapshot)
        self.work_path(stage).unlink(missing_ok=True)
        return snapshot


def ranges_cover_all(ranges: Sequence[Sequence[int]], frame_count: int) -> bool:
    covered = 0
    for start, end in sorted((int(a), int(b)) for a, b in ranges):
        if start != covered:
            return False
        covered = max(covered, end)
    return covered == frame_count


class ForcedTermination(RuntimeError):
    """Raised in-process to emulate SIGKILL after uncommitted work."""

    def __init__(self, stage: str, after_frame: int | None = None):
        super().__init__(f"forced termination in {stage} after frame {after_frame}")
        self.stage = stage
        self.after_frame = after_frame
