"""Feature-spec v1/v2 readers, lossless migrations, and atomic active commits."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_json,
)
from .errors import BusinessError


V1_VERSION = "v1"
V2_VERSION = "v2"


@dataclass(frozen=True)
class FeatureEntry:
    feature_id: str
    effective_ms: int
    generation: int
    base_params: dict[str, Any]
    shard_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "feature_id": self.feature_id,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "base_params": self.base_params,
        }
        if self.shard_id is not None:
            value["shard_id"] = self.shard_id
        return value


@dataclass(frozen=True)
class Tombstone:
    feature_id: str
    effective_ms: int
    generation: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "reason": self.reason,
        }


@dataclass
class Snapshot:
    format_version: str
    snapshot_version: int
    snapshot_id: str
    logical_clock_ms: int
    entries: list[FeatureEntry]
    tombstones: list[Tombstone] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    declared_checksum: str | None = None

    @property
    def invalid_feature_ids(self) -> set[str]:
        return {item.feature_id for item in self.tombstones}

    @property
    def active_feature_ids(self) -> set[str]:
        return {item.feature_id for item in self.entries}

    def entry_map(self) -> dict[str, FeatureEntry]:
        return {entry.feature_id: entry for entry in self.entries}

    def tombstone_map(self) -> dict[str, Tombstone]:
        return {item.feature_id: item for item in self.tombstones}

    def content_dict(self, *, include_provenance: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "format_version": self.format_version,
            "snapshot_version": self.snapshot_version,
            "snapshot_id": self.snapshot_id,
            "logical_clock_ms": self.logical_clock_ms,
            "entries": [
                {
                    "feature_id": entry.feature_id,
                    "effective_ms": entry.effective_ms,
                    "generation": entry.generation,
                    "base_params": entry.base_params,
                }
                for entry in sorted(self.entries, key=lambda item: (item.effective_ms, item.feature_id))
            ],
            "tombstones": [
                {
                    "feature_id": tombstone.feature_id,
                    "effective_ms": tombstone.effective_ms,
                    "generation": tombstone.generation,
                    "reason": tombstone.reason,
                }
                for tombstone in sorted(
                    self.tombstones, key=lambda item: (item.effective_ms, item.feature_id)
                )
            ],
        }
        if include_provenance:
            value["provenance"] = self.provenance
        return value

    def checksum(self) -> str:
        return sha256_json(self.content_dict())

    def task_spec_digest(self, feature_ids: tuple[str, ...]) -> str:
        entries = self.entry_map()
        tombstones = self.tombstone_map()
        spec: dict[str, Any] = {}
        for feature_id in feature_ids:
            if feature_id in tombstones:
                item = tombstones[feature_id]
                spec[feature_id] = {
                    "state": "invalid",
                    "effective_ms": item.effective_ms,
                    "generation": item.generation,
                    "reason": item.reason,
                }
            elif feature_id in entries:
                item = entries[feature_id]
                spec[feature_id] = {
                    "state": "active",
                    "effective_ms": item.effective_ms,
                    "generation": item.generation,
                    "base_params": item.base_params,
                }
            else:
                spec[feature_id] = {"state": "missing"}
        return sha256_json(
            {
                "snapshot_version": self.snapshot_version,
                "features": spec,
            }
        )

    def to_active_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "snapshot_version": self.snapshot_version,
            "snapshot_id": self.snapshot_id,
            "logical_clock_ms": self.logical_clock_ms,
            "entries": [entry.to_dict() for entry in self.entries],
            "tombstones": [item.to_dict() for item in self.tombstones],
            "provenance": self.provenance,
            "checksum": self.checksum(),
            "source_path": self.source_path,
        }

    @classmethod
    def from_active_dict(cls, raw: dict[str, Any]) -> "Snapshot":
        snapshot = cls(
            format_version=str(raw["format_version"]),
            snapshot_version=int(raw["snapshot_version"]),
            snapshot_id=str(raw["snapshot_id"]),
            logical_clock_ms=int(raw["logical_clock_ms"]),
            entries=[
                FeatureEntry(
                    feature_id=str(item["feature_id"]),
                    effective_ms=int(item["effective_ms"]),
                    generation=int(item["generation"]),
                    base_params=dict(item["base_params"]),
                    shard_id=item.get("shard_id"),
                )
                for item in raw["entries"]
            ],
            tombstones=[
                Tombstone(
                    feature_id=str(item["feature_id"]),
                    effective_ms=int(item["effective_ms"]),
                    generation=int(item["generation"]),
                    reason=str(item["reason"]),
                )
                for item in raw.get("tombstones", [])
            ],
            provenance=dict(raw.get("provenance", {})),
            source_path=raw.get("source_path"),
            declared_checksum=raw.get("checksum"),
        )
        _validate_snapshot(snapshot)
        if snapshot.declared_checksum and snapshot.declared_checksum != snapshot.checksum():
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "active snapshot checksum does not match content",
                context={
                    "declared_checksum": snapshot.declared_checksum,
                    "computed_checksum": snapshot.checksum(),
                },
            )
        return snapshot


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError("E_FORMAT_CORRUPT", f"{label} must be a JSON object")
    return value


def _require_field(raw: dict[str, Any], key: str, label: str) -> Any:
    if key not in raw:
        raise BusinessError(
            "E_FORMAT_CORRUPT", f"missing required field {key}", context={"object": label}
        )
    return raw[key]


def _require_int(value: Any, label: str, *, nonnegative: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError("E_FORMAT_CORRUPT", f"{label} must be an integer")
    if nonnegative and value < 0:
        raise BusinessError("E_FORMAT_CORRUPT", f"{label} must be non-negative")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BusinessError("E_FORMAT_CORRUPT", f"{label} must be a non-empty string")
    return value


def _validate_snapshot(snapshot: Snapshot) -> None:
    if snapshot.format_version not in {V1_VERSION, V2_VERSION}:
        raise BusinessError(
            "E_VERSION_UNSUPPORTED",
            "unsupported feature-spec version",
            context={"format_version": snapshot.format_version},
        )
    if snapshot.snapshot_version < 0 or snapshot.logical_clock_ms < 0:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "snapshot version and logical clock must be non-negative",
        )
    entry_ids = [entry.feature_id for entry in snapshot.entries]
    tombstone_ids = [item.feature_id for item in snapshot.tombstones]
    if len(entry_ids) != len(set(entry_ids)):
        raise BusinessError("E_FORMAT_CORRUPT", "duplicate feature_id in active entries")
    if len(tombstone_ids) != len(set(tombstone_ids)):
        raise BusinessError("E_FORMAT_CORRUPT", "duplicate feature_id in tombstones")
    overlap = set(entry_ids) & set(tombstone_ids)
    if overlap:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "a feature cannot be both active and tombstoned",
            context={"feature_ids": sorted(overlap)},
        )
    previous_ms = 0
    for entry in sorted(snapshot.entries, key=lambda item: (item.effective_ms, item.feature_id)):
        if entry.effective_ms < previous_ms:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "active feature effective timestamp moved backwards",
                context={"feature_id": entry.feature_id, "effective_ms": entry.effective_ms},
            )
        if entry.effective_ms > snapshot.logical_clock_ms:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "feature effective timestamp is later than snapshot logical clock",
                context={
                    "feature_id": entry.feature_id,
                    "effective_ms": entry.effective_ms,
                    "logical_clock_ms": snapshot.logical_clock_ms,
                },
            )
        if entry.generation < 0:
            raise BusinessError(
                "E_FORMAT_CORRUPT", "feature generation must be non-negative", context={"feature_id": entry.feature_id}
            )
        if not isinstance(entry.base_params, dict):
            raise BusinessError(
                "E_FORMAT_CORRUPT", "base_params must be an object", context={"feature_id": entry.feature_id}
            )
        previous_ms = entry.effective_ms
    for tombstone in sorted(snapshot.tombstones, key=lambda item: (item.effective_ms, item.feature_id)):
        if tombstone.effective_ms > snapshot.logical_clock_ms:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "tombstone timestamp is later than snapshot logical clock",
                context={"feature_id": tombstone.feature_id},
            )
        if tombstone.generation < 0:
            raise BusinessError(
                "E_FORMAT_CORRUPT", "tombstone generation must be non-negative", context={"feature_id": tombstone.feature_id}
            )


def _seconds_to_ms(seconds: Any) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise BusinessError("E_FORMAT_CORRUPT", "effective_seconds must be numeric")
    numeric = float(seconds)
    if numeric < 0:
        raise BusinessError("E_FORMAT_CORRUPT", "effective_seconds must be non-negative")
    return int(round(numeric * 1000.0))


def _read_v1(path: Path) -> Snapshot:
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("E_FORMAT_CORRUPT", f"cannot read v1 snapshot {path}: {exc}") from exc
    raw = _require_dict(raw, "v1 snapshot")
    label = str(path)
    snapshot_version = _require_int(_require_field(raw, "snapshot_version", label), "snapshot_version")
    snapshot_id = _require_str(_require_field(raw, "snapshot_id", label), "snapshot_id")
    logical_clock_ms = _require_int(_require_field(raw, "logical_clock_ms", label), "logical_clock_ms")
    epoch_ms = _require_int(_require_field(raw, "epoch_ms", label), "epoch_ms")
    default_generation = _require_int(
        _require_field(raw, "feature_generation", label), "feature_generation"
    )
    entries_raw = _require_field(raw, "entries", label)
    if not isinstance(entries_raw, list):
        raise BusinessError("E_FORMAT_CORRUPT", "v1 entries must be a table/list")
    entries: list[FeatureEntry] = []
    provenance: dict[str, Any] = {"entries": {}}
    for index, item in enumerate(entries_raw):
        item = _require_dict(item, f"v1 entries[{index}]")
        feature_id = _require_str(
            _require_field(item, "feature_id", f"v1 entries[{index}]"), "feature_id"
        )
        relative_seconds = _require_field(item, "effective_seconds", f"v1 entries[{index}]")
        effective_ms = epoch_ms + _seconds_to_ms(relative_seconds)
        base_params = _require_field(item, "base_params", f"v1 entries[{index}]")
        if not isinstance(base_params, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "v1 base_params must be an object")
        if "generation" in item:
            generation = _require_int(item["generation"], "entry generation")
            generation_source = "v1.entries[].generation"
        else:
            generation = default_generation
            generation_source = "v1.header.feature_generation"
        entries.append(FeatureEntry(feature_id, effective_ms, generation, base_params))
        provenance["entries"][feature_id] = {
            "absolute_ms": {
                "source": "v1.epoch_ms + round(v1.entries[].effective_seconds * 1000)",
                "epoch_ms": epoch_ms,
                "effective_seconds": float(relative_seconds),
            },
            "generation": {"source": generation_source},
        }
    previous_entry_ms = 0
    for entry in entries:
        if entry.effective_ms < previous_entry_ms:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "v1 entry timestamp moved backwards",
                context={
                    "feature_id": entry.feature_id,
                    "effective_ms": entry.effective_ms,
                    "previous_effective_ms": previous_entry_ms,
                },
            )
        previous_entry_ms = entry.effective_ms
    tombstones: list[Tombstone] = []
    invalid_raw = raw.get("invalid_features", [])
    if not isinstance(invalid_raw, list):
        raise BusinessError("E_FORMAT_CORRUPT", "v1 invalid_features must be a list")
    for index, item in enumerate(invalid_raw):
        if isinstance(item, str):
            item = {
                "feature_id": item,
                "effective_seconds": (logical_clock_ms - epoch_ms) / 1000.0,
                "generation": default_generation,
            }
        item = _require_dict(item, f"v1 invalid_features[{index}]")
        feature_id = _require_str(item.get("feature_id"), "invalid feature_id")
        effective_ms = epoch_ms + _seconds_to_ms(item.get("effective_seconds", 0))
        generation = _require_int(item.get("generation", default_generation), "invalid generation")
        tombstones.append(
            Tombstone(
                feature_id=feature_id,
                effective_ms=effective_ms,
                generation=generation,
                reason="migrated_from_v1_invalid_features",
            )
        )
        provenance.setdefault("tombstones", {})[feature_id] = {
            "effective_ms": {
                "source": "v1.epoch_ms + round(v1.invalid_features[].effective_seconds * 1000)"
            },
            "generation": {"source": "v1.invalid_features[].generation"},
            "reason": {"source": "v1 invalid_features has no reason; explicit default applied"},
        }
    snapshot = Snapshot(
        format_version=V1_VERSION,
        snapshot_version=snapshot_version,
        snapshot_id=snapshot_id,
        logical_clock_ms=logical_clock_ms,
        entries=entries,
        tombstones=tombstones,
        provenance=provenance,
        source_path=str(path),
        declared_checksum=raw.get("checksum"),
    )
    _validate_snapshot(snapshot)
    if snapshot.declared_checksum and snapshot.declared_checksum != snapshot.checksum():
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "v1 snapshot checksum does not match content",
            context={"declared_checksum": snapshot.declared_checksum, "computed": snapshot.checksum()},
        )
    return snapshot


def _sqlite_rows(database: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not database.exists():
        raise BusinessError("E_FORMAT_CORRUPT", "v2 metadata database is missing")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
            feature_rows = connection.execute(
                "SELECT feature_id, effective_ms, generation, shard_id FROM features"
            ).fetchall()
            tombstone_rows = connection.execute(
                "SELECT feature_id, effective_ms, generation, reason FROM tombstones"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BusinessError("E_FORMAT_CORRUPT", f"cannot read v2 SQLite metadata: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    meta = {str(row["key"]): str(row["value"]) for row in meta_rows}
    features = [dict(row) for row in feature_rows]
    tombstones = [dict(row) for row in tombstone_rows]
    return meta, features, tombstones


def _read_v2(path: Path) -> Snapshot:
    manifest_path = path if path.is_file() else path / "manifest.json"
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "E_FORMAT_CORRUPT", f"cannot read v2 manifest {manifest_path}: {exc}"
        ) from exc
    manifest = _require_dict(manifest, "v2 manifest")
    snapshot_version = _require_int(
        _require_field(manifest, "snapshot_version", "v2 manifest"), "snapshot_version"
    )
    snapshot_id = _require_str(_require_field(manifest, "snapshot_id", "v2 manifest"), "snapshot_id")
    logical_clock_ms = _require_int(
        _require_field(manifest, "logical_clock_ms", "v2 manifest"), "logical_clock_ms"
    )
    database_name = _require_str(
        _require_field(manifest, "metadata_db", "v2 manifest"), "metadata_db"
    )
    shard_names = _require_field(manifest, "shards", "v2 manifest")
    if not isinstance(shard_names, list) or any(not isinstance(item, str) for item in shard_names):
        raise BusinessError("E_FORMAT_CORRUPT", "v2 shards must be a string list")
    base = manifest_path.parent
    database = base / database_name
    meta, feature_rows, tombstone_rows = _sqlite_rows(database)
    if meta.get("format_version") not in (None, V2_VERSION):
        raise BusinessError(
            "E_VERSION_UNSUPPORTED",
            "SQLite metadata declares an unsupported format_version",
            context={"format_version": meta.get("format_version")},
        )
    for key, expected in (
        ("snapshot_version", snapshot_version),
        ("logical_clock_ms", logical_clock_ms),
        ("snapshot_id", snapshot_id),
    ):
        if key in meta and str(meta[key]) != str(expected):
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "v2 manifest and SQLite metadata disagree",
                context={"field": key, "manifest": expected, "metadata": meta[key]},
            )

    shard_entries: dict[str, dict[str, Any]] = {}
    for shard_name in shard_names:
        shard_path = base / shard_name
        try:
            shard = read_json(shard_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError("E_FORMAT_CORRUPT", f"cannot read v2 shard {shard_path}: {exc}") from exc
        shard = _require_dict(shard, f"v2 shard {shard_name}")
        shard_version = shard.get("format_version")
        if shard_version not in (None, V2_VERSION):
            raise BusinessError(
                "E_VERSION_UNSUPPORTED",
                "v2 shard declares an unsupported format_version",
                context={"shard": shard_name, "format_version": shard_version},
            )
        shard_id = shard.get("shard_id", shard_name)
        items = _require_field(shard, "entries", f"v2 shard {shard_name}")
        if not isinstance(items, list):
            raise BusinessError("E_FORMAT_CORRUPT", "v2 shard entries must be a list")
        for index, item in enumerate(items):
            item = _require_dict(item, f"v2 shard {shard_name} entries[{index}]")
            feature_id = _require_str(item.get("feature_id"), "shard feature_id")
            if feature_id in shard_entries:
                raise BusinessError(
                    "E_FORMAT_CORRUPT", "duplicate feature_id across v2 shards", context={"feature_id": feature_id}
                )
            shard_entries[feature_id] = {
                "base_params": _require_field(item, "base_params", f"v2 shard {shard_name}"),
                "shard_id": str(shard_id),
            }
    entries: list[FeatureEntry] = []
    previous_feature_ms = 0
    for row in feature_rows:
        feature_id = str(row["feature_id"])
        if feature_id not in shard_entries:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "v2 SQLite feature has no matching shard JSON record",
                context={"feature_id": feature_id},
            )
        effective_ms = _require_int(row["effective_ms"], "effective_ms")
        if effective_ms < previous_feature_ms:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "v2 SQLite feature timestamp moved backwards",
                context={
                    "feature_id": feature_id,
                    "effective_ms": effective_ms,
                    "previous_effective_ms": previous_feature_ms,
                },
            )
        previous_feature_ms = effective_ms
        entries.append(
            FeatureEntry(
                feature_id=feature_id,
                effective_ms=effective_ms,
                generation=_require_int(row["generation"], "generation"),
                base_params=dict(shard_entries[feature_id]["base_params"]),
                shard_id=str(shard_entries[feature_id]["shard_id"]),
            )
        )
    missing_rows = set(shard_entries) - {entry.feature_id for entry in entries}
    if missing_rows:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "v2 shard feature is missing from SQLite metadata",
            context={"feature_ids": sorted(missing_rows)},
        )
    tombstones = [
        Tombstone(
            feature_id=str(row["feature_id"]),
            effective_ms=_require_int(row["effective_ms"], "tombstone effective_ms"),
            generation=_require_int(row["generation"], "tombstone generation"),
            reason=_require_str(row["reason"], "tombstone reason"),
        )
        for row in tombstone_rows
    ]
    snapshot = Snapshot(
        format_version=V2_VERSION,
        snapshot_version=snapshot_version,
        snapshot_id=snapshot_id,
        logical_clock_ms=logical_clock_ms,
        entries=entries,
        tombstones=tombstones,
        provenance=dict(manifest.get("migration_provenance", {})),
        source_path=str(path),
        declared_checksum=manifest.get("checksum"),
    )
    _validate_snapshot(snapshot)
    computed = snapshot.checksum()
    if snapshot.declared_checksum is None:
        raise BusinessError("E_FORMAT_CORRUPT", "v2 manifest is missing checksum")
    if snapshot.declared_checksum != computed:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "v2 snapshot checksum does not match merged content",
            context={"declared_checksum": snapshot.declared_checksum, "computed": computed},
        )
    return snapshot


def read_snapshot(path: Path) -> Snapshot:
    """Dispatch by the version header before interpreting any version-specific data."""

    manifest_path = path if path.is_file() else path / "manifest.json"
    if not manifest_path.exists():
        raise BusinessError("E_FORMAT_CORRUPT", f"snapshot path does not exist: {path}")
    if path.is_file():
        try:
            head = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError("E_FORMAT_CORRUPT", f"cannot read snapshot head: {exc}") from exc
        if not isinstance(head, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "snapshot head must be an object")
        version = head.get("format_version", head.get("version"))
        if version not in {V1_VERSION, V2_VERSION}:
            raise BusinessError(
                "E_VERSION_UNSUPPORTED",
                "unknown snapshot format version",
                context={"format_version": version, "path": str(path)},
            )
        if version != V1_VERSION:
            raise BusinessError(
                "E_FORMAT_CORRUPT", "v2 snapshot must be a manifest JSON plus shards and SQLite"
            )
        return _read_v1(path)
    try:
        head = read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("E_FORMAT_CORRUPT", f"cannot read snapshot head: {exc}") from exc
    if not isinstance(head, dict):
        raise BusinessError("E_FORMAT_CORRUPT", "snapshot head must be an object")
    version = head.get("format_version", head.get("version"))
    if version not in {V1_VERSION, V2_VERSION}:
        raise BusinessError(
            "E_VERSION_UNSUPPORTED",
            "unknown snapshot format version",
            context={"format_version": version, "path": str(path)},
        )
    if version != V2_VERSION:
        raise BusinessError("E_FORMAT_CORRUPT", "v1 snapshot cannot be a directory")
    return _read_v2(path)


def _v1_entry_dict(entry: FeatureEntry, epoch_ms: int) -> dict[str, Any]:
    relative_seconds = (entry.effective_ms - epoch_ms) / 1000.0
    return {
        "feature_id": entry.feature_id,
        "effective_seconds": relative_seconds,
        "generation": entry.generation,
        "base_params": entry.base_params,
    }


def _v1_invalid_dict(tombstone: Tombstone, epoch_ms: int) -> dict[str, Any]:
    return {
        "feature_id": tombstone.feature_id,
        "effective_seconds": (tombstone.effective_ms - epoch_ms) / 1000.0,
        "generation": tombstone.generation,
    }


def _write_v1(snapshot: Snapshot, target: Path, *, epoch_ms: int) -> None:
    active = [
        entry
        for entry in snapshot.entries
        if entry.feature_id not in snapshot.invalid_feature_ids
    ]
    active.sort(key=lambda item: (item.effective_ms, item.feature_id))
    invalid = sorted(snapshot.tombstones, key=lambda item: (item.effective_ms, item.feature_id))
    if active and active[0].effective_ms < epoch_ms:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "v1 epoch is later than an active feature boundary",
            context={"epoch_ms": epoch_ms, "first_effective_ms": active[0].effective_ms},
        )
    default_generation = max(
        [entry.generation for entry in active]
        + [tombstone.generation for tombstone in invalid]
        + [0]
    )
    value: dict[str, Any] = {
        "format_version": V1_VERSION,
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_id": snapshot.snapshot_id,
        "logical_clock_ms": snapshot.logical_clock_ms,
        "epoch_ms": epoch_ms,
        "feature_generation": default_generation,
        "entries": [_v1_entry_dict(entry, epoch_ms) for entry in active],
        "invalid_features": [_v1_invalid_dict(item, epoch_ms) for item in invalid],
        "checksum": sha256_json(
            Snapshot(
                format_version=V1_VERSION,
                snapshot_version=snapshot.snapshot_version,
                snapshot_id=snapshot.snapshot_id,
                logical_clock_ms=snapshot.logical_clock_ms,
                entries=active,
                tombstones=[
                    Tombstone(
                        feature_id=item.feature_id,
                        effective_ms=item.effective_ms,
                        generation=item.generation,
                        reason="migrated_from_v1_invalid_features",
                    )
                    for item in invalid
                ],
            ).content_dict()
        ),
    }
    atomic_write_json(target, value)


def _write_v2(snapshot: Snapshot, target_directory: Path, *, shard_size: int = 4) -> None:
    target_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target_directory.name}.", suffix=".tmp", dir=target_directory.parent)
    )
    backup = target_directory.parent / f".{target_directory.name}.backup"
    try:
        entries = sorted(snapshot.entries, key=lambda item: (item.effective_ms, item.feature_id))
        shard_names: list[str] = []
        for shard_index, offset in enumerate(range(0, len(entries), shard_size)):
            shard_name = f"shard-{shard_index:03d}.json"
            shard_names.append(shard_name)
            shard_entries = [
                {"feature_id": entry.feature_id, "base_params": entry.base_params}
                for entry in entries[offset : offset + shard_size]
            ]
            atomic_write_json(
                staging / shard_name,
                {
                    "format_version": V2_VERSION,
                    "shard_id": shard_index,
                    "entries": shard_entries,
                },
            )
        database = staging / "metadata.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE features (
                    feature_id TEXT PRIMARY KEY,
                    effective_ms INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    shard_id TEXT NOT NULL
                );
                CREATE TABLE tombstones (
                    feature_id TEXT PRIMARY KEY,
                    effective_ms INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("format_version", V2_VERSION),
                    ("snapshot_version", str(snapshot.snapshot_version)),
                    ("snapshot_id", snapshot.snapshot_id),
                    ("logical_clock_ms", str(snapshot.logical_clock_ms)),
                ],
            )
            connection.executemany(
                "INSERT INTO features(feature_id, effective_ms, generation, shard_id) VALUES (?, ?, ?, ?)",
                [
                    (
                        entry.feature_id,
                        entry.effective_ms,
                        entry.generation,
                        str(shard_names[(offset // shard_size)])
                        if shard_size
                        else "shard-000.json",
                    )
                    for offset, entry in enumerate(entries)
                ],
            )
            connection.executemany(
                "INSERT INTO tombstones(feature_id, effective_ms, generation, reason) VALUES (?, ?, ?, ?)",
                [
                    (
                        item.feature_id,
                        item.effective_ms,
                        item.generation,
                        item.reason,
                    )
                    for item in snapshot.tombstones
                ],
            )
            connection.commit()
        finally:
            connection.close()
        checksum = snapshot.checksum()
        manifest = {
            "format_version": V2_VERSION,
            "snapshot_version": snapshot.snapshot_version,
            "snapshot_id": snapshot.snapshot_id,
            "logical_clock_ms": snapshot.logical_clock_ms,
            "generation": max(
                [entry.generation for entry in entries]
                + [item.generation for item in snapshot.tombstones]
                + [0]
            ),
            "metadata_db": "metadata.sqlite",
            "shards": shard_names,
            "checksum": checksum,
            "migration_provenance": snapshot.provenance,
        }
        atomic_write_json(staging / "manifest.json", manifest)
        if target_directory.exists():
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target_directory, backup)
            try:
                os.replace(staging, target_directory)
            except BaseException:
                os.replace(backup, target_directory)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, target_directory)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def migrate_snapshot(
    source: Path,
    target: Path,
    *,
    report_path: Path | None = None,
    epoch_ms: int = 0,
) -> dict[str, Any]:
    snapshot = read_snapshot(source)
    before_active = sorted(snapshot.active_feature_ids)
    before_invalid = sorted(snapshot.invalid_feature_ids)
    before_entries = {
        entry.feature_id: {
            "effective_ms": entry.effective_ms,
            "generation": entry.generation,
        }
        for entry in snapshot.entries
    }
    before_tombstones = {
        item.feature_id: {"effective_ms": item.effective_ms, "generation": item.generation}
        for item in snapshot.tombstones
    }
    if snapshot.format_version == V1_VERSION:
        if target.suffix == ".json":
            raise BusinessError(
                "E_FORMAT_CORRUPT", "v1-to-v2 migration target must be a directory"
            )
        provenance = dict(snapshot.provenance)
        migrated = Snapshot(
            format_version=V2_VERSION,
            snapshot_version=snapshot.snapshot_version,
            snapshot_id=snapshot.snapshot_id,
            logical_clock_ms=snapshot.logical_clock_ms,
            entries=snapshot.entries,
            tombstones=snapshot.tombstones,
            provenance=provenance,
        )
        _write_v2(migrated, target)
        direction = "v1-to-v2"
        dropped_tombstones: list[dict[str, Any]] = []
        notes = [
            "absolute milliseconds completed from v1 epoch_ms plus relative seconds",
            "feature generations completed from v1 entry generation or header feature_generation",
            "v1 invalid feature set-only records were promoted to tombstones with explicit provenance",
        ]
    else:
        if target.suffix != ".json":
            raise BusinessError(
                "E_FORMAT_CORRUPT", "v2-to-v1 migration target must be a .json file"
            )
        active_entries = [
            entry for entry in snapshot.entries if entry.feature_id not in snapshot.invalid_feature_ids
        ]
        preserved_tombstones = [item for item in snapshot.tombstones]
        migrated = Snapshot(
            format_version=V1_VERSION,
            snapshot_version=snapshot.snapshot_version,
            snapshot_id=snapshot.snapshot_id,
            logical_clock_ms=snapshot.logical_clock_ms,
            entries=active_entries,
            tombstones=preserved_tombstones,
            provenance={
                "downgrade": {
                    "milliseconds": "absolute_ms - v1.epoch_ms, converted to relative seconds",
                    "tombstones": "full tombstone objects reduced to invalid_features with id/time/generation; reason discarded",
                }
            },
        )
        _write_v1(migrated, target, epoch_ms=epoch_ms)
        direction = "v2-to-v1"
        dropped_tombstones = [
            {
                "feature_id": item.feature_id,
                "effective_ms": item.effective_ms,
                "generation": item.generation,
                "discarded_reason": item.reason,
                "rule": "v1.invalid_features keeps id/time/generation; reason is explicitly discarded",
            }
            for item in snapshot.tombstones
        ]
        notes = [
            "absolute milliseconds were converted to epoch-relative seconds",
            "active feature boundaries and invalid feature set were preserved",
            "tombstone reason is not expressible in v1 and was explicitly discarded",
        ]
    after = read_snapshot(target)
    after_entries = {
        entry.feature_id: {
            "effective_ms": entry.effective_ms,
            "generation": entry.generation,
        }
        for entry in after.entries
    }
    after_tombstones = {
        item.feature_id: {"effective_ms": item.effective_ms, "generation": item.generation}
        for item in after.tombstones
    }
    if after.active_feature_ids != set(before_active):
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "migration changed active feature set",
            context={"before": before_active, "after": sorted(after.active_feature_ids)},
        )
    if after.invalid_feature_ids != set(before_invalid):
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "migration changed invalid feature set",
            context={"before": before_invalid, "after": sorted(after.invalid_feature_ids)},
        )
    for feature_id, before in before_entries.items():
        if after_entries.get(feature_id, {}).get("effective_ms") != before["effective_ms"]:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "migration changed feature millisecond boundary",
                context={"feature_id": feature_id, "before": before, "after": after_entries.get(feature_id)},
            )
    for feature_id, before in before_tombstones.items():
        if after_tombstones.get(feature_id, {}).get("effective_ms") != before["effective_ms"]:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "migration changed tombstone millisecond boundary",
                context={"feature_id": feature_id, "before": before, "after": after_tombstones.get(feature_id)},
            )
    report = {
        "schema": "featurediag.snapshot-migration.v1",
        "direction": direction,
        "source": str(source),
        "target": str(target),
        "source_version": snapshot.format_version,
        "target_version": after.format_version,
        "source_checksum": snapshot.checksum(),
        "target_checksum": after.checksum(),
        "active_features_before": before_active,
        "active_features_after": sorted(after.active_feature_ids),
        "invalid_features_before": before_invalid,
        "invalid_features_after": sorted(after.invalid_feature_ids),
        "millisecond_boundaries_preserved": True,
        "invalid_set_preserved": True,
        "dropped_tombstones": dropped_tombstones,
        "provenance": after.provenance,
        "notes": notes,
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    return report


class SnapshotCommitStore:
    """Single-writer active snapshot store using atomic whole-file replacement."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.active_path = root / "active_snapshot.json"
        self.lock_path = root / "active_snapshot.lock"

    @contextlib.contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise BusinessError(
                    "E_STAGE_BUSY",
                    "another snapshot commit is active",
                    context={"lock_path": str(self.lock_path)},
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def read_active(self) -> Snapshot | None:
        if not self.active_path.exists():
            return None
        try:
            raw = read_json(self.active_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError("E_FORMAT_CORRUPT", f"cannot read active snapshot: {exc}") from exc
        if not isinstance(raw, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "active snapshot must be an object")
        snapshot_raw = _require_field(raw, "snapshot", "active snapshot")
        if not isinstance(snapshot_raw, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "active snapshot payload must be an object")
        return Snapshot.from_active_dict(snapshot_raw)

    def commit(self, snapshot: Snapshot, *, hold_lock_ms: int = 0) -> dict[str, Any]:
        with self.lock(blocking=False):
            active = self.read_active()
            if active is not None:
                if snapshot.snapshot_version < active.snapshot_version:
                    raise BusinessError(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot version moved backwards",
                        context={
                            "active_snapshot_version": active.snapshot_version,
                            "incoming_snapshot_version": snapshot.snapshot_version,
                        },
                    )
                if snapshot.logical_clock_ms < active.logical_clock_ms:
                    raise BusinessError(
                        "E_CLOCK_ROLLBACK",
                        "snapshot logical clock moved backwards",
                        context={
                            "active_logical_clock_ms": active.logical_clock_ms,
                            "incoming_logical_clock_ms": snapshot.logical_clock_ms,
                        },
                    )
            changed = (
                active is None
                or snapshot.snapshot_version != active.snapshot_version
                or snapshot.checksum() != active.checksum()
            )
            if changed:
                payload = {
                    "schema": "featurediag.active-snapshot.v1",
                    "snapshot": snapshot.to_active_dict(),
                    "commit_metadata": {
                        "snapshot_version": snapshot.snapshot_version,
                        "logical_clock_ms": snapshot.logical_clock_ms,
                        "checksum": snapshot.checksum(),
                        "replacement_mode": "whole_snapshot_atomic_replace",
                    },
                }
                atomic_write_json(self.active_path, payload)
            result = {
                "code": "OK",
                "changed": changed,
                "replacement_mode": "whole_snapshot_atomic_replace" if changed else "unchanged",
                "snapshot_version": snapshot.snapshot_version,
                "checksum": snapshot.checksum(),
                "active_path": str(self.active_path),
            }
            if hold_lock_ms > 0:
                time.sleep(hold_lock_ms / 1000.0)
            return result
