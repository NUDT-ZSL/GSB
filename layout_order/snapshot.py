"""Whole-snapshot versioning, checksums and non-blocking stage commits."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    atomic_write_json,
    content_checksum,
    read_json,
    sha256_obj,
)
from .errors import LayoutError

SECTION_NAMES = ("block_annotations", "type_order", "edge_weights")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class SnapshotSection:
    name: str
    version: int
    checksum: str
    content: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "checksum": self.checksum,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SnapshotSection":
        name = payload.get("name")
        version = payload.get("version")
        checksum = payload.get("checksum")
        if name not in SECTION_NAMES:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot section name is invalid",
                field="name",
                details={"section": name},
            )
        if not _is_int(version) or version < 1:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot section version must be a positive integer",
                field="version",
                details={"section": name},
            )
        if not isinstance(checksum, str) or not checksum:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot section checksum is missing",
                field="checksum",
                details={"section": name},
            )
        if "content" not in payload:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot section content is missing",
                field="content",
                details={"section": name},
            )
        actual = content_checksum(payload["content"])
        if actual != checksum:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot section checksum does not match content",
                field="checksum",
                details={
                    "section": name,
                    "expected": checksum,
                    "actual": actual,
                },
            )
        return cls(
            name=name,
            version=version,
            checksum=checksum,
            content=payload["content"],
        )


@dataclass(frozen=True)
class LayoutSnapshot:
    sections: dict[str, SnapshotSection]
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        missing = [name for name in SECTION_NAMES if name not in self.sections]
        extra = [name for name in self.sections if name not in SECTION_NAMES]
        if missing or extra:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot must contain exactly the three declared sections",
                details={"missing": missing, "extra": extra},
            )
        if not self.snapshot_id:
            object.__setattr__(self, "snapshot_id", self.compute_id())

    def compute_id(self) -> str:
        return sha256_obj(
            {
                name: {
                    "version": self.sections[name].version,
                    "checksum": self.sections[name].checksum,
                }
                for name in SECTION_NAMES
            }
        )

    def section(self, name: str) -> SnapshotSection:
        return self.sections[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "sections": {
                name: self.sections[name].to_dict() for name in SECTION_NAMES
            },
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "sections": {
                name: {
                    "version": self.sections[name].version,
                    "checksum": self.sections[name].checksum,
                }
                for name in SECTION_NAMES
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LayoutSnapshot":
        sections_payload = payload.get("sections")
        if not isinstance(sections_payload, dict):
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot sections are missing",
                field="sections",
            )
        sections: dict[str, SnapshotSection] = {}
        for name in SECTION_NAMES:
            item = sections_payload.get(name)
            if not isinstance(item, dict):
                raise LayoutError(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot section is missing",
                    field=f"sections.{name}",
                )
            section = SnapshotSection.from_dict(item)
            if section.name != name:
                raise LayoutError(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot section key and name differ",
                    field=f"sections.{name}",
                )
            sections[name] = section
        snapshot = cls(sections=sections)
        declared_id = payload.get("snapshot_id")
        if declared_id is not None and declared_id != snapshot.snapshot_id:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot id does not match section descriptors",
                field="snapshot_id",
                details={
                    "expected": declared_id,
                    "actual": snapshot.snapshot_id,
                },
            )
        return snapshot


def make_section(name: str, version: int, content: Any) -> SnapshotSection:
    if name not in SECTION_NAMES:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "unknown snapshot section",
            field="section",
            details={"section": name},
        )
    if not _is_int(version) or version < 1:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "snapshot version must be a positive integer",
            field=f"{name}.version",
        )
    return SnapshotSection(
        name=name,
        version=version,
        checksum=content_checksum(content),
        content=content,
    )


def build_snapshot(
    block_annotations: dict[str, Any],
    type_order: dict[str, int],
    edge_weights: dict[str, Any],
    versions: dict[str, int] | None = None,
) -> LayoutSnapshot:
    supplied_versions = dict(versions or {})
    sections = {
        "block_annotations": make_section(
            "block_annotations",
            supplied_versions.get("block_annotations", 1),
            block_annotations,
        ),
        "type_order": make_section(
            "type_order",
            supplied_versions.get("type_order", 1),
            type_order,
        ),
        "edge_weights": make_section(
            "edge_weights",
            supplied_versions.get("edge_weights", 1),
            edge_weights,
        ),
    }
    return LayoutSnapshot(sections=sections)


def replace_snapshot_section(
    snapshot: LayoutSnapshot,
    name: str,
    content: Any,
    *,
    version: int | None = None,
) -> LayoutSnapshot:
    current = snapshot.section(name)
    next_version = current.version + 1 if version is None else version
    sections = dict(snapshot.sections)
    sections[name] = make_section(name, next_version, content)
    return LayoutSnapshot(sections=sections)


@dataclass
class CommitResult:
    snapshot: LayoutSnapshot
    changed: bool
    previous_snapshot_id: str | None
    commit_lock_wait_ms: int = 0


class SnapshotStore:
    """Single-writer snapshot store with optimistic whole-snapshot replacement."""

    def __init__(self, directory: str | Path, *, autoload: bool = True) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current_path = self.directory / "current.json"
        self.lock_path = self.directory / "stage.lock"
        self._thread_guard = threading.Lock()
        self._lease_fd: int | None = None
        self.current: LayoutSnapshot | None = None
        self.last_commit: CommitResult | None = None
        if autoload and self.current_path.exists():
            self.current = self._load_current()

    def _load_current(self) -> LayoutSnapshot:
        try:
            payload = read_json(self.current_path)
        except Exception as exc:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot file cannot be read or parsed",
                field="current.json",
                details={"error": str(exc)},
            ) from exc
        try:
            return LayoutSnapshot.from_dict(payload)
        except LayoutError:
            raise
        except Exception as exc:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot file schema is invalid",
                field="current.json",
                details={"error": str(exc)},
            ) from exc

    def _acquire_stage(self) -> None:
        if not self._thread_guard.acquire(blocking=False):
            raise LayoutError(
                "E_STAGE_BUSY",
                "another snapshot stage is active",
                field="stage",
            )
        fd: int | None = None
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if fd is not None:
                os.close(fd)
            self._thread_guard.release()
            raise LayoutError(
                "E_STAGE_BUSY",
                "another snapshot stage is active",
                field="stage",
            ) from None
        self._lease_fd = fd

    def _release_stage(self) -> None:
        if self._lease_fd is not None:
            try:
                fcntl.flock(self._lease_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lease_fd)
                self._lease_fd = None
        try:
            self._thread_guard.release()
        except RuntimeError:
            pass

    @contextmanager
    def stage_lease(self) -> Iterator["SnapshotStore"]:
        self._acquire_stage()
        try:
            yield self
        finally:
            self._release_stage()

    def _validate_candidate(self, candidate: LayoutSnapshot) -> None:
        missing = [name for name in SECTION_NAMES if name not in candidate.sections]
        extra = [name for name in candidate.sections if name not in SECTION_NAMES]
        if missing or extra:
            raise LayoutError(
                "E_SNAPSHOT_CORRUPT",
                "candidate snapshot must contain exactly three sections",
                details={"missing": missing, "extra": extra},
            )
        for name in SECTION_NAMES:
            section = candidate.section(name)
            actual = content_checksum(section.content)
            if actual != section.checksum:
                raise LayoutError(
                    "E_SNAPSHOT_CORRUPT",
                    "candidate snapshot checksum does not match content",
                    field=f"{name}.checksum",
                    details={"expected": section.checksum, "actual": actual},
                )
            if section.version < 1:
                raise LayoutError(
                    "E_SNAPSHOT_CORRUPT",
                    "candidate snapshot version is invalid",
                    field=f"{name}.version",
                )

    def _check_rollback(self, candidate: LayoutSnapshot) -> None:
        if self.current is None:
            return
        for name in SECTION_NAMES:
            old = self.current.section(name)
            new = candidate.section(name)
            if new.version < old.version:
                raise LayoutError(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot section version is older than the committed version",
                    field=f"{name}.version",
                    details={"current": old.version, "incoming": new.version},
                )
            if new.version == old.version and new.checksum != old.checksum:
                raise LayoutError(
                    "E_SNAPSHOT_ROLLBACK",
                    "same-version snapshot content cannot overwrite committed content",
                    field=f"{name}.checksum",
                    details={
                        "version": new.version,
                        "current": old.checksum,
                        "incoming": new.checksum,
                    },
                )

    def commit(self, candidate: LayoutSnapshot) -> CommitResult:
        self._acquire_stage()
        try:
            self._validate_candidate(candidate)
            self._check_rollback(candidate)
            previous = self.current.snapshot_id if self.current else None
            changed = self.current is None or candidate.snapshot_id != previous
            if changed:
                atomic_write_json(self.current_path, candidate.to_dict())
                self.current = candidate
            result = CommitResult(
                snapshot=self.current or candidate,
                changed=changed,
                previous_snapshot_id=previous,
            )
            self.last_commit = result
            return result
        finally:
            self._release_stage()

