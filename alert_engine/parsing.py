"""Strict parsers for the deterministic offline input files."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .codes import Code
from .errors import RejectRecord
from .models import (
    Alert,
    ParsedInputs,
    PatchBatch,
    RulePatch,
    RuleVersion,
)
from .util import (
    canonical_rule_key,
    digest_bytes,
    digest_file,
    is_int,
    is_nonnegative_int,
    normalize_digest,
    normalize_process_guid,
    stable_json,
)


_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _reject(
    rejections: list[dict],
    code: str,
    message: str,
    *,
    source: str,
    record_index: int,
    field: str | None = None,
    details: dict | None = None,
) -> None:
    rejections.append(
        RejectRecord(
            code,
            message,
            source=source,
            record_index=record_index,
            field=field,
            details=details,
        ).as_dict()
    )


def _read_json_file(
    path: Path,
    *,
    source: str,
    rejections: list[dict],
) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _reject(
            rejections,
            Code.E_JSON,
            "file cannot be parsed as JSON",
            source=source,
            record_index=0,
            field="file",
            details={"path": str(path), "error": str(exc)},
        )
        return None


def _string_list(
    value: Any,
    *,
    field_name: str,
    required: bool = True,
) -> tuple[str, ...] | None:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        return None
    return tuple(sorted(set(normalized)))


def parse_v1(
    path: Path,
    rejections: list[dict],
) -> tuple[int, int, int, list[RuleVersion]]:
    """Return observation clock, severity range and normalized v1 versions."""

    document = _read_json_file(path, source="v1", rejections=rejections)
    if not isinstance(document, dict):
        if document is not None:
            _reject(
                rejections,
                Code.E_SCHEMA,
                "v1 root must be an object",
                source="v1",
                record_index=0,
            )
        return 0, 0, 0, []

    observation_clock = document.get("observation_clock_ms")
    clock_valid = is_nonnegative_int(observation_clock)
    if not clock_valid:
        _reject(
            rejections,
            Code.E_V1_TIME,
            "observation_clock_ms must be a non-negative integer",
            source="v1",
            record_index=0,
            field="observation_clock_ms",
        )
        observation_clock = 0

    severity_range = document.get("severity_range")
    if isinstance(severity_range, dict):
        severity_min = severity_range.get("min")
        severity_max = severity_range.get("max")
    else:
        severity_min = document.get("severity_min")
        severity_max = document.get("severity_max")
    if (
        not is_nonnegative_int(severity_min)
        or not is_nonnegative_int(severity_max)
        or severity_min > severity_max
    ):
        _reject(
            rejections,
            Code.E_SEVERITY,
            "declared severity range is invalid",
            source="v1",
            record_index=0,
            field="severity_range",
        )
        severity_min, severity_max = 0, 0

    rules = document.get("rules")
    if not isinstance(rules, list):
        _reject(
            rejections,
            Code.E_SCHEMA,
            "v1 rules must be an array",
            source="v1",
            record_index=0,
            field="rules",
        )
        return observation_clock, severity_min, severity_max, []

    versions: list[RuleVersion] = []
    for index, record in enumerate(rules, start=1):
        if not clock_valid:
            _reject(
                rejections,
                Code.E_V1_TIME,
                "v1 rule cannot be converted without a valid observation clock",
                source="v1",
                record_index=index,
                field="observation_clock_ms",
            )
            continue
        if not isinstance(record, dict):
            _reject(
                rejections,
                Code.E_V1_RULE,
                "v1 rule must be an object",
                source="v1",
                record_index=index,
            )
            continue
        raw_key = record.get("rule_key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            _reject(
                rejections,
                Code.E_RULE_KEY,
                "rule_key must be a non-empty string",
                source="v1",
                record_index=index,
                field="rule_key",
            )
            continue
        source_key = raw_key.strip()
        canonical = record.get("canonical_rule_key", source_key)
        if not isinstance(canonical, str) or not canonical.strip():
            _reject(
                rejections,
                Code.E_RULE_KEY,
                "canonical_rule_key must be a non-empty string",
                source="v1",
                record_index=index,
                field="canonical_rule_key",
            )
            continue
        effective_after_s = record.get("effective_after_s")
        dedup_window_s = record.get("dedup_window_s")
        if not is_nonnegative_int(effective_after_s):
            _reject(
                rejections,
                Code.E_V1_TIME,
                "effective_after_s must be a non-negative integer",
                source="v1",
                record_index=index,
                field="effective_after_s",
            )
            continue
        if not is_nonnegative_int(dedup_window_s):
            _reject(
                rejections,
                Code.E_V1_WINDOW,
                "dedup_window_s must be a non-negative integer",
                source="v1",
                record_index=index,
                field="dedup_window_s",
            )
            continue
        match_strings = _string_list(
            record.get("match_strings"),
            field_name="match_strings",
        )
        if match_strings is None:
            _reject(
                rejections,
                Code.E_V1_RULE,
                "match_strings must be an array of non-empty strings",
                source="v1",
                record_index=index,
                field="match_strings",
            )
            continue
        enabled = record.get("enabled")
        if not isinstance(enabled, bool):
            _reject(
                rejections,
                Code.E_V1_RULE,
                "enabled must be a boolean",
                source="v1",
                record_index=index,
                field="enabled",
            )
            continue

        # v1 has one-second granularity.  A second field is interpreted as a
        # floor value, so the unknown sub-second part is in [0, 999] ms.
        versions.append(
            RuleVersion(
                rule_key=canonical_rule_key(canonical),
                source_rule_key=source_key,
                source="v1",
                effective_ms=observation_clock + effective_after_s * 1000,
                effective_error_ms=999,
                generation=1,
                dedup_window_ms=dedup_window_s * 1000,
                window_error_ms=999,
                match_strings=match_strings,
                enabled=enabled,
                tombstone=False,
                parent_rule_keys=(),
                record_index=index,
            )
        )
    return observation_clock, severity_min, severity_max, versions


def _parse_v2_metadata(
    path: Path,
    rejections: list[dict],
) -> dict[tuple[str, int, int], dict]:
    rows: dict[tuple[str, int, int], dict] = {}
    if not path.exists():
        _reject(
            rejections,
            Code.E_SCHEMA,
            "v2 metadata database does not exist",
            source="v2_meta",
            record_index=0,
            details={"path": str(path)},
        )
        return rows
    try:
        metadata_uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(metadata_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(
                """
                SELECT rule_key, effective_ms, generation, dedup_window_ms,
                       tombstone, parent_rule_keys_json, shard_id,
                       record_index
                  FROM v2_rule_meta
                 ORDER BY rule_key, effective_ms, generation
                """
            )
            for row_number, row in enumerate(cursor, start=1):
                raw_key = row["rule_key"]
                effective_ms = row["effective_ms"]
                generation = row["generation"]
                dedup_window_ms = row["dedup_window_ms"]
                tombstone = row["tombstone"]
                parent_json = row["parent_rule_keys_json"]
                if not isinstance(raw_key, str) or not raw_key.strip():
                    _reject(
                        rejections,
                        Code.E_RULE_KEY,
                        "metadata rule_key must be a non-empty string",
                        source="v2_meta",
                        record_index=row_number,
                        field="rule_key",
                    )
                    continue
                if not is_nonnegative_int(effective_ms):
                    _reject(
                        rejections,
                        Code.E_V2_TIME,
                        "metadata effective_ms must be non-negative integer",
                        source="v2_meta",
                        record_index=row_number,
                        field="effective_ms",
                    )
                    continue
                if not is_nonnegative_int(generation):
                    _reject(
                        rejections,
                        Code.E_V2_GENERATION,
                        "metadata generation must be non-negative integer",
                        source="v2_meta",
                        record_index=row_number,
                        field="generation",
                    )
                    continue
                if not is_nonnegative_int(dedup_window_ms):
                    _reject(
                        rejections,
                        Code.E_V2_WINDOW,
                        "metadata dedup_window_ms must be non-negative integer",
                        source="v2_meta",
                        record_index=row_number,
                        field="dedup_window_ms",
                    )
                    continue
                if tombstone not in (0, 1):
                    _reject(
                        rejections,
                        Code.E_SCHEMA,
                        "metadata tombstone must be 0 or 1",
                        source="v2_meta",
                        record_index=row_number,
                        field="tombstone",
                    )
                    continue
                try:
                    parents_value = json.loads(parent_json or "[]")
                except (TypeError, json.JSONDecodeError):
                    parents_value = None
                parents = _string_list(parents_value, field_name="parent_rule_keys")
                if parents is None:
                    _reject(
                        rejections,
                        Code.E_SCHEMA,
                        "metadata parent_rule_keys_json must encode a string array",
                        source="v2_meta",
                        record_index=row_number,
                        field="parent_rule_keys_json",
                    )
                    continue
                key = (
                    canonical_rule_key(raw_key),
                    effective_ms,
                    generation,
                )
                if key in rows:
                    _reject(
                        rejections,
                        Code.E_V2_DUP,
                        "duplicate v2 metadata rule version",
                        source="v2_meta",
                        record_index=row_number,
                        details={
                            "rule_key": key[0],
                            "effective_ms": effective_ms,
                            "generation": generation,
                        },
                    )
                    continue
                rows[key] = {
                    "dedup_window_ms": dedup_window_ms,
                    "tombstone": bool(tombstone),
                    "parent_rule_keys": tuple(
                        sorted(canonical_rule_key(item) for item in parents)
                    ),
                    "shard_id": row["shard_id"],
                    "record_index": row["record_index"],
                }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        _reject(
            rejections,
            Code.E_SCHEMA,
            "v2 metadata database cannot be read",
            source="v2_meta",
            record_index=0,
            details={"path": str(path), "error": str(exc)},
        )
    return rows


def _iter_v2_shard_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(item for item in path.glob("*.json") if item.is_file())
    return [path] if path.is_file() else []


def parse_v2(
    shards_path: Path,
    metadata_path: Path,
    rejections: list[dict],
) -> tuple[list[RuleVersion], list[RuleVersion], dict[str, str]]:
    """Parse v2 shards and cross-check them with the SQLite metadata DB."""

    metadata = _parse_v2_metadata(metadata_path, rejections)
    matched_metadata: set[tuple[str, int, int]] = set()
    shard_files = _iter_v2_shard_files(shards_path)
    if not shard_files:
        _reject(
            rejections,
            Code.E_SCHEMA,
            "no v2 shard JSON files found",
            source="v2_shard",
            record_index=0,
            details={"path": str(shards_path)},
        )

    candidates: list[RuleVersion] = []
    hashes: dict[str, str] = {}
    global_index = 0
    for shard_path in shard_files:
        hashes[f"v2_shards/{shard_path.name}"] = digest_file(shard_path)
        document = _read_json_file(
            shard_path,
            source="v2_shard",
            rejections=rejections,
        )
        if not isinstance(document, dict):
            continue
        rules = document.get("rules")
        if not isinstance(rules, list):
            _reject(
                rejections,
                Code.E_SCHEMA,
                "v2 shard rules must be an array",
                source="v2_shard",
                record_index=0,
                details={"shard": shard_path.name},
            )
            continue
        shard_id = document.get("shard_id", shard_path.stem)
        if not isinstance(shard_id, str) or not shard_id.strip():
            shard_id = shard_path.stem
        for local_index, record in enumerate(rules, start=1):
            global_index += 1
            index = global_index
            if not isinstance(record, dict):
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "v2 rule must be an object",
                    source="v2_shard",
                    record_index=index,
                    details={"shard": shard_path.name, "local_index": local_index},
                )
                continue
            raw_key = record.get("rule_key")
            if not isinstance(raw_key, str) or not raw_key.strip():
                _reject(
                    rejections,
                    Code.E_RULE_KEY,
                    "rule_key must be a non-empty string",
                    source="v2_shard",
                    record_index=index,
                    field="rule_key",
                    details={"shard": shard_path.name},
                )
                continue
            source_key = raw_key.strip()
            canonical = record.get("canonical_rule_key", source_key)
            if not isinstance(canonical, str) or not canonical.strip():
                _reject(
                    rejections,
                    Code.E_RULE_KEY,
                    "canonical_rule_key must be a non-empty string",
                    source="v2_shard",
                    record_index=index,
                    field="canonical_rule_key",
                    details={"shard": shard_path.name},
                )
                continue
            key = canonical_rule_key(canonical)
            effective_ms = record.get("effective_ms")
            generation = record.get("generation")
            dedup_window_ms = record.get("dedup_window_ms")
            if not is_nonnegative_int(effective_ms):
                _reject(
                    rejections,
                    Code.E_V2_TIME,
                    "effective_ms must be a non-negative integer",
                    source="v2_shard",
                    record_index=index,
                    field="effective_ms",
                    details={"shard": shard_path.name},
                )
                continue
            if not is_nonnegative_int(generation):
                _reject(
                    rejections,
                    Code.E_V2_GENERATION,
                    "generation must be a non-negative integer",
                    source="v2_shard",
                    record_index=index,
                    field="generation",
                    details={"shard": shard_path.name},
                )
                continue
            if not is_nonnegative_int(dedup_window_ms):
                _reject(
                    rejections,
                    Code.E_V2_WINDOW,
                    "dedup_window_ms must be a non-negative integer",
                    source="v2_shard",
                    record_index=index,
                    field="dedup_window_ms",
                    details={"shard": shard_path.name},
                )
                continue
            tombstone = record.get("tombstone")
            if not isinstance(tombstone, bool):
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "tombstone must be a boolean",
                    source="v2_shard",
                    record_index=index,
                    field="tombstone",
                    details={"shard": shard_path.name},
                )
                continue
            enabled = record.get("enabled")
            if not isinstance(enabled, bool):
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "enabled must be a boolean",
                    source="v2_shard",
                    record_index=index,
                    field="enabled",
                    details={"shard": shard_path.name},
                )
                continue
            match_strings = _string_list(
                record.get("match_strings"),
                field_name="match_strings",
            )
            if match_strings is None:
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "match_strings must be an array of non-empty strings",
                    source="v2_shard",
                    record_index=index,
                    field="match_strings",
                    details={"shard": shard_path.name},
                )
                continue
            parents = _string_list(
                record.get("parent_rule_keys", []),
                field_name="parent_rule_keys",
            )
            if parents is None:
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "parent_rule_keys must be an array of non-empty strings",
                    source="v2_shard",
                    record_index=index,
                    field="parent_rule_keys",
                    details={"shard": shard_path.name},
                )
                continue
            parent_keys = tuple(sorted(canonical_rule_key(item) for item in parents))

            metadata_key = (key, effective_ms, generation)
            meta = metadata.get(metadata_key)
            if meta is None:
                _reject(
                    rejections,
                    Code.E_V2_META_MISSING,
                    "v2 shard rule has no matching metadata row",
                    source="v2_shard",
                    record_index=index,
                    details={
                        "shard": shard_path.name,
                        "rule_key": key,
                        "effective_ms": effective_ms,
                        "generation": generation,
                    },
                )
                continue
            matched_metadata.add(metadata_key)
            if (
                meta["dedup_window_ms"] != dedup_window_ms
                or meta["tombstone"] != tombstone
                or meta["parent_rule_keys"] != parent_keys
            ):
                _reject(
                    rejections,
                    Code.E_V2_META_MISMATCH,
                    "v2 shard and metadata fields disagree",
                    source="v2_shard",
                    record_index=index,
                    details={
                        "shard": shard_path.name,
                        "rule_key": key,
                        "effective_ms": effective_ms,
                        "generation": generation,
                        "shard_fields": {
                            "dedup_window_ms": dedup_window_ms,
                            "tombstone": tombstone,
                            "parent_rule_keys": list(parent_keys),
                        },
                        "metadata_fields": {
                            "dedup_window_ms": meta["dedup_window_ms"],
                            "tombstone": meta["tombstone"],
                            "parent_rule_keys": list(meta["parent_rule_keys"]),
                        },
                    },
                )
                continue
            candidates.append(
                RuleVersion(
                    rule_key=key,
                    source_rule_key=source_key,
                    source="v2",
                    effective_ms=effective_ms,
                    effective_error_ms=0,
                    generation=generation,
                    dedup_window_ms=dedup_window_ms,
                    window_error_ms=0,
                    match_strings=match_strings,
                    enabled=enabled,
                    tombstone=tombstone,
                    parent_rule_keys=parent_keys,
                    record_index=index,
                    shard_id=shard_id,
                )
            )

    for metadata_key in sorted(metadata):
        if metadata_key not in matched_metadata:
            _reject(
                rejections,
                Code.E_V2_META_MISMATCH,
                "metadata row has no matching shard rule",
                source="v2_meta",
                record_index=int(metadata[metadata_key].get("record_index") or 0),
                details={
                    "rule_key": metadata_key[0],
                    "effective_ms": metadata_key[1],
                    "generation": metadata_key[2],
                },
            )

    accepted, duplicates = _validate_v2_chain(candidates, rejections)
    return accepted, duplicates, hashes


def _validate_v2_chain(
    candidates: Iterable[RuleVersion],
    rejections: list[dict],
) -> tuple[list[RuleVersion], list[RuleVersion]]:
    """Enforce strictly increasing generation along each v2 effective chain."""

    by_key: dict[str, list[RuleVersion]] = {}
    for version in candidates:
        by_key.setdefault(version.rule_key, []).append(version)

    accepted: list[RuleVersion] = []
    duplicates: list[RuleVersion] = []
    for key in sorted(by_key):
        versions = sorted(
            by_key[key],
            key=lambda item: (
                item.effective_ms,
                item.generation,
                item.record_index,
            ),
        )
        unique: list[RuleVersion] = []
        seen_pairs: set[tuple[int, int]] = set()
        for version in versions:
            pair = (version.effective_ms, version.generation)
            if pair in seen_pairs:
                duplicates.append(version)
                _reject(
                    rejections,
                    Code.E_V2_DUP,
                    "duplicate v2 rule version for the same rule key",
                    source="v2_shard",
                    record_index=version.record_index,
                    details={
                        "rule_key": key,
                        "effective_ms": version.effective_ms,
                        "generation": version.generation,
                    },
                )
                continue
            seen_pairs.add(pair)
            unique.append(version)

        previous_max: int | None = None
        groups: dict[int, list[RuleVersion]] = {}
        for version in unique:
            groups.setdefault(version.effective_ms, []).append(version)
        for effective_ms in sorted(groups):
            group = groups[effective_ms]
            minimum_generation = min(item.generation for item in group)
            if previous_max is not None and minimum_generation <= previous_max:
                for version in group:
                    _reject(
                        rejections,
                        Code.E_V2_GENERATION_ORDER,
                        "generation is not monotonic along the v2 effective chain",
                        source="v2_shard",
                        record_index=version.record_index,
                        field="generation",
                        details={
                            "rule_key": key,
                            "effective_ms": effective_ms,
                            "generation": version.generation,
                            "previous_effective_generation": previous_max,
                        },
                    )
                continue
            accepted.extend(group)
            previous_max = max(item.generation for item in group)
    return accepted, duplicates


def parse_alerts(
    path: Path,
    *,
    allowed_rule_keys: set[str],
    severity_min: int,
    severity_max: int,
    rejections: list[dict],
) -> list[Alert]:
    alerts: list[Alert] = []
    seen_ids: dict[str, int] = {}
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        _reject(
            rejections,
            Code.E_JSON,
            "alert stream cannot be opened",
            source="alert",
            record_index=0,
            details={"path": str(path), "error": str(exc)},
        )
        return alerts

    with handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                _reject(
                    rejections,
                    Code.E_JSON,
                    "blank alert record",
                    source="alert",
                    record_index=index,
                )
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                _reject(
                    rejections,
                    Code.E_JSON,
                    "alert record is not valid JSON",
                    source="alert",
                    record_index=index,
                    details={"error": str(exc)},
                )
                continue
            if not isinstance(record, dict):
                _reject(
                    rejections,
                    Code.E_SCHEMA,
                    "alert record must be an object",
                    source="alert",
                    record_index=index,
                )
                continue

            alert_id = record.get("alert_id")
            if not isinstance(alert_id, str) or not alert_id.strip():
                _reject(
                    rejections,
                    Code.E_ALERT_ID,
                    "alert_id must be a non-empty string",
                    source="alert",
                    record_index=index,
                    field="alert_id",
                )
                continue
            alert_id = alert_id.strip()
            alert_ts_ms = record.get("alert_ts_ms")
            if not is_nonnegative_int(alert_ts_ms):
                _reject(
                    rejections,
                    Code.E_ALERT_TS,
                    "alert_ts_ms must be a non-negative integer",
                    source="alert",
                    record_index=index,
                    field="alert_ts_ms",
                    details={"alert_id": alert_id},
                )
                continue
            if alert_id in seen_ids:
                _reject(
                    rejections,
                    Code.E_ALERT_ID_DUP,
                    "alert_id is not unique",
                    source="alert",
                    record_index=index,
                    field="alert_id",
                    details={
                        "alert_id": alert_id,
                        "first_ts_ms": seen_ids[alert_id],
                        "duplicate_ts_ms": alert_ts_ms,
                        "timestamp_conflict": seen_ids[alert_id] != alert_ts_ms,
                    },
                )
                continue

            host_id = record.get("host_id")
            if not isinstance(host_id, str) or not host_id.strip():
                _reject(
                    rejections,
                    Code.E_HOST_ID,
                    "host_id must be a non-empty string",
                    source="alert",
                    record_index=index,
                    field="host_id",
                    details={"alert_id": alert_id},
                )
                continue
            host_id = host_id.strip()

            raw_rule_key = record.get("rule_key")
            if not isinstance(raw_rule_key, str) or not raw_rule_key.strip():
                _reject(
                    rejections,
                    Code.E_RULE_KEY,
                    "rule_key must be a non-empty string",
                    source="alert",
                    record_index=index,
                    field="rule_key",
                    details={"alert_id": alert_id},
                )
                continue
            raw_rule_key = raw_rule_key.strip()
            rule_key = canonical_rule_key(raw_rule_key)
            if rule_key not in allowed_rule_keys:
                _reject(
                    rejections,
                    Code.E_RULE_UNKNOWN,
                    "rule_key cannot be resolved from any rule snapshot",
                    source="alert",
                    record_index=index,
                    field="rule_key",
                    details={"alert_id": alert_id, "rule_key": rule_key},
                )
                continue

            severity = record.get("severity")
            if (
                not is_int(severity)
                or severity < severity_min
                or severity > severity_max
            ):
                _reject(
                    rejections,
                    Code.E_SEVERITY,
                    "severity is outside the declared range",
                    source="alert",
                    record_index=index,
                    field="severity",
                    details={
                        "alert_id": alert_id,
                        "severity": severity,
                        "minimum": severity_min,
                        "maximum": severity_max,
                    },
                )
                continue

            process_guid = record.get("process_guid")
            if not isinstance(process_guid, str) or not process_guid.strip():
                _reject(
                    rejections,
                    Code.E_PROCESS,
                    "process_guid must be a non-empty string",
                    source="alert",
                    record_index=index,
                    field="process_guid",
                    details={"alert_id": alert_id},
                )
                continue
            process_guid = normalize_process_guid(process_guid)

            parent_value = record.get("parent_process_guid")
            if parent_value is None or parent_value == "":
                parent_process_guid = None
            elif isinstance(parent_value, str) and parent_value.strip():
                parent_process_guid = normalize_process_guid(parent_value)
            else:
                _reject(
                    rejections,
                    Code.E_PROCESS,
                    "parent_process_guid must be null or a non-empty string",
                    source="alert",
                    record_index=index,
                    field="parent_process_guid",
                    details={"alert_id": alert_id},
                )
                continue

            payload_digest = record.get("payload_digest")
            if (
                not isinstance(payload_digest, str)
                or not _DIGEST_RE.fullmatch(payload_digest.strip())
            ):
                _reject(
                    rejections,
                    Code.E_DIGEST,
                    "payload_digest must be 64 hexadecimal characters",
                    source="alert",
                    record_index=index,
                    field="payload_digest",
                    details={"alert_id": alert_id},
                )
                continue
            payload_digest = normalize_digest(payload_digest)

            seen_ids[alert_id] = alert_ts_ms
            alerts.append(
                Alert(
                    alert_id=alert_id,
                    host_id=host_id,
                    alert_ts_ms=alert_ts_ms,
                    rule_key=rule_key,
                    raw_rule_key=raw_rule_key,
                    severity=severity,
                    process_guid=process_guid,
                    parent_process_guid=parent_process_guid,
                    payload_digest=payload_digest,
                    record_index=index,
                )
            )
    return alerts


def _parse_patch_rule(
    record: Any,
    *,
    batch_id: str,
    batch_index: int,
    local_index: int,
    rejections: list[dict],
) -> RulePatch | None:
    if not isinstance(record, dict):
        _reject(
            rejections,
            Code.E_PATCH_RULE,
            "patch rule must be an object",
            source="patch",
            record_index=batch_index,
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    raw_key = record.get("rule_key")
    if not isinstance(raw_key, str) or not raw_key.strip():
        _reject(
            rejections,
            Code.E_RULE_KEY,
            "patch rule_key must be a non-empty string",
            source="patch",
            record_index=batch_index,
            field="rule_key",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    source_key = raw_key.strip()
    canonical = record.get("canonical_rule_key", source_key)
    if not isinstance(canonical, str) or not canonical.strip():
        _reject(
            rejections,
            Code.E_RULE_KEY,
            "patch canonical_rule_key must be a non-empty string",
            source="patch",
            record_index=batch_index,
            field="canonical_rule_key",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    effective_ms = record.get("effective_ms")
    generation = record.get("generation")
    if not is_nonnegative_int(effective_ms):
        _reject(
            rejections,
            Code.E_V2_TIME,
            "patch effective_ms must be a non-negative integer",
            source="patch",
            record_index=batch_index,
            field="effective_ms",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    if not is_nonnegative_int(generation):
        _reject(
            rejections,
            Code.E_V2_GENERATION,
            "patch generation must be a non-negative integer",
            source="patch",
            record_index=batch_index,
            field="generation",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    tombstone = record.get("tombstone", False)
    if not isinstance(tombstone, bool):
        _reject(
            rejections,
            Code.E_PATCH_RULE,
            "patch tombstone must be a boolean",
            source="patch",
            record_index=batch_index,
            field="tombstone",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    dedup_window_ms = record.get("dedup_window_ms", 0 if tombstone else None)
    enabled = record.get("enabled", False if tombstone else None)
    match_strings_value = record.get("match_strings", [] if tombstone else None)
    if not is_nonnegative_int(dedup_window_ms):
        _reject(
            rejections,
            Code.E_V2_WINDOW,
            "patch dedup_window_ms must be a non-negative integer",
            source="patch",
            record_index=batch_index,
            field="dedup_window_ms",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    if not isinstance(enabled, bool):
        _reject(
            rejections,
            Code.E_PATCH_RULE,
            "patch enabled must be a boolean",
            source="patch",
            record_index=batch_index,
            field="enabled",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    match_strings = _string_list(
        match_strings_value,
        field_name="match_strings",
    )
    if match_strings is None:
        _reject(
            rejections,
            Code.E_PATCH_RULE,
            "patch match_strings must be an array of non-empty strings",
            source="patch",
            record_index=batch_index,
            field="match_strings",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    parents = _string_list(
        record.get("parent_rule_keys", []),
        field_name="parent_rule_keys",
    )
    if parents is None:
        _reject(
            rejections,
            Code.E_PATCH_RULE,
            "patch parent_rule_keys must be an array of non-empty strings",
            source="patch",
            record_index=batch_index,
            field="parent_rule_keys",
            details={"batch_id": batch_id, "local_index": local_index},
        )
        return None
    return RulePatch(
        rule_key=canonical_rule_key(canonical),
        source_rule_key=source_key,
        effective_ms=effective_ms,
        generation=generation,
        dedup_window_ms=dedup_window_ms,
        match_strings=match_strings,
        enabled=enabled,
        tombstone=tombstone,
        parent_rule_keys=tuple(
            sorted(canonical_rule_key(item) for item in parents)
        ),
        record_index=local_index,
    )


def parse_patches(
    path: Path,
    rejections: list[dict],
) -> list[PatchBatch]:
    batches: list[PatchBatch] = []
    seen_batch_ids: set[str] = set()
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        _reject(
            rejections,
            Code.E_JSON,
            "patch stream cannot be opened",
            source="patch",
            record_index=0,
            details={"path": str(path), "error": str(exc)},
        )
        return batches

    with handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                _reject(
                    rejections,
                    Code.E_JSON,
                    "blank patch batch record",
                    source="patch",
                    record_index=index,
                )
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                _reject(
                    rejections,
                    Code.E_JSON,
                    "patch batch is not valid JSON",
                    source="patch",
                    record_index=index,
                    details={"error": str(exc)},
                )
                continue
            if not isinstance(record, dict):
                _reject(
                    rejections,
                    Code.E_PATCH_RULE,
                    "patch batch must be an object",
                    source="patch",
                    record_index=index,
                )
                continue
            batch_id = record.get("batch_id")
            if not isinstance(batch_id, str) or not batch_id.strip():
                _reject(
                    rejections,
                    Code.E_PATCH_ID,
                    "batch_id must be a non-empty string",
                    source="patch",
                    record_index=index,
                    field="batch_id",
                )
                continue
            batch_id = batch_id.strip()
            if batch_id in seen_batch_ids:
                _reject(
                    rejections,
                    Code.E_PATCH_DUP,
                    "patch batch_id is not unique",
                    source="patch",
                    record_index=index,
                    field="batch_id",
                    details={"batch_id": batch_id},
                )
                continue
            logical_ts_ms = record.get("logical_ts_ms")
            if not is_nonnegative_int(logical_ts_ms):
                _reject(
                    rejections,
                    Code.E_PATCH_TIME,
                    "logical_ts_ms must be a non-negative integer",
                    source="patch",
                    record_index=index,
                    field="logical_ts_ms",
                    details={"batch_id": batch_id},
                )
                continue
            rules_value = record.get("rules")
            if not isinstance(rules_value, list) or not rules_value:
                _reject(
                    rejections,
                    Code.E_PATCH_RULE,
                    "patch rules must be a non-empty array",
                    source="patch",
                    record_index=index,
                    field="rules",
                    details={"batch_id": batch_id},
                )
                continue
            parsed_rules: list[RulePatch] = []
            batch_ok = True
            for local_index, rule_record in enumerate(rules_value, start=1):
                rule = _parse_patch_rule(
                    rule_record,
                    batch_id=batch_id,
                    batch_index=index,
                    local_index=local_index,
                    rejections=rejections,
                )
                if rule is None:
                    batch_ok = False
                    break
                parsed_rules.append(rule)
            if not batch_ok:
                continue
            seen_batch_ids.add(batch_id)
            batches.append(
                PatchBatch(
                    batch_id=batch_id,
                    logical_ts_ms=logical_ts_ms,
                    rules=tuple(parsed_rules),
                    record_index=index,
                    arrival_index=len(batches),
                )
            )
    return batches


def load_inputs(
    *,
    alerts_path: Path,
    v1_path: Path,
    v2_shards_path: Path,
    v2_meta_path: Path,
    patches_path: Path,
) -> ParsedInputs:
    rejections: list[dict] = []
    observation_clock, severity_min, severity_max, v1_versions = parse_v1(
        v1_path, rejections
    )
    v2_versions, v2_duplicates, shard_hashes = parse_v2(
        v2_shards_path, v2_meta_path, rejections
    )
    patch_batches = parse_patches(patches_path, rejections)

    allowed_keys = {
        version.rule_key for version in v1_versions + v2_versions
    }
    allowed_keys.update(
        rule.rule_key
        for batch in patch_batches
        for rule in batch.rules
    )
    alerts = parse_alerts(
        alerts_path,
        allowed_rule_keys=allowed_keys,
        severity_min=severity_min,
        severity_max=severity_max,
        rejections=rejections,
    )

    input_hashes: dict[str, str] = {}
    for name, path in (
        ("alerts.jsonl", alerts_path),
        ("rules_v1.json", v1_path),
        ("v2_meta.sqlite", v2_meta_path),
        ("patches.jsonl", patches_path),
    ):
        if path.exists() and path.is_file():
            input_hashes[name] = digest_file(path)
        else:
            input_hashes[name] = digest_bytes(
                stable_json({"missing": str(path)}).encode("utf-8")
            )
    input_hashes.update(shard_hashes)
    input_hashes = dict(sorted(input_hashes.items()))

    rejections.sort(
        key=lambda item: (
            item.get("source", ""),
            item.get("record_index", 0),
            item.get("code", ""),
            stable_json(item.get("details", {})),
        )
    )
    return ParsedInputs(
        observation_clock_ms=observation_clock,
        severity_min=severity_min,
        severity_max=severity_max,
        alerts=alerts,
        v1_versions=v1_versions,
        v2_versions=v2_versions,
        patch_batches=patch_batches,
        rejections=rejections,
        input_hashes=input_hashes,
        v2_duplicate_versions=v2_duplicates,
    )
