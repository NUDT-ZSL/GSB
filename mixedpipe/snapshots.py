"""Versioned, checksummed snapshot storage with whole-file replacement."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import atomic_write_bytes, canonical_json_bytes, content_checksum
from .canonical import envelope_checksum
from .errors import (
    BusinessError,
    E_STALE_VERSION,
    SnapshotCorruptError,
)


SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_CATEGORIES = ("plan", "ledger", "results")
_VERSION_PATTERN = re.compile(rb'"version"\s*:\s*(\d+)')


@dataclass(frozen=True)
class SnapshotEnvelope:
    category: str
    version: int
    checksum: str
    payload: dict[str, Any]
    path: Path

    @property
    def payload_checksum(self) -> str:
        return content_checksum(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "category": self.category,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }


class SnapshotStore:
    """One file per category; each commit atomically replaces the whole file."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, SnapshotEnvelope] = {}

    def path_for(self, category: str) -> Path:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        return self.root / f"{category}.json"

    @staticmethod
    def _extract_version(raw: bytes) -> int | None:
        match = _VERSION_PATTERN.search(raw)
        return int(match.group(1)) if match else None

    def _read(self, category: str) -> SnapshotEnvelope | None:
        path = self.path_for(category)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SnapshotCorruptError(
                category,
                None,
                "snapshot cannot be read",
                {"path": str(path), "error": str(exc)},
            ) from exc

        version_hint = None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            version_hint = self._extract_version(raw)
            raise SnapshotCorruptError(
                category,
                version_hint,
                "snapshot JSON is invalid; partial data was not used",
                {"path": str(path), "error": str(exc)},
            ) from exc

        if not isinstance(decoded, dict):
            raise SnapshotCorruptError(
                category,
                version_hint,
                "snapshot envelope must be an object",
                {"path": str(path)},
            )
        version = decoded.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SnapshotCorruptError(
                category,
                version if isinstance(version, int) else None,
                "snapshot version is invalid",
                {"path": str(path)},
            )
        if decoded.get("category") != category:
            raise SnapshotCorruptError(
                category,
                version,
                "snapshot category does not match its file",
                {"path": str(path), "stored_category": decoded.get("category")},
            )
        if decoded.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotCorruptError(
                category,
                version,
                "snapshot schema version is unsupported",
                {
                    "path": str(path),
                    "stored_schema_version": decoded.get("schema_version"),
                },
            )
        payload = decoded.get("payload")
        if not isinstance(payload, dict):
            raise SnapshotCorruptError(
                category,
                version,
                "snapshot payload must be an object",
                {"path": str(path)},
            )
        checksum = decoded.get("checksum")
        expected = envelope_checksum(
            category,
            version,
            payload,
            SNAPSHOT_SCHEMA_VERSION,
        )
        if not isinstance(checksum, str) or checksum != expected:
            raise SnapshotCorruptError(
                category,
                version,
                "snapshot checksum mismatch",
                {
                    "path": str(path),
                    "stored_checksum": checksum,
                    "expected_checksum": expected,
                },
            )
        return SnapshotEnvelope(
            category=category,
            version=version,
            checksum=checksum,
            payload=payload,
            path=path,
        )

    def load(self, category: str) -> SnapshotEnvelope | None:
        with self._lock:
            envelope = self._read(category)
            if envelope is not None:
                # A changed version or checksum replaces the cached object
                # wholesale; no payload is patched in place.
                self._cache[category] = envelope
            else:
                self._cache.pop(category, None)
            return envelope

    def load_all(
        self,
        categories: Iterable[str] = SNAPSHOT_CATEGORIES,
    ) -> dict[str, SnapshotEnvelope | None]:
        return {category: self.load(category) for category in categories}

    def commit(
        self,
        category: str,
        payload: dict[str, Any],
        version: int,
    ) -> SnapshotEnvelope:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BusinessError(
                E_STALE_VERSION,
                "snapshot version must be a positive integer",
                stage="snapshot_commit",
                details={"snapshot_category": category, "version": version},
            )
        with self._lock:
            current = self.load(category)
            if current is not None and version <= current.version:
                raise BusinessError(
                    E_STALE_VERSION,
                    "a completed snapshot version cannot be overwritten",
                    stage="snapshot_commit",
                    details={
                        "snapshot_category": category,
                        "current_version": current.version,
                        "attempted_version": version,
                    },
                )
            checksum = envelope_checksum(
                category,
                version,
                payload,
                SNAPSHOT_SCHEMA_VERSION,
            )
            envelope = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "category": category,
                "version": version,
                "checksum": checksum,
                "payload": payload,
            }
            raw = canonical_json_bytes(envelope) + b"\n"
            path = self.path_for(category)
            atomic_write_bytes(path, raw)
            committed = SnapshotEnvelope(
                category=category,
                version=version,
                checksum=checksum,
                payload=payload,
                path=path,
            )
            self._cache[category] = committed
            # Read the complete replacement back before reporting success.
            verified = self._read(category)
            if verified is None or verified.checksum != checksum:
                raise SnapshotCorruptError(
                    category,
                    version,
                    "snapshot verification failed after atomic replacement",
                    {"path": str(path)},
                )
            return verified

    def current_version(self) -> int:
        versions = [
            envelope.version
            for envelope in self.load_all().values()
            if envelope is not None
        ]
        return max(versions, default=0)
