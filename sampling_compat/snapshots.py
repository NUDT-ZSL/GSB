"""Whole-snapshot replacement with per-entry version and checksum validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .utils import atomic_write_text, checksum_obj, pretty_json, sha256_bytes

SNAPSHOT_FORMAT = "sampling-compat-snapshots"
SNAPSHOT_FORMAT_VERSION = 1
KNOWN_SNAPSHOT_KINDS = frozenset(
    {
        "vocab_table",
        "normalized_params",
        "sampling_decisions",
        "migration_records",
    }
)


def make_envelope(kind: str, version: int, content: Any) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "version": version,
        "content": content,
    }
    payload["checksum"] = checksum_obj(payload)
    return payload


def verify_envelope(kind: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot entry '{kind}' is not an object",
            entry=kind,
            version="unknown",
        )
    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot entry '{kind}' has an invalid version",
            entry=kind,
            version=version if version is not None else "missing",
        )
    if raw.get("kind") != kind:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot entry '{kind}' declares a different kind",
            entry=kind,
            version=version,
            details={"declared_kind": raw.get("kind")},
        )
    expected = raw.get("checksum")
    if not isinstance(expected, str):
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot entry '{kind}' is missing a checksum",
            entry=kind,
            version=version,
        )
    payload = {
        "kind": raw.get("kind"),
        "version": version,
        "content": raw.get("content"),
    }
    actual = checksum_obj(payload)
    if actual != expected:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot entry '{kind}' checksum does not match its content",
            entry=kind,
            version=version,
            details={"expected": expected, "actual": actual},
        )
    return {
        "kind": kind,
        "version": version,
        "checksum": expected,
        "content": raw.get("content"),
    }


class SnapshotRepository:
    """Repository replacing the complete on-disk snapshot on every commit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._signature: str | None = None
        self.reload_count = 0
        self.last_reload: dict[str, Any] = {"changed": False, "versions": {}}

    def load_all(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            snapshots: dict[str, dict[str, Any]] = {}
            signature = "missing"
        else:
            try:
                file_bytes = self.path.read_bytes()
                raw = json.loads(file_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BusinessError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    f"snapshot file cannot be loaded: {exc}",
                    entry="file",
                    version="unknown",
                ) from exc
            signature = sha256_bytes(file_bytes)
            if not isinstance(raw, dict):
                raise BusinessError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "snapshot file root must be an object",
                    entry="file",
                    version="unknown",
                )
            if raw.get("format") != SNAPSHOT_FORMAT or raw.get("format_version") != 1:
                raise BusinessError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "snapshot file has an unsupported format header",
                    entry="file",
                    version=raw.get("format_version", "missing"),
                )
            raw_snapshots = raw.get("snapshots")
            if not isinstance(raw_snapshots, dict):
                raise BusinessError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "snapshot file is missing its snapshots object",
                    entry="file",
                    version="unknown",
                )
            snapshots = {}
            for kind, envelope in raw_snapshots.items():
                if not isinstance(kind, str):
                    raise BusinessError(
                        ErrorCode.SNAPSHOT_CORRUPT,
                        "snapshot entry kind must be a string",
                        entry="file",
                        version="unknown",
                    )
                snapshots[kind] = verify_envelope(kind, envelope)

        changed = signature != self._signature
        if changed:
            previous_versions = {
                kind: envelope["version"] for kind, envelope in self._snapshots.items()
            }
            self._snapshots = snapshots
            self._signature = signature
            self.reload_count += 1
            self.last_reload = {
                "changed": True,
                "previous_versions": previous_versions,
                "versions": {
                    kind: envelope["version"] for kind, envelope in snapshots.items()
                },
                "reload_count": self.reload_count,
            }
        return {
            kind: {
                **envelope,
                "content": envelope["content"],
            }
            for kind, envelope in self._snapshots.items()
        }

    def get(self, kind: str) -> dict[str, Any] | None:
        snapshots = self.load_all()
        return snapshots.get(kind)

    def commit_many(self, updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if not updates:
            raise ValueError("commit_many requires at least one snapshot update")
        unknown = sorted(set(updates) - KNOWN_SNAPSHOT_KINDS)
        if unknown:
            raise ValueError(f"unknown snapshot kinds: {unknown}")

        current = self.load_all()
        new_snapshots = {
            kind: {
                "kind": envelope["kind"],
                "version": envelope["version"],
                "checksum": envelope["checksum"],
                "content": envelope["content"],
            }
            for kind, envelope in current.items()
        }
        committed: dict[str, dict[str, Any]] = {}
        for kind, content in updates.items():
            previous = current.get(kind)
            next_version = previous["version"] + 1 if previous else 1
            envelope = make_envelope(kind, next_version, content)
            new_snapshots[kind] = envelope
            committed[kind] = envelope

        document = {
            "format": SNAPSHOT_FORMAT,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "snapshots": new_snapshots,
        }
        atomic_write_text(self.path, pretty_json(document))
        self.load_all()
        return committed

    def versions(self) -> dict[str, int]:
        return {
            kind: envelope["version"]
            for kind, envelope in self.load_all().items()
        }

    def checksums(self) -> dict[str, str]:
        return {
            kind: envelope["checksum"]
            for kind, envelope in self.load_all().items()
        }


def probe_snapshot_file(path: Path) -> dict[str, dict[str, Any]]:
    repository = SnapshotRepository(path)
    return repository.load_all()
