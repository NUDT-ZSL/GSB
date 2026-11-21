from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_PARTIAL,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    BusinessError,
)
from .util import atomic_write_json, canonical_json_bytes, json_digest, read_json


STAGE_ORDER = {
    "model_package_validation": 1,
    "layer_quantization": 2,
    "calibration_statistics": 3,
    "layer_inference": 4,
}
STAGE_BY_INDEX = {value: key for key, value in STAGE_ORDER.items()}

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


class InterProcessFileLock:
    def __init__(self, path: Path):
        self.path = path
        self._thread_lock = _process_lock(path)
        self._handle = None
        self._acquired = False

    def acquire(self) -> None:
        if not self._thread_lock.acquire(blocking=False):
            raise BusinessError(
                E_STAGE_BUSY,
                "another stage commit is already in progress",
                stage="commit",
                trace=["snapshots:commit_guard"],
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle = handle
            self._acquired = True
        except (BlockingIOError, OSError) as exc:
            try:
                handle.close()
            except UnboundLocalError:
                pass
            self._thread_lock.release()
            raise BusinessError(
                E_STAGE_BUSY,
                "another process is committing a stage",
                stage="commit",
                details={"lock_path": str(self.path)},
                trace=["snapshots:commit_guard"],
            ) from exc

    def release(self) -> None:
        if not self._acquired or self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._acquired = False
            self._thread_lock.release()


class SnapshotStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.snapshot_dir = base_dir / "snapshots"
        self.current_path = base_dir / "current.json"
        self.commit_lock_path = base_dir / "commit.lock"
        self.inflight_path = base_dir / "inflight.json"
        self.events_path = base_dir / "events.jsonl"
        self.events_lock_path = base_dir / "events.lock"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def commit_guard(self) -> Iterator[None]:
        lock = InterProcessFileLock(self.commit_lock_path)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _snapshot_path(self, version: int) -> Path:
        return self.snapshot_dir / f"v{version:012d}.json"

    @staticmethod
    def _content_checksum(payload: dict[str, Any]) -> str:
        return json_digest(
            {key: value for key, value in payload.items() if key != "content_checksum"}
        )

    def load_latest(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            pointer = read_json(self.current_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "current snapshot pointer is unreadable",
                stage="unknown",
                details={"path": str(self.current_path)},
            ) from exc
        version = pointer.get("version")
        stage = pointer.get("stage", "unknown")
        if not isinstance(version, int):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "current snapshot pointer has an invalid version",
                stage=stage,
                details={"pointer": pointer},
            )
        snapshot = self._load_snapshot_version(version, pointed_stage=stage)
        if pointer.get("content_checksum") != snapshot.get("content_checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot pointer checksum does not match content",
                stage=stage,
                version=version,
            )
        return snapshot

    def _load_snapshot_version(
        self, version: int, *, pointed_stage: str | None = None
    ) -> dict[str, Any]:
        path = self._snapshot_path(version)
        try:
            snapshot = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot file is missing or unreadable",
                stage=pointed_stage or "unknown",
                version=version,
                details={"path": str(path)},
            ) from exc
        actual_checksum = self._content_checksum(snapshot)
        expected_checksum = snapshot.get("content_checksum")
        if actual_checksum != expected_checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot content checksum mismatch",
                stage=pointed_stage or str(snapshot.get("stage", "unknown")),
                version=version,
                details={
                    "expected_checksum": expected_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        if snapshot.get("version") != version or (
            pointed_stage is not None and snapshot.get("stage") != pointed_stage
        ):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot pointer and content disagree",
                stage=pointed_stage or str(snapshot.get("stage", "unknown")),
                version=version,
                details={
                    "pointer_stage": pointed_stage,
                    "snapshot_stage": snapshot.get("stage"),
                },
            )
        return snapshot

    def load_latest_with_stage(self, stage: str) -> dict[str, Any] | None:
        for snapshot in reversed(self.committed_lineage()):
            if stage in snapshot.get("stages", {}):
                return snapshot
        return None

    def committed_lineage(self) -> list[dict[str, Any]]:
        latest = self.load_latest()
        if latest is None:
            return []
        lineage = [latest]
        current = latest
        while current.get("prev_version"):
            previous_version = int(current["prev_version"])
            previous = self._load_snapshot_version(previous_version)
            if previous.get("content_checksum") != current.get("prev_checksum"):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot parent checksum does not match its child",
                    stage=str(previous.get("stage", "unknown")),
                    version=previous_version,
                )
            lineage.append(previous)
            current = previous
        lineage.reverse()
        return lineage

    def commit(
        self,
        *,
        stage: str,
        model_tag: str,
        manifest_digest: str,
        config_digest: str,
        layer_order: list[str],
        completed_layers: list[str],
        stages: dict[str, Any],
        dependency: dict[str, Any],
        result_digest: str,
        degradations: list[dict[str, Any]],
        injections: list[dict[str, Any]],
        version_hint: int | None = None,
        parent_checksum: str | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise BusinessError(
                "E_INVALID_STAGE",
                f"unknown stage: {stage}",
                stage=stage,
            )
        if set(completed_layers) != set(layer_order):
            raise BusinessError(
                E_SNAPSHOT_PARTIAL,
                "a stage cannot commit before every layer is complete",
                stage=stage,
                details={
                    "completed_layers": sorted(completed_layers),
                    "required_layers": sorted(layer_order),
                },
                trace=["snapshots:stage_completeness_guard"],
            )
        with self.commit_guard():
            latest = self.load_latest()
            latest_version = int(latest["version"]) if latest else 0
            next_version = latest_version + 1
            if version_hint is not None and version_hint != next_version:
                if version_hint <= latest_version:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "an older snapshot version cannot overwrite current progress",
                        stage=stage,
                        version=version_hint,
                        details={"latest_version": latest_version},
                        trace=["snapshots:rollback_guard"],
                    )
                raise BusinessError(
                    "E_SNAPSHOT_VERSION_GAP",
                    "snapshot version is not the next monotonic version",
                    stage=stage,
                    version=version_hint,
                    details={"expected_version": next_version},
                )
            latest_checksum = latest.get("content_checksum") if latest else None
            if parent_checksum is not None and parent_checksum != latest_checksum:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "stale parent checksum cannot replace the current snapshot",
                    stage=stage,
                    version=latest_version,
                    details={
                        "expected_parent_checksum": latest_checksum,
                        "provided_parent_checksum": parent_checksum,
                    },
                    trace=["snapshots:parent_guard"],
                )
            snapshot: dict[str, Any] = {
                "schema_version": 1,
                "version": next_version,
                "stage": stage,
                "stage_index": STAGE_ORDER[stage],
                "status": "committed",
                "model_tag": model_tag,
                "manifest_digest": manifest_digest,
                "config_digest": config_digest,
                "layer_order": list(layer_order),
                "completed_layers": list(completed_layers),
                "stages": stages,
                "dependency": dependency,
                "result_digest": result_digest,
                "degradations": degradations,
                "injections": injections,
                "prev_version": latest_version,
                "prev_checksum": latest_checksum,
            }
            snapshot["content_checksum"] = self._content_checksum(snapshot)
            snapshot_path = self._snapshot_path(next_version)
            atomic_write_json(snapshot_path, snapshot)
            pointer = {
                "version": next_version,
                "stage": stage,
                "content_checksum": snapshot["content_checksum"],
            }
            atomic_write_json(self.current_path, pointer)
            return snapshot

    def history(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for payload in self.committed_lineage():
            result.append(
                {
                    "version": payload.get("version"),
                    "stage": payload.get("stage"),
                    "stage_index": payload.get("stage_index"),
                    "completed_layers": payload.get("completed_layers", []),
                    "content_checksum": payload.get("content_checksum"),
                    "result_digest": payload.get("result_digest"),
                }
            )
        return result

    def write_inflight(self, value: dict[str, Any]) -> None:
        atomic_write_json(self.inflight_path, value)

    def read_inflight(self) -> dict[str, Any] | None:
        if not self.inflight_path.exists():
            return None
        try:
            return read_json(self.inflight_path)
        except (OSError, json.JSONDecodeError):
            return {
                "status": "unreadable",
                "path": str(self.inflight_path),
            }

    def clear_inflight(self) -> None:
        self.inflight_path.unlink(missing_ok=True)

    def append_event(self, value: dict[str, Any]) -> None:
        lock = InterProcessFileLock(self.events_lock_path)
        lock.acquire()
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            sequence = 0
            if self.events_path.exists():
                with self.events_path.open("rb") as handle:
                    sequence = sum(1 for _ in handle)
            event = dict(value)
            event["event_seq"] = sequence + 1
            with self.events_path.open("ab") as handle:
                handle.write(canonical_json_bytes(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            lock.release()

    def load_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError:
                    rows.append({"event_seq": None, "status": "corrupt_event"})
        return rows[-limit:]
