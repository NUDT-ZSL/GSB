"""Atomic, monotonically versioned whole-snapshot storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .codes import EngineError, ErrorCode
from .hashing import canonical_hash, read_json, write_atomic_json


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        safe_name = name.replace("/", "_").replace("..", "_")
        return self.root / safe_name

    def latest_path(self, name: str) -> Path:
        return self._dir(name) / "latest.json"

    def get_latest(self, name: str) -> Optional[Dict[str, Any]]:
        path = self.latest_path(name)
        if not path.exists():
            return None
        envelope = read_json(path)
        checksum = canonical_hash(envelope.get("payload"))
        if envelope.get("checksum") != checksum:
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                "snapshot checksum mismatch",
                {"name": name, "path": str(path)},
            )
        return envelope

    def put(self, name: str, payload: Any, version: int) -> Dict[str, Any]:
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise EngineError(
                ErrorCode.E_CONFIG_VERSION,
                "snapshot version must be a positive integer",
                {"name": name, "version": version},
            )
        directory = self._dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        latest = self.get_latest(name)
        checksum = canonical_hash(payload)
        if latest is not None:
            current_version = int(latest["version"])
            current_checksum = latest["checksum"]
            if version < current_version:
                raise EngineError(
                    ErrorCode.E_SNAPSHOT_ROLLBACK,
                    "refusing to replace a newer snapshot with an older version",
                    {
                        "name": name,
                        "attempted_version": version,
                        "current_version": current_version,
                    },
                )
            if version == current_version:
                if checksum == current_checksum:
                    return latest
                raise EngineError(
                    ErrorCode.E_CONFIG_VERSION,
                    "same snapshot version has a different checksum; increment version before replacement",
                    {"name": name, "version": version, "current_checksum": current_checksum},
                )

        envelope = {
            "name": name,
            "version": int(version),
            "checksum": checksum,
            "payload": payload,
        }
        write_atomic_json(directory / f"v{version:08d}.json", envelope)
        write_atomic_json(self.latest_path(name), envelope)
        return envelope

    def rollback_to(self, name: str, version: int) -> None:
        raise EngineError(
            ErrorCode.E_SNAPSHOT_ROLLBACK,
            "explicit snapshot rollback is forbidden; restore a forward version instead",
            {"name": name, "requested_version": version},
        )
