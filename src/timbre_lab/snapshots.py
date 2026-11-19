"""Whole-snapshot versioning with monotonic versions and content checksums."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .deterministic import (
    atomic_write_bytes,
    canonical_json_text,
    sha256_json,
    to_jsonable,
)
from .errors import (
    DomainError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
)


SNAPSHOT_CATEGORIES = (
    "embedding_stats",
    "precision_config",
    "error_spectrum",
)


@dataclass(frozen=True)
class SnapshotRecord:
    category: str
    version: int
    checksum: str
    payload: dict[str, Any]

    def to_wrapper(self) -> dict[str, Any]:
        return {
            "snapshot_format": 1,
            "category": self.category,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }

    def status(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "version": self.version,
            "checksum": self.checksum,
            "payload_checksum": sha256_json(self.payload),
        }


def _checksum(category: str, version: int, payload: dict[str, Any]) -> str:
    return sha256_json(
        {
            "category": category,
            "version": version,
            "payload": payload,
        }
    )


class SnapshotManager:
    """A category file is always replaced as a complete validated document."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._current: dict[str, SnapshotRecord] = {}

    def _path(self, category: str) -> Path:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        return self.root / f"{category}.snapshot.json"

    def _raise_corrupt(
        self,
        category: str,
        version: int | None,
        reason: str,
        **details: Any,
    ) -> None:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            f"snapshot {category!r} is corrupt: {reason}",
            stage="snapshot_load",
            field_name="checksum",
            details={
                "snapshot_category": category,
                "snapshot_version": version,
                "reason": reason,
                **details,
            },
        )

    def _read_record(self, category: str) -> SnapshotRecord | None:
        path = self._path(category)
        if not path.exists():
            return None
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._raise_corrupt(category, None, "unreadable_json", error=str(exc))
        if not isinstance(wrapper, dict):
            self._raise_corrupt(category, None, "wrapper_not_object")
        version_value = wrapper.get("version")
        version = version_value if isinstance(version_value, int) else None
        if wrapper.get("snapshot_format") != 1:
            self._raise_corrupt(category, version, "unknown_snapshot_format")
        if wrapper.get("category") != category:
            self._raise_corrupt(category, version, "category_mismatch")
        if version is None or version <= 0:
            self._raise_corrupt(category, version, "invalid_version")
        payload = wrapper.get("payload")
        if not isinstance(payload, dict):
            self._raise_corrupt(category, version, "payload_not_object")
        expected = _checksum(category, version, payload)
        actual = wrapper.get("checksum")
        if actual != expected:
            self._raise_corrupt(
                category,
                version,
                "checksum_mismatch",
                expected_checksum=expected,
                actual_checksum=actual,
            )
        return SnapshotRecord(
            category=category,
            version=version,
            checksum=expected,
            payload=payload,
        )

    def load(self, category: str) -> SnapshotRecord | None:
        record = self._read_record(category)
        if record is None:
            return None
        current = self._current.get(category)
        if current is not None:
            if record.version < current.version:
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "on-disk snapshot version is older than the in-memory snapshot",
                    stage="snapshot_load",
                    details={
                        "snapshot_category": category,
                        "current_version": current.version,
                        "loaded_version": record.version,
                    },
                )
            if record.version == current.version and record.checksum != current.checksum:
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "same snapshot version has different content; refusing replacement",
                    stage="snapshot_load",
                    details={
                        "snapshot_category": category,
                        "version": record.version,
                        "current_checksum": current.checksum,
                        "loaded_checksum": record.checksum,
                    },
                )
        self._current[category] = record
        return record

    def reload(self) -> dict[str, Any]:
        loaded: dict[str, SnapshotRecord] = {}
        for category in SNAPSHOT_CATEGORIES:
            record = self._read_record(category)
            if record is not None:
                loaded[category] = record
        for category in sorted(set(self._current) & set(loaded)):
            previous = self._current[category]
            current = loaded[category]
            if current.version < previous.version:
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "reload detected an older snapshot version",
                    stage="snapshot_reload",
                    details={
                        "snapshot_category": category,
                        "current_version": previous.version,
                        "loaded_version": current.version,
                    },
                )
            if current.version == previous.version and current.checksum != previous.checksum:
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "reload detected changed content at the same version",
                    stage="snapshot_reload",
                    details={
                        "snapshot_category": category,
                        "version": current.version,
                        "current_checksum": previous.checksum,
                        "loaded_checksum": current.checksum,
                    },
                )
        changes: dict[str, Any] = {}
        all_categories = sorted(set(self._current) | set(loaded))
        for category in all_categories:
            previous = self._current.get(category)
            current = loaded.get(category)
            if previous is None and current is not None:
                changes[category] = {
                    "change": "added",
                    "version": current.version,
                    "checksum": current.checksum,
                }
            elif previous is not None and current is None:
                changes[category] = {
                    "change": "removed",
                    "version": previous.version,
                    "checksum": previous.checksum,
                }
            elif previous is not None and current is not None:
                if previous.version != current.version:
                    changes[category] = {
                        "change": "version_changed",
                        "from_version": previous.version,
                        "to_version": current.version,
                        "checksum": current.checksum,
                    }
                elif previous.checksum != current.checksum:
                    changes[category] = {
                        "change": "checksum_changed",
                        "version": current.version,
                        "from_checksum": previous.checksum,
                        "to_checksum": current.checksum,
                    }
        self._current = loaded
        return {
            "replaced_wholesale": True,
            "changes": changes,
            "snapshots": {
                category: self._current[category].status()
                for category in sorted(self._current)
            },
        }

    def commit(
        self,
        category: str,
        version: int,
        payload: dict[str, Any],
    ) -> SnapshotRecord:
        normalized, checksum, existing = self._preflight_commit(
            category,
            version,
            payload,
        )
        if existing is not None and version == existing.version:
            self._current[category] = existing
            return existing
        record = SnapshotRecord(
            category=category,
            version=version,
            checksum=checksum,
            payload=normalized,
        )
        text = canonical_json_text(record.to_wrapper(), indent=2) + "\n"
        atomic_write_bytes(self._path(category), text.encode("utf-8"))
        self._current[category] = record
        return record

    def _preflight_commit(
        self,
        category: str,
        version: int,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str, SnapshotRecord | None]:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("snapshot version must be a positive integer")
        if not isinstance(payload, dict):
            raise ValueError("snapshot payload must be a JSON object")
        normalized = to_jsonable(payload)
        checksum = _checksum(category, version, normalized)
        existing = self._read_record(category)
        if existing is not None:
            if version < existing.version:
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot commit would roll back to an older version",
                    stage="snapshot_commit",
                    details={
                        "snapshot_category": category,
                        "current_version": existing.version,
                        "attempted_version": version,
                    },
                )
            if version == existing.version:
                if checksum == existing.checksum:
                    return normalized, checksum, existing
                raise DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot commit attempted to replace an existing version with different content",
                    stage="snapshot_commit",
                    details={
                        "snapshot_category": category,
                        "current_version": existing.version,
                        "attempted_version": version,
                        "current_checksum": existing.checksum,
                        "attempted_checksum": checksum,
                    },
                )
        return normalized, checksum, existing

    def commit_all(
        self,
        payloads: dict[str, dict[str, Any]],
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        unknown = sorted(set(payloads) - set(SNAPSHOT_CATEGORIES))
        if unknown:
            raise ValueError(f"unknown snapshot categories: {unknown}")
        if not payloads:
            raise ValueError("at least one snapshot payload is required")
        versions: list[int] = []
        for category in sorted(payloads):
            existing = self._read_record(category)
            if existing is not None:
                versions.append(existing.version)
        if version is None:
            version = max(versions, default=0) + 1
        normalized_payloads: dict[str, dict[str, Any]] = {}
        for category in sorted(payloads):
            normalized, _, _ = self._preflight_commit(
                category,
                version,
                payloads[category],
            )
            normalized_payloads[category] = normalized
        committed = {
            category: self.commit(category, version, normalized_payloads[category])
            for category in sorted(normalized_payloads)
        }
        return {
            "version": version,
            "snapshots": {
                category: record.status()
                for category, record in sorted(committed.items())
            },
        }

    def next_version(self) -> int:
        versions: list[int] = []
        for category in SNAPSHOT_CATEGORIES:
            record = self._read_record(category)
            if record is not None:
                versions.append(record.version)
        return max(versions, default=0) + 1

    def current(self) -> dict[str, Any]:
        return {
            category: self._current[category].status()
            for category in sorted(self._current)
        }
