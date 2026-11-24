from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .errors import SessionFixError
from .models import (
    PolicyConflict,
    PolicyRecord,
    PolicyView,
    Rejection,
    normalize_key,
)
from .util import canonical_json, digest_json, read_json, safe_relative_path


def _record_rejection(
    category: str,
    index: int,
    code: str,
    message: str,
    source_ref: str,
    raw: Any,
) -> Rejection:
    return Rejection(
        source_category=category,
        record_index=index,
        code=code,
        message=message,
        source_ref=source_ref,
        record_digest=digest_json(raw),
    )


def _require_int(value: Any, name: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _record_list(document: Any, wrapper_key: str) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get(wrapper_key), list):
        return list(document[wrapper_key])
    if isinstance(document, dict) and "policy_key" in document:
        return [document]
    raise ValueError(f"expected a list or an object containing '{wrapper_key}'")


def load_policy_v1(
    path: Path,
    observed_clock_ms: int,
) -> tuple[list[PolicyRecord], list[Rejection]]:
    if not path.exists():
        raise SessionFixError("E_POLICY_V1_MISSING", f"v1 policy file not found: {path}")
    try:
        document = read_json(path)
        raw_records = _record_list(document, "records")
    except (OSError, ValueError, TypeError) as exc:
        raise SessionFixError(
            "E_POLICY_V1_INVALID",
            f"cannot parse v1 policy file: {exc}",
            str(path),
        ) from exc

    records: list[PolicyRecord] = []
    rejections: list[Rejection] = []
    for index, raw in enumerate(sorted(raw_records, key=canonical_json)):
        source_ref = f"{path.name}#{index}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("record must be an object")
            policy_key = normalize_key(_require_text(raw.get("policy_key"), "policy_key"))
            relative_seconds = _require_int(
                raw.get("relative_start_seconds"),
                "relative_start_seconds",
                nonnegative=True,
            )
            minimum_seconds = _require_int(
                raw.get("min_rotation_interval_seconds"),
                "min_rotation_interval_seconds",
                nonnegative=True,
            )
            rotation_mode = _require_text(raw.get("rotation_mode"), "rotation_mode")
            bind_required = _require_bool(
                raw.get("bind_device_required", True),
                "bind_device_required",
            )
            generation = _require_int(
                raw.get("policy_generation", 1),
                "policy_generation",
                nonnegative=True,
            )
            tombstone = _require_bool(raw.get("tombstone", False), "tombstone")
            records.append(
                PolicyRecord(
                    policy_key=policy_key,
                    policy_generation=generation,
                    effective_at_ms=observed_clock_ms + relative_seconds * 1000,
                    rotation_mode=rotation_mode,
                    min_rotation_interval_ms=minimum_seconds * 1000,
                    bind_device_required=bind_required,
                    tombstone=tombstone,
                    source_format="v1",
                    source_ref=source_ref,
                    record_index=index,
                )
            )
        except (TypeError, ValueError) as exc:
            rejections.append(
                _record_rejection(
                    "policy_v1",
                    index,
                    "E_POLICY_V1_RECORD",
                    str(exc),
                    source_ref,
                    raw,
                )
            )
    return records, rejections


def _read_v2_metadata(
    db_path: Path,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if not db_path.exists():
        raise SessionFixError(
            "E_POLICY_META_MISSING",
            f"v2 policy metadata database not found: {db_path}",
            str(db_path),
        )
    try:
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            metadata_rows = connection.execute(
                "SELECT key, value FROM policy_metadata"
            ).fetchall()
            shard_rows = connection.execute(
                "SELECT path, sha256, record_count FROM policy_shards"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SessionFixError(
            "E_POLICY_META_INVALID",
            f"cannot read v2 policy metadata: {exc}",
            str(db_path),
        ) from exc
    metadata = {str(key): str(value) for key, value in metadata_rows}
    try:
        shards = {
            str(path): {
                "path": str(path),
                "sha256": str(sha),
                "record_count": int(record_count),
            }
            for path, sha, record_count in shard_rows
        }
    except (TypeError, ValueError) as exc:
        raise SessionFixError(
            "E_POLICY_META_INVALID",
            f"invalid record_count in policy metadata: {exc}",
            str(db_path),
        ) from exc
    return metadata, shards


def _v2_shard_entries(index: dict[str, Any], db_shards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw_shards = index.get("shards")
    entries: dict[str, dict[str, Any]] = {}
    if raw_shards is not None:
        if not isinstance(raw_shards, list):
            raise SessionFixError("E_POLICY_V2_INDEX", "v2 shards must be a list")
        for item in raw_shards:
            if isinstance(item, str):
                entries[item] = {"path": item}
            elif isinstance(item, dict):
                path = str(item.get("path", ""))
                if not path:
                    raise SessionFixError(
                        "E_POLICY_V2_INDEX", "v2 shard path must not be empty"
                    )
                entries[path] = {
                    "path": path,
                    "sha256": item.get("sha256"),
                }
            else:
                raise SessionFixError(
                    "E_POLICY_V2_INDEX", "v2 shard entry must be a string or object"
                )
    for path, meta in db_shards.items():
        entry = entries.setdefault(path, {"path": path})
        if entry.get("sha256") not in {None, meta["sha256"]}:
            raise SessionFixError(
                "E_POLICY_META_MISMATCH",
                f"index and metadata disagree on sha256 for {path}",
            )
        if (
            entry.get("record_count") is not None
            and int(entry["record_count"]) != meta["record_count"]
        ):
            raise SessionFixError(
                "E_POLICY_META_MISMATCH",
                f"index and metadata disagree on record_count for {path}",
            )
        entry["sha256"] = meta["sha256"]
        entry["record_count"] = meta["record_count"]
    return [entries[path] for path in sorted(entries)]


def load_policy_v2(path: Path) -> tuple[list[PolicyRecord], list[Rejection]]:
    if not path.exists():
        raise SessionFixError("E_POLICY_V2_MISSING", f"v2 policy path not found: {path}")
    if path.is_dir():
        index_path = path / "index.json"
        root = path
    else:
        index_path = path
        root = path.parent
    if not index_path.exists():
        raise SessionFixError(
            "E_POLICY_V2_INDEX", f"v2 policy index not found: {index_path}"
        )
    try:
        index = read_json(index_path)
    except (OSError, ValueError, TypeError) as exc:
        raise SessionFixError(
            "E_POLICY_V2_INDEX",
            f"cannot parse v2 policy index: {exc}",
            str(index_path),
        ) from exc
    if not isinstance(index, dict):
        raise SessionFixError(
            "E_POLICY_V2_INDEX", "v2 policy index must be an object"
        )
    metadata_rel = str(index.get("metadata_db", "policy_meta.sqlite3"))
    try:
        metadata_path = safe_relative_path(root, metadata_rel)
    except ValueError as exc:
        raise SessionFixError("E_POLICY_META_INVALID", str(exc)) from exc
    metadata, db_shards = _read_v2_metadata(metadata_path)
    if metadata.get("format_version") not in {None, "2"}:
        raise SessionFixError(
            "E_POLICY_META_INVALID",
            "v2 policy metadata format_version must be 2",
            str(metadata_path),
        )
    entries = _v2_shard_entries(index, db_shards)

    records: list[PolicyRecord] = []
    rejections: list[Rejection] = []
    for shard_entry in entries:
        shard_rel = str(shard_entry["path"])
        try:
            shard_path = safe_relative_path(root, shard_rel)
        except ValueError as exc:
            rejections.append(
                _record_rejection(
                    "policy_v2_shard",
                    0,
                    "E_POLICY_V2_SHARD_PATH",
                    str(exc),
                    shard_rel,
                    shard_entry,
                )
            )
            continue
        if not shard_path.exists():
            rejections.append(
                _record_rejection(
                    "policy_v2_shard",
                    0,
                    "E_POLICY_V2_SHARD_MISSING",
                    f"shard not found: {shard_rel}",
                    shard_rel,
                    shard_entry,
                )
            )
            continue
        data = shard_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected = shard_entry.get("sha256") or db_shards.get(shard_rel, {}).get("sha256")
        if expected and digest != expected:
            rejections.append(
                _record_rejection(
                    "policy_v2_shard",
                    0,
                    "E_POLICY_V2_SHARD_HASH",
                    f"shard sha256 mismatch: expected {expected}, got {digest}",
                    shard_rel,
                    shard_entry,
                )
            )
            continue
        try:
            document = read_json(shard_path)
            raw_records = _record_list(document, "records")
        except (OSError, ValueError, TypeError) as exc:
            rejections.append(
                _record_rejection(
                    "policy_v2_shard",
                    0,
                    "E_POLICY_V2_SHARD_INVALID",
                    f"cannot parse shard: {exc}",
                    shard_rel,
                    shard_entry,
                )
            )
            continue
        expected_count = shard_entry.get("record_count")
        if expected_count is not None and int(expected_count) != len(raw_records):
            rejections.append(
                _record_rejection(
                    "policy_v2_shard",
                    0,
                    "E_POLICY_V2_SHARD_COUNT",
                    (
                        f"metadata record_count={expected_count}, "
                        f"shard contains {len(raw_records)}"
                    ),
                    shard_rel,
                    shard_entry,
                )
            )
        for index, raw in enumerate(sorted(raw_records, key=canonical_json)):
            source_ref = f"{shard_rel}#{index}"
            try:
                if not isinstance(raw, dict):
                    raise ValueError("record must be an object")
                policy_key = normalize_key(
                    _require_text(raw.get("policy_key"), "policy_key")
                )
                effective_at_ms = _require_int(
                    raw.get("effective_at_ms"),
                    "effective_at_ms",
                    nonnegative=True,
                )
                generation = _require_int(
                    raw.get("policy_generation"),
                    "policy_generation",
                    nonnegative=True,
                )
                tombstone = _require_bool(
                    raw.get("tombstone", False),
                    "tombstone",
                )
                rotation_value = raw.get("rotation_mode")
                rotation_mode = (
                    "observe"
                    if rotation_value is None and tombstone
                    else _require_text(rotation_value, "rotation_mode")
                )
                binding_value = raw.get("bind_device_required")
                binding_required = (
                    False
                    if binding_value is None and tombstone
                    else _require_bool(
                        binding_value,
                        "bind_device_required",
                    )
                )
                minimum_ms: int | None = None
                if "min_rotation_interval_seconds" in raw:
                    minimum_ms = (
                        _require_int(
                            raw.get("min_rotation_interval_seconds"),
                            "min_rotation_interval_seconds",
                            nonnegative=True,
                        )
                        * 1000
                    )
                elif "min_rotation_interval_ms" in raw:
                    minimum_ms = _require_int(
                        raw.get("min_rotation_interval_ms"),
                        "min_rotation_interval_ms",
                        nonnegative=True,
                    )
                records.append(
                    PolicyRecord(
                        policy_key=policy_key,
                        policy_generation=generation,
                        effective_at_ms=effective_at_ms,
                        rotation_mode=rotation_mode,
                        min_rotation_interval_ms=minimum_ms,
                        bind_device_required=binding_required,
                        tombstone=tombstone,
                        source_format="v2",
                        source_ref=source_ref,
                        record_index=index,
                    )
                )
            except (TypeError, ValueError) as exc:
                rejections.append(
                    _record_rejection(
                        "policy_v2_shard",
                        index,
                        "E_POLICY_V2_RECORD",
                        str(exc),
                        source_ref,
                        raw,
                    )
                )
    return records, rejections


def merge_policies(
    records: list[PolicyRecord],
) -> tuple[dict[str, PolicyView], tuple[PolicyConflict, ...], list[Rejection]]:
    groups: dict[str, list[PolicyRecord]] = {}
    for record in records:
        groups.setdefault(record.policy_key, []).append(record)

    views: dict[str, PolicyView] = {}
    conflicts: list[PolicyConflict] = []
    rejections: list[Rejection] = []
    for policy_key in sorted(groups):
        group = sorted(groups[policy_key], key=lambda item: item.authority_key)
        winner = group[-1]
        v1_records = [item for item in group if item.source_format == "v1"]
        v2_records = [item for item in group if item.source_format == "v2"]
        v1_winner = v1_records[-1] if v1_records else None
        v2_winner = v2_records[-1] if v2_records else None

        view_conflicts: list[PolicyConflict] = []
        if (
            v1_winner is not None
            and v2_winner is not None
            and not v1_winner.tombstone
            and not v2_winner.tombstone
        ):
            comparisons = (
                ("rotation_mode", v1_winner.rotation_mode, v2_winner.rotation_mode),
                (
                    "binding_required",
                    v1_winner.bind_device_required,
                    v2_winner.bind_device_required,
                ),
            )
            for field_name, v1_value, v2_value in comparisons:
                if v1_value != v2_value:
                    conflict = PolicyConflict(
                        policy_key=policy_key,
                        field=field_name,
                        v1_value=v1_value,
                        v2_value=v2_value,
                        v1_record=v1_winner.to_dict(),
                        v2_record=v2_winner.to_dict(),
                    )
                    view_conflicts.append(conflict)
                    conflicts.append(conflict)

        if winner.tombstone:
            rotation_mode: str | None = winner.rotation_mode
            binding_required: bool | None = winner.bind_device_required
        else:
            rotation_mode = winner.rotation_mode
            binding_required = winner.bind_device_required
            for conflict in view_conflicts:
                if conflict.field == "rotation_mode":
                    rotation_mode = None
                elif conflict.field == "binding_required":
                    binding_required = None

        minimum = winner.min_rotation_interval_ms
        if minimum is None:
            alternatives = [
                item.min_rotation_interval_ms
                for item in group
                if item.min_rotation_interval_ms is not None
            ]
            minimum = max(alternatives) if alternatives else 0

        views[policy_key] = PolicyView(
            policy_key=policy_key,
            policy_generation=winner.policy_generation,
            effective_at_ms=winner.effective_at_ms,
            rotation_mode=rotation_mode,
            min_rotation_interval_ms=minimum,
            binding_required=binding_required,
            tombstone=winner.tombstone,
            authority_source=winner.source_format,
            authority_record=winner.to_dict(),
            conflicts=tuple(sorted(view_conflicts, key=lambda item: item.stable_key)),
            records=tuple(group),
        )

    conflict_tuple = tuple(
        sorted(conflicts, key=lambda item: (item.policy_key, item.field))
    )
    return views, conflict_tuple, rejections
