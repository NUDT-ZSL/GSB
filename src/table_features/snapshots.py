"""Whole-snapshot versioning for config, statistics, and selection payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_json, sha256_obj
from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, BusinessError


SNAPSHOT_KINDS = {"config", "statistics", "selection"}
SNAPSHOT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class SnapshotWrite:
    kind: str
    version: int
    checksum: str
    previous_version: int | None
    previous_checksum: str | None
    status: str
    replaced_entirely: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "previous_version": self.previous_version,
            "previous_checksum": self.previous_checksum,
            "status": self.status,
            "replaced_entirely": self.replaced_entirely,
        }


class SnapshotStore:
    """One atomic JSON envelope per snapshot kind.

    Updates always write a complete payload. Hidden merge or patch behaviour is
    intentionally absent.
    """

    def __init__(self, root: str | Path, *, clear: bool = False) -> None:
        self.root = Path(root)
        if clear and self.root.exists():
            for path in sorted(self.root.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str) -> Path:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind: {kind}")
        return self.root / f"{kind}.json"

    @staticmethod
    def _envelope(kind: str, version: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "kind": kind,
            "version": version,
            "checksum": sha256_obj(payload),
            "payload": payload,
        }

    def _read(self, kind: str) -> dict[str, Any] | None:
        path = self._path(kind)
        if not path.exists():
            return None
        try:
            envelope = read_json(path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"{kind} snapshot is unreadable",
                stage="snapshot",
                feature=kind,
                details={"path": str(path)},
            ) from exc
        if not isinstance(envelope, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"{kind} snapshot is not an object",
                stage="snapshot",
                feature=kind,
                details={"path": str(path)},
            )
        if (
            envelope.get("format_version") != SNAPSHOT_FORMAT_VERSION
            or envelope.get("kind") != kind
            or isinstance(envelope.get("version"), bool)
            or not isinstance(envelope.get("version"), int)
            or envelope["version"] < 1
            or not isinstance(envelope.get("checksum"), str)
            or not isinstance(envelope.get("payload"), dict)
        ):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"{kind} snapshot envelope is invalid",
                stage="snapshot",
                feature=kind,
                details={"path": str(path)},
            )
        expected = sha256_obj(envelope["payload"])
        if expected != envelope["checksum"]:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"{kind} snapshot checksum mismatch",
                stage="snapshot",
                feature=kind,
                details={
                    "path": str(path),
                    "expected_checksum": envelope["checksum"],
                    "actual_checksum": expected,
                },
            )
        return envelope

    def get(self, kind: str) -> dict[str, Any] | None:
        return self._read(kind)

    def put(self, kind: str, version: int, payload: dict[str, Any]) -> SnapshotWrite:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind: {kind}")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version must be a positive integer",
                stage="snapshot",
                feature=kind,
                details={"version": version},
            )
        if not isinstance(payload, dict):
            raise ValueError("snapshot payload must be a dictionary")

        envelope = self._envelope(kind, version, payload)
        current = self._read(kind)
        previous_version = current["version"] if current else None
        previous_checksum = current["checksum"] if current else None

        if current is not None and version < current["version"]:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                f"cannot replace {kind} snapshot version {current['version']} with {version}",
                stage="snapshot",
                feature=kind,
                details={
                    "current_version": current["version"],
                    "attempted_version": version,
                    "current_checksum": current["checksum"],
                },
            )

        if current is not None and version == current["version"] and envelope["checksum"] == current[
            "checksum"
        ]:
            return SnapshotWrite(
                kind=kind,
                version=version,
                checksum=envelope["checksum"],
                previous_version=previous_version,
                previous_checksum=previous_checksum,
                status="unchanged",
                replaced_entirely=False,
            )

        atomic_write_json(self._path(kind), envelope)
        if current is None:
            status = "created"
        elif version > current["version"]:
            status = "replaced_new_version"
        else:
            status = "replaced_same_version_changed_checksum"
        return SnapshotWrite(
            kind=kind,
            version=version,
            checksum=envelope["checksum"],
            previous_version=previous_version,
            previous_checksum=previous_checksum,
            status=status,
            replaced_entirely=True,
        )

    def clear(self) -> None:
        for kind in SNAPSHOT_KINDS:
            path = self._path(kind)
            if path.exists():
                path.unlink()

