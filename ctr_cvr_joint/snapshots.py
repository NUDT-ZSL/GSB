"""Monotonic, whole-payload snapshot registry with checksum validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    E_CACHE_CORRUPT,
    E_INVALID_ARGUMENT,
    E_SNAPSHOT_ROLLBACK,
    fail,
)
from .hashing import atomic_write_json, read_json, sha256_obj


@dataclass(frozen=True)
class SnapshotInfo:
    kind: str
    version: int
    checksum: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "path": self.path,
        }


class SnapshotRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _kind_dir(self, kind: str) -> Path:
        if not kind or any(character not in "abcdefghijklmnopqrstuvwxyz_0123456789" for character in kind):
            fail(E_INVALID_ARGUMENT, "invalid snapshot kind", field="kind", kind=kind)
        return self.root / kind

    def _version_path(self, kind: str, version: int) -> Path:
        return self._kind_dir(kind) / f"v{int(version)}.json"

    def _current_path(self, kind: str) -> Path:
        return self._kind_dir(kind) / "current.json"

    def current_info(self, kind: str) -> SnapshotInfo | None:
        path = self._current_path(kind)
        if not path.exists():
            return None
        try:
            manifest = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            fail(
                E_CACHE_CORRUPT,
                "snapshot current manifest is unreadable",
                field="snapshot_current",
                path=str(path),
                reason=str(exc),
            )
        if not isinstance(manifest, dict):
            fail(
                E_CACHE_CORRUPT,
                "snapshot current manifest is invalid",
                field="snapshot_current",
                path=str(path),
            )
        info = SnapshotInfo(
            kind=kind,
            version=int(manifest.get("version", 0)),
            checksum=str(manifest.get("checksum", "")),
            path=str(manifest.get("path", "")),
        )
        self.load(kind, info.version)
        return info

    def commit(self, kind: str, version: int, payload: Any) -> SnapshotInfo:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            fail(
                E_INVALID_ARGUMENT,
                "snapshot version must be a positive integer",
                field="version",
                version=version,
            )
        checksum = sha256_obj(payload)
        current = self.current_info(kind)
        if current is not None:
            if version < current.version:
                fail(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot version rollback is forbidden",
                    kind=kind,
                    attempted_version=version,
                    current_version=current.version,
                )
            if version == current.version:
                if checksum != current.checksum:
                    fail(
                        E_CACHE_CORRUPT,
                        "same snapshot version arrived with a different checksum",
                        kind=kind,
                        version=version,
                        expected_checksum=current.checksum,
                        actual_checksum=checksum,
                    )
                return current
        wrapper = {
            "schema": 1,
            "kind": kind,
            "version": version,
            "checksum": checksum,
            "payload": payload,
        }
        path = self._version_path(kind, version)
        atomic_write_json(path, wrapper)
        manifest = {
            "kind": kind,
            "version": version,
            "checksum": checksum,
            "path": str(path),
        }
        atomic_write_json(self._current_path(kind), manifest)
        return SnapshotInfo(kind=kind, version=version, checksum=checksum, path=str(path))

    def load(self, kind: str, version: int) -> Any:
        path = self._version_path(kind, version)
        if not path.exists():
            fail(
                E_CACHE_CORRUPT,
                "snapshot version file is missing",
                kind=kind,
                version=version,
                path=str(path),
            )
        try:
            wrapper = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            fail(
                E_CACHE_CORRUPT,
                "snapshot file is unreadable",
                kind=kind,
                version=version,
                path=str(path),
                reason=str(exc),
            )
        if not isinstance(wrapper, dict):
            fail(
                E_CACHE_CORRUPT,
                "snapshot wrapper is not an object",
                kind=kind,
                version=version,
                path=str(path),
            )
        actual = sha256_obj(wrapper.get("payload"))
        if (
            wrapper.get("kind") != kind
            or int(wrapper.get("version", -1)) != version
            or wrapper.get("checksum") != actual
        ):
            fail(
                E_CACHE_CORRUPT,
                "snapshot checksum or identity mismatch",
                kind=kind,
                version=version,
                path=str(path),
                expected_checksum=wrapper.get("checksum"),
                actual_checksum=actual,
            )
        return wrapper["payload"]

    def info(self, kind: str, version: int) -> SnapshotInfo:
        payload = self.load(kind, version)
        return SnapshotInfo(
            kind=kind,
            version=version,
            checksum=sha256_obj(payload),
            path=str(self._version_path(kind, version)),
        )


__all__ = ["SnapshotInfo", "SnapshotRegistry"]

