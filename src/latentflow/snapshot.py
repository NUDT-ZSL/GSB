from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, checksum_payload
from .errors import BusinessError, ErrorCode

SNAPSHOT_CATEGORIES = ("decode_params", "chunk_plan", "ready_blocks")
_VERSION_FILE = re.compile(r"^(?P<category>[a-z_]+)-(?P<version>\d{8})\.json$")


@dataclass(frozen=True)
class Snapshot:
    category: str
    version: int
    checksum: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }


class SnapshotStore:
    """Versioned whole-document snapshots with atomic active-pointer replacement."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Snapshot] = {}

    def _latest_version_on_disk(self, category: str) -> int:
        versions = []
        for path in self.directory.glob(f"{category}-*.json"):
            match = _VERSION_FILE.match(path.name)
            if match and match.group("category") == category:
                versions.append(int(match.group("version")))
        return max(versions, default=0)

    def _active_path(self, category: str) -> Path:
        return self.directory / f"active-{category}.json"

    def _version_hint_path(self, category: str) -> Path:
        return self.directory / f"active-{category}.version"

    def commit(self, category: str, payload: dict[str, Any]) -> Snapshot:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        with self._lock:
            version = max(
                self._latest_version_on_disk(category),
                self._cache.get(category).version if category in self._cache else 0,
            ) + 1
            checksum = checksum_payload(payload)
            snapshot = Snapshot(
                category=category,
                version=version,
                checksum=checksum,
                payload=payload,
            )
            encoded = canonical_json(snapshot.to_dict()).encode("utf-8")
            version_path = self.directory / f"{category}-{version:08d}.json"
            self._atomic_write(version_path, encoded)
            self._atomic_write(self._active_path(category), encoded)
            self._atomic_write(
                self._version_hint_path(category), str(version).encode("ascii")
            )
            self._cache[category] = snapshot
            return snapshot

    def load(self, category: str) -> Snapshot:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        path = self._active_path(category)
        hint = self._read_version_hint(category)
        if not path.exists():
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "活动快照文件不存在",
                {"category": category, "version": hint},
            )
        try:
            raw = path.read_text("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("snapshot root is not an object")
            version = int(parsed.get("version", hint))
            checksum = str(parsed.get("checksum", ""))
            payload = parsed.get("payload")
            if parsed.get("category") != category or not isinstance(payload, dict):
                raise ValueError("snapshot category or payload is invalid")
            actual = checksum_payload(payload)
            if checksum != actual:
                raise ValueError("checksum does not match payload")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            version = self._read_version_hint(category)
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "快照损坏或内容校验和不匹配",
                {
                    "category": category,
                    "version": version,
                    "path": str(path),
                    "cause": str(exc),
                },
            ) from exc
        snapshot = Snapshot(category, version, checksum, payload)
        with self._lock:
            self._cache[category] = snapshot
        return snapshot

    def load_all(self) -> dict[str, Snapshot]:
        loaded: dict[str, Snapshot] = {}
        for category in SNAPSHOT_CATEGORIES:
            if self._active_path(category).exists():
                loaded[category] = self.load(category)
        return loaded

    def observe_replacement(self, category: str) -> dict[str, Any]:
        cached = self._cache.get(category)
        loaded = self.load(category)
        previous_version = cached.version if cached else None
        replaced = previous_version is None or loaded.version > previous_version
        same_payload = (
            cached is not None and cached.checksum == loaded.checksum
        )
        return {
            "category": category,
            "previous_version": previous_version,
            "loaded_version": loaded.version,
            "replaced": replaced,
            "payload_unchanged": same_payload,
            "checksum": loaded.checksum,
        }

    def versions(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for category in SNAPSHOT_CATEGORIES:
            try:
                snapshot = self.load(category)
            except BusinessError as exc:
                result[category] = {
                    "error": exc.code.value,
                    "version": exc.details.get("version"),
                }
            else:
                result[category] = {
                    "version": snapshot.version,
                    "checksum": snapshot.checksum,
                }
        return result

    def _read_version_hint(self, category: str) -> int | None:
        try:
            return int(self._version_hint_path(category).read_text("ascii").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
