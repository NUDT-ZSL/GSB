"""Versioned whole-snapshot storage for model, policy and history state."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes, checksum, sha256_bytes
from .codes import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK
from .errors import BusinessError

SNAPSHOT_CATEGORIES = ("model_parameters", "temporal_policy", "history_state")
_PATTERN = re.compile(r"(?P<category>[a-z_]+)\.v(?P<version>\d{9})\.json$")


class SnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _files_for(self, category: str) -> list[tuple[int, Path]]:
        values: list[tuple[int, Path]] = []
        for path in self.root.glob(f"{category}.v*.json"):
            match = _PATTERN.match(path.name)
            if match and match.group("category") == category:
                values.append((int(match.group("version")), path))
        values.sort(key=lambda item: item[0])
        return values

    def _read(self, category: str, version: int, path: Path) -> dict[str, Any]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("category") != category:
                raise ValueError("category mismatch")
            if int(envelope.get("version", -1)) != version:
                raise ValueError("filename/envelope version mismatch")
            expected_checksum = sha256_bytes(canonical_bytes(envelope.get("payload")))
            if envelope.get("content_checksum") != expected_checksum:
                raise ValueError("content checksum mismatch")
            if envelope.get("envelope_checksum") != checksum(
                {
                    "category": category,
                    "version": version,
                    "content_checksum": envelope.get("content_checksum"),
                    "payload": envelope.get("payload"),
                }
            ):
                raise ValueError("envelope checksum mismatch")
            return envelope
        except Exception as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot {category} v{version} is corrupt: {exc}",
                details={"snapshot_category": category, "snapshot_version": version, "path": str(path)},
            ) from exc

    def get(self, category: str) -> dict[str, Any] | None:
        files = self._files_for(category)
        if not files:
            return None
        version, path = files[-1]
        return self._read(category, version, path)

    def put(self, category: str, version: int, payload: Any) -> dict[str, Any]:
        """Write a complete snapshot; stale versions can never overwrite newer ones."""

        if category not in SNAPSHOT_CATEGORIES:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"unknown snapshot category {category!r}",
                details={"snapshot_category": category, "snapshot_version": version},
            )
        version = int(version)
        if version < 1:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot version must be >= 1",
                details={"snapshot_category": category, "snapshot_version": version},
            )
        existing_files = self._files_for(category)
        existing: dict[str, Any] | None = None
        if existing_files:
            existing_version, existing_path = existing_files[-1]
            existing = self._read(category, existing_version, existing_path)
            if existing_version > version:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    f"attempt to overwrite v{existing_version} with v{version}",
                    details={
                        "snapshot_category": category,
                        "snapshot_version": version,
                        "current_version": existing_version,
                    },
                )
            if existing_version == version:
                if existing["content_checksum"] == sha256_bytes(canonical_bytes(payload)):
                    return existing
                # A checksum change is a whole-snapshot replacement, never a partial patch.

        content_checksum = sha256_bytes(canonical_bytes(payload))
        envelope = {
            "category": category,
            "version": version,
            "content_checksum": content_checksum,
            "payload": payload,
        }
        envelope["envelope_checksum"] = checksum(
            {
                "category": category,
                "version": version,
                "content_checksum": content_checksum,
                "payload": payload,
            }
        )
        new_path = self.root / f"{category}.v{version:09d}.json"
        atomic_write_bytes(new_path, canonical_bytes(envelope) + b"\n")
        for old_version, old_path in existing_files:
            if old_version != version:
                try:
                    old_path.unlink()
                except FileNotFoundError:
                    pass
        return envelope

    def verify_all(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for category in SNAPSHOT_CATEGORIES:
            envelope = self.get(category)
            if envelope is not None:
                result[category] = envelope
        return result

