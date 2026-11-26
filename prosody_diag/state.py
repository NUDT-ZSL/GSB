from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_hash, read_json, write_json_atomic
from .errors import E_STAGE_BUSY, E_STATE_CORRUPT, DiagnosticError


STAGES = ("features", "adjudication", "error_spectrum", "report")


class StateStore:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.state_dir = self.output_dir / "state"
        self.snapshot_dir = self.state_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.state_dir / "commit.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def commit_lock(self) -> Iterator[Any]:
        handle = self.lock_path.open("a+b")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DiagnosticError(
                    E_STAGE_BUSY,
                    "another snapshot or stage commit is active",
                    field="commit_lock",
                    details={"lock_path": str(self.lock_path)},
                ) from exc
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def latest_path(self, stage: str) -> Path:
        return self.state_dir / f"{stage}.latest.json"

    def load_latest(self, stage: str) -> dict[str, Any] | None:
        path = self.latest_path(stage)
        if not path.exists():
            return None
        snapshot = read_json(path)
        expected_hash = snapshot.get("content_sha256")
        content = {
            "completed_utterance_ids": snapshot.get("completed_utterance_ids"),
            "next_utterance_id": snapshot.get("next_utterance_id"),
            "stage": snapshot.get("stage"),
            "artifact_hashes": snapshot.get("artifact_hashes", {}),
        }
        if expected_hash and canonical_hash(content) != expected_hash:
            raise DiagnosticError(
                E_STATE_CORRUPT,
                "stage snapshot checksum does not match its content",
                field=f"state.{stage}.latest.json",
            )
        return snapshot

    def latest_committed_stage_index(self) -> int:
        latest = -1
        for index, stage in enumerate(STAGES):
            snapshot = self.load_latest(stage)
            if snapshot and snapshot.get("stage") == stage:
                latest = index
        return latest

    def next_version(self, stage: str) -> int:
        pattern = re.compile(rf"^{re.escape(stage)}\.v(\d+)\.json$")
        versions = [0]
        for path in self.snapshot_dir.glob(f"{stage}.v*.json"):
            match = pattern.match(path.name)
            if match:
                versions.append(int(match.group(1)))
        return max(versions) + 1

    def commit_snapshot(
        self,
        stage: str,
        completed_utterance_ids: list[str],
        *,
        artifact_hashes: dict[str, str] | None = None,
        next_utterance_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        with self.commit_lock():
            version = self.next_version(stage)
            content = {
                "stage": stage,
                "completed_utterance_ids": list(completed_utterance_ids),
                "next_utterance_id": next_utterance_id,
                "artifact_hashes": artifact_hashes or {},
            }
            snapshot = {
                **content,
                "version": version,
                "utterance_count": len(completed_utterance_ids),
                "content_sha256": canonical_hash(content),
                **(extra or {}),
            }
            versioned = self.snapshot_dir / f"{stage}.v{version:06d}.json"
            write_json_atomic(versioned, snapshot)
            write_json_atomic(self.latest_path(stage), snapshot)
            return snapshot

    def hold_lock(self, seconds: float) -> None:
        import time

        with self.commit_lock():
            time.sleep(seconds)
