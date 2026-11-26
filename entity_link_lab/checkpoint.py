"""Mention-boundary checkpoints, commit locking and recovery classification."""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import content_hash, read_json, write_json
from .errors import E_CHECKPOINT_DIRTY, E_STAGE_BUSY, LinkError


STAGES = (
    "candidate_generation",
    "path_selection",
    "evidence_aggregation",
    "invalidation_propagation",
)


class CommitLock:
    """Non-blocking cross-process commit lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = None

    def __enter__(self) -> "CommitLock":
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise LinkError(
                E_STAGE_BUSY,
                "another checkpoint commit is in progress",
                stage="checkpoint.commit",
                details={"lock": str(self.path)},
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


@contextmanager
def session_commit_lock(checkpoint_dir: str | Path) -> Iterator[None]:
    store = CheckpointStore(checkpoint_dir)
    with CommitLock(store.lock_path):
        yield


class CheckpointStore:
    def __init__(self, checkpoint_dir: str | Path) -> None:
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.dir / "latest.json"
        self.lock_path = self.dir / "commit.lock"
        self.status_path = self.dir / "run_status.json"

    def _checkpoint_path(self, version: int) -> Path:
        return self.dir / f"checkpoint-{version:06d}.json"

    def list_versions(self) -> list[int]:
        versions: list[int] = []
        for path in self.dir.glob("checkpoint-*.json"):
            match = re.fullmatch(r"checkpoint-(\d{6})\.json", path.name)
            if match:
                versions.append(int(match.group(1)))
        return sorted(versions)

    def max_committed_version(self) -> int:
        versions = self.list_versions()
        return versions[-1] if versions else 0

    def write_run_status(self, status: str, **details: Any) -> None:
        write_json(self.status_path, {"status": status, **details})

    def read_run_status(self) -> dict[str, Any]:
        if not self.status_path.exists():
            return {"status": "fresh"}
        return read_json(self.status_path)

    def commit(
        self,
        *,
        stage: str,
        processed_mention_ids: list[str],
        completed_doc_ids: list[str],
        state: dict[str, Any],
        expected_version: int | None = None,
        stop_reason: str | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise LinkError(E_CHECKPOINT_DIRTY, f"unknown checkpoint stage {stage!r}")
        with CommitLock(self.lock_path):
            latest_version = self.max_committed_version()
            if expected_version is not None and expected_version != latest_version:
                raise LinkError(
                    E_STAGE_BUSY,
                    "checkpoint parent version conflict",
                    stage="checkpoint.commit",
                    details={
                        "expected_version": expected_version,
                        "latest_version": latest_version,
                    },
                )
            version = latest_version + 1
            payload = {
                "version": version,
                "stage": stage,
                "stage_index": STAGES.index(stage),
                "processed_mention_ids": sorted(processed_mention_ids),
                "completed_doc_ids": sorted(completed_doc_ids),
                "state": state,
                "stop_reason": stop_reason,
            }
            record = {**payload, "content_checksum": content_hash(payload)}
            checkpoint_path = self._checkpoint_path(version)
            tmp_path = checkpoint_path.with_name(f".{checkpoint_path.name}.tmp-{os.getpid()}")
            tmp_path.write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, checkpoint_path)

            latest_tmp = self.latest_path.with_name(f".latest.json.tmp-{os.getpid()}")
            latest_tmp.write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(latest_tmp, self.latest_path)
            return record

    def read_checkpoint_file(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        try:
            record = read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            raise LinkError(
                E_CHECKPOINT_DIRTY,
                "checkpoint is not valid JSON",
                stage="checkpoint.recovery",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        checksum = record.get("content_checksum")
        body = {key: value for key, value in record.items() if key != "content_checksum"}
        if not isinstance(checksum, str) or content_hash(body) != checksum:
            raise LinkError(
                E_CHECKPOINT_DIRTY,
                "checkpoint content checksum mismatch",
                stage="checkpoint.recovery",
                details={"path": str(path)},
            )
        version = record.get("version")
        expected_from_name = None
        match = re.fullmatch(r"checkpoint-(\d{6})\.json", path.name)
        if match:
            expected_from_name = int(match.group(1))
            if version != expected_from_name:
                raise LinkError(
                    E_CHECKPOINT_DIRTY,
                    "checkpoint filename/body version mismatch",
                    stage="checkpoint.recovery",
                    details={"path": str(path), "body_version": version},
                )
        return record

    def load_latest_valid(self) -> dict[str, Any] | None:
        latest_record: dict[str, Any] | None = None
        if self.latest_path.exists():
            latest_record = self.read_checkpoint_file(self.latest_path)
        versions = self.list_versions()
        if not versions:
            return latest_record
        highest = self.read_checkpoint_file(self._checkpoint_path(versions[-1]))
        if latest_record is None or int(highest["version"]) > int(latest_record["version"]):
            # A hard kill can occur after the immutable checkpoint was renamed
            # but before latest.json was replaced. The immutable record is the
            # nearest committed checkpoint and its checksum was validated.
            return highest
        return latest_record

    def inspect_recovery(self) -> dict[str, Any]:
        tmp_files = sorted(path.name for path in self.dir.glob("*.tmp-*"))
        try:
            checkpoint = self.load_latest_valid()
        except LinkError as exc:
            if exc.code != E_CHECKPOINT_DIRTY:
                raise
            previous = self._previous_valid_after_dirty()
            return {
                "mode": "dirty_checkpoint",
                "code": E_CHECKPOINT_DIRTY,
                "error": exc.to_dict(),
                "temporary_files": tmp_files,
                "previous_version": previous.get("version") if previous else None,
            }

        status = self.read_run_status()
        if checkpoint is None:
            return {
                "mode": "fresh",
                "latest_version": 0,
                "temporary_files": tmp_files,
                "run_status": status,
            }
        stop_reason = checkpoint.get("stop_reason") or status.get("last_signal")
        if status.get("status") == "signal_stopped" or (
            isinstance(stop_reason, str) and stop_reason.startswith("signal:")
        ):
            mode = "handleable_signal"
        elif status.get("status") == "complete":
            mode = "completed"
        elif status.get("status") == "running" or tmp_files:
            mode = "forced_termination"
        else:
            mode = "clean_interruption"
        return {
            "mode": mode,
            "latest_version": checkpoint["version"],
            "stage": checkpoint["stage"],
            "checkpoint": checkpoint,
            "temporary_files": tmp_files,
            "run_status": status,
        }

    def _previous_valid_after_dirty(self) -> dict[str, Any] | None:
        for version in reversed(self.list_versions()):
            path = self._checkpoint_path(version)
            try:
                return self.read_checkpoint_file(path)
            except LinkError:
                continue
        return None

    def archive_dirty(self) -> Path:
        archive = self.dir / "dirty"
        archive.mkdir(exist_ok=True)
        for name in ("latest.json",):
            path = self.dir / name
            if path.exists():
                os.replace(path, archive / name)
        for path in self.dir.glob("checkpoint-*.json"):
            try:
                self.read_checkpoint_file(path)
            except LinkError:
                os.replace(path, archive / path.name)
        return archive
