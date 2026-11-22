"""Atomic, checksummed stage snapshots and single-writer commit control."""

from __future__ import annotations

import fcntl
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .serialization import (
    atomic_write_json,
    canonical_bytes,
    checksum,
    read_json,
)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
SNAPSHOT_STAGE_NAMES = {
    1: "preprocess",
    2: "detect",
    3: "recognize",
    4: "aggregate",
}


def _stage_label(stage: int) -> str:
    return SNAPSHOT_STAGE_NAMES.get(stage, f"stage-{stage}")


def _thread_lock_for(path: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[path] = lock
        return lock


class CommitGuard:
    """Nonblocking process/thread guard for one active stage commit."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / ".commit.lock"
        self._thread_lock = _thread_lock_for(str(self.path))
        self._fd: int | None = None

    def __enter__(self) -> "CommitGuard":
        if not self._thread_lock.acquire(blocking=False):
            raise BusinessError(
                E_STAGE_BUSY,
                "another stage commit is already active",
                stage="snapshot",
                field="commit_guard",
            )
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusinessError(
                    E_STAGE_BUSY,
                    "another process is committing a stage",
                    stage="snapshot",
                    field="commit_guard",
                ) from exc
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._thread_lock.release()


@dataclass(frozen=True)
class SnapshotRecord:
    stage: int
    stage_name: str
    version: int
    signature: str
    completed_pages: tuple[str, ...]
    content_checksum: str
    content: dict[str, Any]
    raw: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_name": self.stage_name,
            "version": self.version,
            "signature": self.signature,
            "completed_pages": list(self.completed_pages),
            "page_count": len(self.completed_pages),
            "content_checksum": self.content_checksum,
            "header_checksum": self.raw.get("header_checksum"),
            "config_version": self.raw.get("config_version"),
            "config_checksum": self.raw.get("config_checksum"),
            "template_version": self.raw.get("template_version"),
            "template_checksum": self.raw.get("template_checksum"),
            "dataset_checksum": self.raw.get("dataset_checksum"),
        }


class SnapshotStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"

    def stage_path(self, stage: int) -> Path:
        return self.state_dir / f"stage_{stage}.json"

    @staticmethod
    def _header_digest(header: dict[str, Any]) -> str:
        return checksum(
            {
                key: value
                for key, value in header.items()
                if key != "header_checksum"
            }
        )

    def _read_header(self, stage: int) -> dict[str, Any] | None:
        path = self.stage_path(stage)
        if not path.exists():
            return None
        try:
            value = read_json(path)
        except Exception as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot is not valid JSON",
                stage=_stage_label(stage),
                details={"path": str(path), "stage_number": stage, "stage_name": _stage_label(stage)},
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot header is invalid",
                stage=_stage_label(stage),
                details={"stage_number": stage},
            )
        return value

    def _max_version(self) -> int:
        versions = [0]
        for stage in range(1, 5):
            header = self._read_header(stage)
            if header is not None:
                try:
                    versions.append(int(header.get("version", 0)))
                except (TypeError, ValueError):
                    raise BusinessError(
                        E_SNAPSHOT_CORRUPT,
                        f"stage {stage} snapshot has an invalid version",
                        stage=_stage_label(stage),
                        details={"stage_number": stage, "version": header.get("version")},
                    )
        if self.state_path.exists():
            try:
                state = read_json(self.state_path)
                versions.append(int(state.get("max_version", 0)))
            except Exception as exc:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot state ledger is corrupt",
                    stage="state",
                ) from exc
        return max(versions)

    def _write_state(self, stage: int, version: int) -> None:
        state = {
            "schema_version": 1,
            "max_version": int(version),
            "last_committed_stage": int(stage),
            "stages": {},
        }
        for candidate in range(1, 5):
            header = self._read_header(candidate)
            if header is not None:
                state["stages"][str(candidate)] = {
                    "version": int(header.get("version", 0)),
                    "signature": header.get("signature"),
                    "completed_pages": header.get("completed_pages", []),
                    "content_checksum": header.get("content_checksum"),
                    "config_version": header.get("config_version"),
                    "config_checksum": header.get("config_checksum"),
                    "template_version": header.get("template_version"),
                    "template_checksum": header.get("template_checksum"),
                    "dataset_checksum": header.get("dataset_checksum"),
                }
        atomic_write_json(self.state_path, state)

    def load(
        self,
        stage: int,
        *,
        signature: str | None = None,
        expected_pages: set[str] | None = None,
    ) -> SnapshotRecord | None:
        path = self.stage_path(stage)
        if not path.exists():
            return None
        header = self._read_header(stage)
        assert header is not None
        version = header.get("version")
        content = header.get("content")
        content_checksum = header.get("content_checksum")
        if not isinstance(version, int) or not isinstance(content, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot has an invalid header",
                stage=_stage_label(stage),
                details={"stage_number": stage, "version": version},
            )
        if header.get("header_checksum") != self._header_digest(header):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot header checksum does not match",
                stage=_stage_label(stage),
                details={
                    "stage_number": stage,
                    "version": version,
                    "stage_name": header.get("stage_name"),
                    "expected": header.get("header_checksum"),
                    "actual": self._header_digest(header),
                },
            )
        actual_checksum = checksum(content)
        if actual_checksum != content_checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot content checksum does not match",
                stage=_stage_label(stage),
                details={
                    "stage_number": stage,
                    "version": version,
                    "stage_name": header.get("stage_name"),
                    "expected": content_checksum,
                    "actual": actual_checksum,
                },
            )
        pages = content.get("pages")
        if not isinstance(pages, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot has no page map",
                stage=_stage_label(stage),
                details={"stage_number": stage, "version": version, "stage_name": header.get("stage_name")},
            )
        completed = tuple(sorted(str(page_id) for page_id in pages))
        declared = tuple(sorted(str(page_id) for page_id in header.get("completed_pages", [])))
        if completed != declared:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} completed page set does not match content",
                stage=_stage_label(stage),
                details={"stage_number": stage, "version": version, "stage_name": header.get("stage_name")},
            )
        if expected_pages is not None and set(completed) != set(expected_pages):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"stage {stage} snapshot does not contain the complete page set",
                stage=_stage_label(stage),
                details={
                    "stage_number": stage,
                    "version": version,
                    "stage_name": header.get("stage_name"),
                    "missing": sorted(set(expected_pages) - set(completed)),
                    "extra": sorted(set(completed) - set(expected_pages)),
                },
            )
        if signature is not None and header.get("signature") != signature:
            return None
        return SnapshotRecord(
            stage=stage,
            stage_name=str(header.get("stage_name", "")),
            version=version,
            signature=str(header.get("signature", "")),
            completed_pages=completed,
            content_checksum=str(content_checksum),
            content=content,
            raw=header,
        )

    def load_compatible(
        self,
        stage: int,
        *,
        signature: str,
        expected_pages: set[str],
    ) -> tuple[SnapshotRecord | None, SnapshotRecord | None]:
        record = self.load(stage, expected_pages=expected_pages)
        if record is None:
            return None, None
        if record.signature == signature:
            return record, None
        return None, record

    def commit_stage(
        self,
        stage: int,
        *,
        stage_name: str,
        signature: str,
        content: dict[str, Any],
        expected_pages: set[str],
        explicit_version: int | None = None,
        identity: dict[str, Any] | None = None,
    ) -> SnapshotRecord:
        pages = content.get("pages")
        if not isinstance(pages, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "refusing to commit a stage without a page map",
                stage=_stage_label(stage),
                details={"stage_number": stage},
            )
        completed = set(str(page_id) for page_id in pages)
        if completed != set(expected_pages):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "refusing to commit an incomplete stage",
                stage=_stage_label(stage),
                details={
                    "stage_number": stage,
                    "missing": sorted(set(expected_pages) - completed),
                    "extra": sorted(completed - set(expected_pages)),
                },
            )
        with CommitGuard(self.state_dir):
            existing = self._read_header(stage)
            current_version = int(existing.get("version", 0)) if existing else 0
            max_version = self._max_version()
            if explicit_version is not None:
                version = int(explicit_version)
                if version <= max_version or version <= current_version:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot version would overwrite a newer committed version",
                        stage=_stage_label(stage),
                        details={
                            "stage_number": stage,
                            "requested_version": version,
                            "current_stage_version": current_version,
                            "max_committed_version": max_version,
                        },
                    )
            else:
                version = max_version + 1
                if version <= current_version:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "computed snapshot version is not newer than the existing stage version",
                        stage=_stage_label(stage),
                        details={
                            "stage_number": stage,
                            "computed_version": version,
                            "current_stage_version": current_version,
                        },
                    )
            content_checksum = checksum(content)
            header = {
                "schema_version": 1,
                "stage": stage,
                "stage_name": stage_name,
                "version": version,
                "signature": signature,
                "config_version": (identity or {}).get("config_version"),
                "config_checksum": (identity or {}).get("config_checksum"),
                "template_version": (identity or {}).get("template_version"),
                "template_checksum": (identity or {}).get("template_checksum"),
                "dataset_checksum": (identity or {}).get("dataset_checksum"),
                "completed_pages": sorted(completed),
                "page_count": len(completed),
                "content_checksum": content_checksum,
                "content": content,
            }
            header["header_checksum"] = self._header_digest(header)
            atomic_write_json(self.stage_path(stage), header)
            for downstream in range(stage + 1, 5):
                try:
                    self.stage_path(downstream).unlink()
                except FileNotFoundError:
                    pass
            self._write_state(stage, version)
            return SnapshotRecord(
                stage=stage,
                stage_name=stage_name,
                version=version,
                signature=signature,
                completed_pages=tuple(sorted(completed)),
                content_checksum=content_checksum,
                content=content,
                raw=header,
            )

    def invalidate_from(self, stage: int) -> None:
        with CommitGuard(self.state_dir):
            for candidate in range(stage, 5):
                try:
                    self.stage_path(candidate).unlink()
                except FileNotFoundError:
                    pass
            header = self._read_header(max(1, stage - 1)) if stage > 1 else None
            max_version = self._max_version()
            last_stage = stage - 1
            if header is None:
                last_stage = 0
            state = {
                "schema_version": 1,
                "max_version": max_version,
                "last_committed_stage": last_stage,
                "stages": {},
            }
            for candidate in range(1, 5):
                item = self._read_header(candidate)
                if item is not None:
                    state["stages"][str(candidate)] = {
                        "version": int(item.get("version", 0)),
                        "signature": item.get("signature"),
                        "completed_pages": item.get("completed_pages", []),
                        "content_checksum": item.get("content_checksum"),
                        "config_version": item.get("config_version"),
                        "config_checksum": item.get("config_checksum"),
                        "template_version": item.get("template_version"),
                        "template_checksum": item.get("template_checksum"),
                        "dataset_checksum": item.get("dataset_checksum"),
                    }
            atomic_write_json(self.state_path, state)

    def metadata(self) -> dict[str, Any]:
        stages: dict[str, Any] = {}
        for stage in range(1, 5):
            header = self._read_header(stage)
            if header is None:
                continue
            stages[str(stage)] = {
                "stage": stage,
                "stage_name": header.get("stage_name"),
                "version": header.get("version"),
                "signature": header.get("signature"),
                "completed_pages": header.get("completed_pages", []),
                "page_count": header.get("page_count", 0),
                "content_checksum": header.get("content_checksum"),
                "header_checksum": header.get("header_checksum"),
                "config_version": header.get("config_version"),
                "config_checksum": header.get("config_checksum"),
                "template_version": header.get("template_version"),
                "template_checksum": header.get("template_checksum"),
                "dataset_checksum": header.get("dataset_checksum"),
            }
        ledger = read_json(self.state_path) if self.state_path.exists() else {}
        return {
            "state_dir": str(self.state_dir),
            "max_version": ledger.get("max_version", 0),
            "last_committed_stage": ledger.get("last_committed_stage", 0),
            "stages": stages,
        }

    def verify_all(self, expected_pages: set[str]) -> list[dict[str, Any]]:
        verified = []
        for stage in range(1, 5):
            record = self.load(stage, expected_pages=expected_pages)
            if record is not None:
                verified.append(record.metadata())
        return verified

    @staticmethod
    def corrupt_payload_digest(snapshot: dict[str, Any]) -> bytes:
        return canonical_bytes(snapshot)

