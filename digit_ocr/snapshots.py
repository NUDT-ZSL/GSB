"""Versioned whole-snapshot storage with rollback and corruption checks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .canonical import SCHEMA_VERSION, canonical_bytes, sha256_json
from .errors import (
    E_INVALID_INPUT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    BusinessError,
)


SNAPSHOT_NAMES = ("normalization", "prototypes", "thresholds", "distance_metric", "metrics")


@dataclass(frozen=True)
class SnapshotRecord:
    name: str
    version: int
    checksum: str
    payload: Mapping[str, Any]
    raw_bytes: bytes

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "checksum": self.checksum,
        }

    def to_envelope(self) -> Dict[str, Any]:
        return {
            "format": SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "checksum": self.checksum,
            "payload": dict(self.payload),
        }


class SnapshotStore:
    """Stores every version as a complete JSON envelope.

    A newer version always fully replaces the prior snapshot. Same-version
    payload changes and bad checksums are corruption; lower versions are
    rejected as rollbacks.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _validate_name(self, name: str) -> None:
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in name):
            raise BusinessError(
                E_INVALID_INPUT,
                "invalid snapshot name",
                snapshot_name=name,
                category="snapshot_name",
            )

    def _checksum(self, name: str, version: int, payload: Mapping[str, Any]) -> str:
        # The checksum covers snapshot content. Identity fields are validated
        # separately against the path and envelope.
        return sha256_json(payload)

    def path_for(self, name: str, version: int) -> Path:
        self._validate_name(name)
        return self.root / name / "v{0:06d}.json".format(version)

    def _parse_path_version(self, path: Path) -> Optional[int]:
        stem = path.stem
        if not stem.startswith("v"):
            return None
        try:
            return int(stem[1:])
        except ValueError:
            return None

    def load(self, name: str, version: int) -> SnapshotRecord:
        path = self.path_for(name, version)
        if not path.exists():
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version does not exist",
                snapshot_name=name,
                category="snapshot_missing",
                details={"version": version},
            )
        try:
            raw = path.read_bytes()
            envelope = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot cannot be decoded: {0}".format(exc),
                snapshot_name=name,
                category="snapshot_decode",
                details={"version": version},
            )
        if not isinstance(envelope, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot envelope is not an object",
                snapshot_name=name,
                category="snapshot_shape",
                details={"version": version},
            )
        if envelope.get("format") != SCHEMA_VERSION:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot schema is unsupported",
                snapshot_name=name,
                category="snapshot_schema",
                details={"version": version},
            )
        if envelope.get("name") != name or envelope.get("version") != version:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot identity does not match its path",
                snapshot_name=name,
                category="snapshot_identity",
                details={"version": version},
            )
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot payload is not an object",
                snapshot_name=name,
                category="snapshot_payload",
                details={"version": version},
            )
        expected = self._checksum(name, version, payload)
        if envelope.get("checksum") != expected:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum verification failed",
                snapshot_name=name,
                category="snapshot_checksum",
                details={"version": version},
            )
        return SnapshotRecord(
            name=name,
            version=version,
            checksum=expected,
            payload=payload,
            raw_bytes=canonical_bytes(envelope),
        )

    def versions(self, name: str) -> List[int]:
        self._validate_name(name)
        directory = self.root / name
        if not directory.exists():
            return []
        result: List[int] = []
        for path in directory.glob("v*.json"):
            version = self._parse_path_version(path)
            if version is not None:
                result.append(version)
        return sorted(result)

    def current(self, name: str) -> Optional[SnapshotRecord]:
        versions = self.versions(name)
        if not versions:
            return None
        return self.load(name, versions[-1])

    def validate_replacement(
        self,
        name: str,
        version: int,
        payload: Mapping[str, Any],
        *,
        expected_checksum: Optional[str] = None,
    ) -> str:
        self._validate_name(name)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BusinessError(
                E_INVALID_INPUT,
                "snapshot version must be an integer >= 1",
                snapshot_name=name,
                category="snapshot_version",
            )
        checksum = self._checksum(name, version, payload)
        current = self.current(name)
        if current is not None:
            if version < current.version:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "a lower snapshot version cannot replace a newer snapshot",
                    snapshot_name=name,
                    category="snapshot_rollback",
                    details={
                        "attempted_version": version,
                        "current_version": current.version,
                    },
                )
            if version == current.version and checksum != current.checksum:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "same version has a different checksum",
                    snapshot_name=name,
                    category="snapshot_checksum",
                    details={
                        "version": version,
                        "current_checksum": current.checksum,
                        "attempted_checksum": checksum,
                    },
                )
        if expected_checksum is not None and expected_checksum != checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum does not match the declared checksum",
                snapshot_name=name,
                category="snapshot_checksum",
                details={"version": version},
            )
        return checksum

    def replace(
        self,
        name: str,
        version: int,
        payload: Mapping[str, Any],
        *,
        expected_checksum: Optional[str] = None,
    ) -> SnapshotRecord:
        checksum = self.validate_replacement(
            name,
            version,
            payload,
            expected_checksum=expected_checksum,
        )
        current = self.current(name)
        if current is not None and current.version == version:
            if current.checksum != checksum:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "same snapshot version has conflicting content",
                    snapshot_name=name,
                    category="snapshot_checksum",
                )
            return current
        envelope = {
            "format": SCHEMA_VERSION,
            "name": name,
            "version": version,
            "checksum": checksum,
            "payload": dict(payload),
        }
        encoded = canonical_bytes(envelope)
        path = self.path_for(name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        return SnapshotRecord(
            name=name,
            version=version,
            checksum=checksum,
            payload=dict(payload),
            raw_bytes=encoded,
        )

    def append(self, name: str, payload: Mapping[str, Any]) -> SnapshotRecord:
        current = self.current(name)
        version = 1 if current is None else current.version + 1
        return self.replace(name, version, payload)

    def all_current_metadata(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for name in SNAPSHOT_NAMES:
            current = self.current(name)
            if current is not None:
                result[name] = current.metadata()
        return result
