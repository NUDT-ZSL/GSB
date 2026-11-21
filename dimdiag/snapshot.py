"""Whole-snapshot versioning, checksums, and atomic replacement."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    BusinessError,
)
from .serialization import (
    atomic_write_bytes,
    pretty_json_bytes,
    sha256_json,
)


SCHEMA_VERSION = 1
REQUIRED_SECTIONS = (
    "config",
    "standardization",
    "model",
    "coordinates",
    "evaluation",
    "error_spectrum",
)


def make_section(content: Any, version: int) -> dict[str, Any]:
    return {
        "version": int(version),
        "checksum": sha256_json(content),
        "content": content,
    }


def finalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(snapshot)
    payload.pop("overall_checksum", None)
    payload["overall_checksum"] = sha256_json(payload)
    return payload


def verify_snapshot(snapshot: Mapping[str, Any]) -> None:
    version = snapshot.get("snapshot_version")
    if not isinstance(version, int) or version < 1:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot version is invalid",
            {"field": "snapshot_version", "value": version},
        )
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot schema version is unsupported",
            {"field": "schema_version", "value": snapshot.get("schema_version")},
        )
    sections = snapshot.get("sections")
    if not isinstance(sections, Mapping):
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot sections are missing",
            {"field": "sections"},
        )
    for section_name in REQUIRED_SECTIONS:
        section = sections.get(section_name)
        if not isinstance(section, Mapping):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot section is missing",
                {"field": f"sections.{section_name}"},
            )
        if section.get("version") != version:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot section version differs from the snapshot version",
                {
                    "field": f"sections.{section_name}.version",
                    "section_version": section.get("version"),
                    "snapshot_version": version,
                },
            )
        expected = sha256_json(section.get("content"))
        if section.get("checksum") != expected:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot section checksum mismatch",
                {
                    "field": f"sections.{section_name}.checksum",
                    "expected": expected,
                    "actual": section.get("checksum"),
                },
            )
    stored_checksum = snapshot.get("overall_checksum")
    if not isinstance(stored_checksum, str):
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot overall checksum is missing",
            {"field": "overall_checksum"},
        )
    payload = dict(snapshot)
    payload.pop("overall_checksum", None)
    expected_overall = sha256_json(payload)
    if stored_checksum != expected_overall:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot overall checksum mismatch",
            {"expected": expected_overall, "actual": stored_checksum},
        )


class SnapshotStore:
    """A single active snapshot replaced atomically on each commit."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshot_dir = self.root / "snapshots"
        self.invalidated_dir = self.snapshot_dir / "invalidated"
        self.active_path = self.snapshot_dir / "active.json"
        self.meta_path = self.snapshot_dir / "meta.json"
        self.cache_dir = self.root / "cache"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.invalidated_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_json(self, path: Path, field: str) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot file cannot be read",
                {"field": field, "path": str(path), "reason": str(exc)},
            ) from exc

    def load_meta(self) -> dict[str, Any] | None:
        meta = self._read_json(self.meta_path, "meta")
        if meta is None:
            return None
        if not isinstance(meta, Mapping):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot metadata is malformed",
                {"field": "meta"},
            )
        return dict(meta)

    def _write_meta(
        self,
        *,
        latest_version: int,
        status: str,
        active_version: int | None = None,
        active_checksum: str | None = None,
        invalidated_version: int | None = None,
        invalidated_checksum: str | None = None,
        reason: str | None = None,
        requested_precision: Mapping[str, Any] | None = None,
    ) -> None:
        meta = {
            "schema_version": SCHEMA_VERSION,
            "latest_version": int(latest_version),
            "status": status,
            "active_version": active_version,
            "active_checksum": active_checksum,
            "invalidated_version": invalidated_version,
            "invalidated_checksum": invalidated_checksum,
            "reason": reason,
            "requested_precision": dict(requested_precision or {}),
        }
        atomic_write_bytes(self.meta_path, pretty_json_bytes(meta))

    def load_active(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.active_path.exists():
                return None
            snapshot = self._read_json(self.active_path, "active_snapshot")
            if not isinstance(snapshot, dict):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "active snapshot is not an object",
                    {"field": "active_snapshot"},
                )
            verify_snapshot(snapshot)
            meta = self.load_meta()
            if meta is not None:
                latest_version = int(meta.get("latest_version", 0))
                if int(snapshot["snapshot_version"]) < latest_version:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "active snapshot is older than the recorded latest version",
                        {
                            "active_version": int(snapshot["snapshot_version"]),
                            "latest_version": latest_version,
                        },
                    )
                if int(snapshot["snapshot_version"]) > latest_version:
                    self._write_meta(
                        latest_version=int(snapshot["snapshot_version"]),
                        status="active",
                        active_version=int(snapshot["snapshot_version"]),
                        active_checksum=str(snapshot["overall_checksum"]),
                    )
                elif (
                    meta.get("status") == "active"
                    and meta.get("active_checksum")
                    and meta.get("active_checksum") != snapshot["overall_checksum"]
                ):
                    raise BusinessError(
                        E_SNAPSHOT_CORRUPT,
                        "active snapshot checksum differs from persisted metadata",
                        {
                            "version": int(snapshot["snapshot_version"]),
                            "meta_checksum": meta.get("active_checksum"),
                            "snapshot_checksum": snapshot["overall_checksum"],
                        },
                    )
            return snapshot

    def latest_version(self) -> int:
        with self._lock:
            meta = self.load_meta()
            latest = int(meta.get("latest_version", 0)) if meta else 0
            if self.active_path.exists():
                snapshot = self._read_json(self.active_path, "active_snapshot")
                if isinstance(snapshot, Mapping):
                    verify_snapshot(snapshot)
                    latest = max(latest, int(snapshot["snapshot_version"]))
            return latest

    def next_version(self) -> int:
        return self.latest_version() + 1

    def commit(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            incoming = dict(snapshot)
            incoming_version = incoming.get("snapshot_version")
            if (
                not isinstance(incoming_version, int)
                or isinstance(incoming_version, bool)
                or incoming_version < 0
            ):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "incoming snapshot version is invalid",
                    {"value": incoming_version},
                )
            latest = self.latest_version()
            if incoming_version < latest:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "an older snapshot cannot overwrite a newer snapshot",
                    {
                        "incoming_version": incoming_version,
                        "latest_version": latest,
                    },
                )
            if incoming_version < 1:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "incoming snapshot version is invalid",
                    {"value": incoming_version},
                )
            if incoming_version == latest:
                current = self.load_active()
                if (
                    current is not None
                    and current.get("overall_checksum")
                    == incoming.get("overall_checksum")
                ):
                    return current
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "the current version already has different content",
                    {
                        "incoming_version": incoming_version,
                        "latest_version": latest,
                    },
                )
            verify_snapshot(incoming)
            payload = pretty_json_bytes(incoming)
            atomic_write_bytes(self.active_path, payload)
            self._write_meta(
                latest_version=incoming_version,
                status="active",
                active_version=incoming_version,
                active_checksum=str(incoming["overall_checksum"]),
            )
            return dict(incoming)

    def invalidate_active(
        self,
        *,
        reason: str,
        requested_precision: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self.load_active()
            if snapshot is None:
                return None
            version = int(snapshot["snapshot_version"])
            checksum = str(snapshot["overall_checksum"])
            archive_path = self.invalidated_dir / f"snapshot-v{version:06d}.json"
            atomic_write_bytes(archive_path, pretty_json_bytes(snapshot))
            self.active_path.unlink()
            meta = self.load_meta() or {}
            self._write_meta(
                latest_version=max(int(meta.get("latest_version", 0)), version),
                status="invalidated",
                invalidated_version=version,
                invalidated_checksum=checksum,
                reason=reason,
                requested_precision=requested_precision,
            )
            return {
                "version": version,
                "checksum": checksum,
                "archive_path": str(archive_path),
                "reason": reason,
            }

    def reset(self, reason: str) -> dict[str, Any] | None:
        return self.invalidate_active(
            reason=reason,
            requested_precision={},
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            meta = self.load_meta()
            active = self.load_active()
            return {
                "meta": meta,
                "active_version": (
                    int(active["snapshot_version"]) if active is not None else None
                ),
                "active_checksum": (
                    active.get("overall_checksum") if active is not None else None
                ),
                "status": "active" if active is not None else (meta or {}).get("status", "empty"),
            }
