"""Unified parsing for v1 JSON, v2 shard/SQLite snapshots and dependencies."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import (
    E_CANONICAL_KEY,
    E_DEPENDENCY_KEY,
    E_DUP_KEY,
    E_FILE_NOT_FOUND,
    E_HASH_ALGO,
    E_HASH_HEX,
    E_METADATA_MISMATCH,
    E_NON_NEGATIVE_INT,
    E_PARSE_JSON,
    E_SIGNATURE_STATUS,
    E_TOMBSTONE_TYPE,
    E_VERSION_SYNTAX,
    SCHEMA_CLOCK,
    SCHEMA_DEPENDENCIES,
    SCHEMA_V1,
    SCHEMA_V2_MANIFEST,
    SCHEMA_V2_SHARD,
    SIGNATURE_STATUSES,
    rejection,
)
from .digests import validate_digest
from .io_utils import read_json
from .timeutils import convert_relative_seconds, is_non_negative_int, validate_clock
from .versions import canonical_key, validate_constraint_expression


class SnapshotParseError(Exception):
    def __init__(self, code: str, message: str, path: str | Path | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None


@dataclass
class ParseResult:
    snapshot_id: str
    source_format: str
    path: str | None = None
    batch_id: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    fatal_error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.fatal_error is None


def _candidate_key(record: dict[str, Any]) -> str:
    if isinstance(record.get("artifact_key"), str) and record["artifact_key"].strip():
        return record["artifact_key"].strip()
    name = record.get("name", record.get("artifact", record.get("id", "<missing>")))
    version = record.get("version", record.get("artifact_version", "<missing>"))
    return canonical_key(name if isinstance(name, str) else "<invalid-name>", version)


def _validate_key(record: dict[str, Any], snapshot_id: str, index: int):
    candidate = _candidate_key(record)
    key = None
    errors = []
    raw_key = record.get("artifact_key")
    if isinstance(raw_key, str) and "@" in raw_key:
        from .versions import validate_canonical_key

        ok, code = validate_canonical_key(raw_key)
        if ok:
            key = raw_key.strip()
        else:
            errors.append(
                rejection(
                    snapshot_id,
                    candidate,
                    code or E_CANONICAL_KEY,
                    "artifact key or version is not canonical",
                    source_index=index,
                )
            )
    else:
        name = record.get("name", record.get("artifact", record.get("id")))
        version = record.get("version", record.get("artifact_version"))
        from .versions import valid_exact_version, valid_name
        from .versions import normalize_name

        normalized_name = normalize_name(name)
        version_text = version.strip() if isinstance(version, str) else ""
        name_ok = bool(normalized_name) and valid_name(normalized_name)
        version_ok = valid_exact_version(version)
        if name_ok and version_ok:
            key = canonical_key(normalized_name, version_text)
        else:
            if not name_ok:
                errors.append(
                    rejection(
                        snapshot_id,
                        candidate,
                        E_CANONICAL_KEY,
                        "artifact name is not a canonical lower-case identifier",
                        source_index=index,
                    )
                )
            if not version_ok:
                errors.append(
                    rejection(
                        snapshot_id,
                        candidate,
                        E_VERSION_SYNTAX,
                        "artifact version is not a valid exact semantic version",
                        source_index=index,
                    )
                )
    return key, candidate, errors


def _digest_fields(record: dict[str, Any], snapshot_id: str, key: str, index: int):
    digest = record.get("digest", record.get("content_digest"))
    algorithm = record.get("algorithm", record.get("digest_algorithm"))
    validation = validate_digest(digest, algorithm)
    if validation.ok:
        return {
            "digest_declared": digest,
            "digest_normalized": validation.digest_normalized,
            "algorithm_declared": validation.algorithm_declared,
            "algorithm_normalized": validation.algorithm_normalized,
            "width_bits_expected": validation.width_bits_expected,
            "width_bits_actual": validation.width_bits_actual,
            "case_profile": validation.case_profile,
        }, []
    message = validation.message or "invalid digest"
    extra = {}
    if validation.width_bits_expected is not None:
        extra["expected_width_bits"] = validation.width_bits_expected
    if validation.width_bits_actual is not None:
        extra["actual_width_bits"] = validation.width_bits_actual
    return {}, [
        rejection(
            snapshot_id,
            key,
            validation.code or E_HASH_HEX,
            message,
            source_index=index,
            algorithm_declared=validation.algorithm_declared,
            **extra,
        )
    ]


def _common_validations(record, snapshot_id, key, index):
    errors = []
    signature_status = record.get("signature_status", record.get("signature"))
    if signature_status not in SIGNATURE_STATUSES:
        errors.append(
            rejection(
                snapshot_id,
                key,
                E_SIGNATURE_STATUS,
                f"signature_status must be one of {sorted(SIGNATURE_STATUSES)}",
                source_index=index,
                declared_status=signature_status,
            )
        )

    generation = record.get("digest_generation", record.get("generation"))
    if generation is not None and not is_non_negative_int(generation):
        errors.append(
            rejection(
                snapshot_id,
                key,
                E_NON_NEGATIVE_INT,
                "digest_generation must be a non-negative integer",
                source_index=index,
                field="digest_generation",
                value=generation,
            )
        )
    return errors


def load_clock(path: str | Path) -> dict[str, Any]:
    clock_path = Path(path)
    if not clock_path.exists():
        raise SnapshotParseError(E_FILE_NOT_FOUND, f"clock not found: {path}", path)
    try:
        clock = read_json(clock_path)
    except json.JSONDecodeError as exc:
        raise SnapshotParseError(E_PARSE_JSON, str(exc), path) from exc
    errors = validate_clock(clock)
    if errors:
        raise SnapshotParseError(
            errors[0]["code"], f"invalid clock declaration: {errors[0]['message']}", path
        )
    clock.setdefault("schema", SCHEMA_CLOCK)
    return clock


def parse_v1(payload: dict[str, Any], snapshot_id: str, clock: dict[str, Any]):
    result = ParseResult(snapshot_id, "v1", batch_id=payload.get("batch_id"))
    records = payload.get("artifacts", [])
    if not isinstance(records, list):
        result.fatal_error = {
            "code": E_PARSE_JSON,
            "message": "v1 artifacts field must be a list",
        }
        return result

    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            result.rejections.append(
                rejection(snapshot_id, "<non-object-record>", E_PARSE_JSON, "record must be an object", source_index=index)
            )
            continue
        key, candidate, key_errors = _validate_key(record, snapshot_id, index)
        errors = list(key_errors)
        lookup_key = key or candidate

        digest_fields, digest_errors = _digest_fields(record, snapshot_id, lookup_key, index)
        errors.extend(digest_errors)
        errors.extend(_common_validations(record, snapshot_id, lookup_key, index))

        relative_seconds = record.get(
            "published_relative_seconds",
            record.get("relative_published_seconds", record.get("release_seconds")),
        )
        conversion = None
        published_at_ms = None
        if not is_non_negative_int(relative_seconds):
            errors.append(
                rejection(
                    snapshot_id,
                    lookup_key,
                    E_NON_NEGATIVE_INT,
                    "published_relative_seconds must be a non-negative integer",
                    source_index=index,
                    field="published_relative_seconds",
                    value=relative_seconds,
                )
            )
        else:
            conversion = convert_relative_seconds(relative_seconds, clock)
            published_at_ms = conversion["absolute_ms"]

        if conversion is not None:
            for error in errors:
                error.setdefault("time_conversion", conversion)
                error.setdefault("error_bound_ms", conversion["error_bound_ms"])

        if key is not None:
            if key in seen:
                errors.append(
                    rejection(snapshot_id, key, E_DUP_KEY, "duplicate canonical artifact key", source_index=index)
                )
            else:
                seen.add(key)

        result.rejections.extend(errors)
        if errors:
            continue

        result.claims.append(
            {
                "artifact_key": key,
                "name": key.rsplit("@", 1)[0],
                "version": key.rsplit("@", 1)[1],
                "snapshot_id": snapshot_id,
                "batch_id": payload.get("batch_id"),
                "source_format": "v1",
                "source_index": index,
                "signature_status": record["signature_status"],
                "published_at_ms": published_at_ms,
                "time_conversion": conversion,
                "relative_seconds": relative_seconds,
                "digest_generation": record.get("digest_generation", record.get("generation", 1)),
                "tombstoned": False,
                "content_path": record.get("content_path"),
                **digest_fields,
            }
        )
    return result


def _read_v2_metadata(db_path: Path) -> dict[str, dict[str, Any]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT artifact_key, published_at_ms, digest_generation, algorithm, tombstoned "
            "FROM artifact_metadata"
        ).fetchall()
    metadata = {}
    for row in rows:
        item = dict(row)
        item["tombstoned"] = bool(item["tombstoned"])
        metadata[item["artifact_key"]] = item
    return metadata


def parse_v2_manifest(manifest_path: str | Path, clock: dict[str, Any] | None = None):
    path = Path(manifest_path)
    try:
        manifest = read_json(path)
    except FileNotFoundError as exc:
        raise SnapshotParseError(E_FILE_NOT_FOUND, f"v2 manifest not found: {path}", path) from exc
    except json.JSONDecodeError as exc:
        raise SnapshotParseError(E_PARSE_JSON, str(exc), path) from exc

    snapshot_id = manifest.get("snapshot_id", path.stem) if isinstance(manifest, dict) else path.stem
    result = ParseResult(snapshot_id, "v2", str(path), batch_id=manifest.get("batch_id"))
    if not isinstance(manifest, dict) or manifest.get("schema") not in {SCHEMA_V2_MANIFEST, None}:
        result.fatal_error = {"code": E_PARSE_JSON, "message": "unsupported v2 manifest schema"}
        return result

    base_dir = path.parent
    shard_names = manifest.get("shards", [])
    if not isinstance(shard_names, list):
        result.fatal_error = {"code": E_PARSE_JSON, "message": "v2 shards must be a list"}
        return result

    records = []
    for shard_name in shard_names:
        shard_path = base_dir / str(shard_name)
        try:
            shard = read_json(shard_path)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            result.rejections.append(
                rejection(snapshot_id, "<shard>", E_PARSE_JSON, f"cannot parse shard {shard_name}: {exc}")
            )
            continue
        shard_id = shard.get("shard_id", shard_name) if isinstance(shard, dict) else shard_name
        artifacts = shard.get("artifacts", []) if isinstance(shard, dict) else []
        if not isinstance(artifacts, list):
            result.rejections.append(
                rejection(snapshot_id, str(shard_id), E_PARSE_JSON, "shard artifacts must be a list")
            )
            continue
        for offset, record in enumerate(artifacts):
            if isinstance(record, dict):
                record = dict(record)
                record.setdefault("_shard_id", shard_id)
                record.setdefault("_source_index", offset)
            records.append(record)

    db_name = manifest.get("metadata_db", "metadata.sqlite3")
    db_path = base_dir / str(db_name)
    try:
        metadata = _read_v2_metadata(db_path)
    except (sqlite3.Error, OSError) as exc:
        result.fatal_error = {"code": E_PARSE_JSON, "message": f"cannot read v2 SQLite metadata: {exc}"}
        return result

    seen = set()
    json_keys_seen = set()
    for global_index, record in enumerate(records):
        if not isinstance(record, dict):
            result.rejections.append(
                rejection(snapshot_id, "<non-object-record>", E_PARSE_JSON, "record must be an object", source_index=global_index)
            )
            continue
        index = record.get("_source_index", global_index)
        key, candidate, key_errors = _validate_key(record, snapshot_id, index)
        errors = list(key_errors)
        lookup_key = key or candidate
        digest_fields, digest_errors = _digest_fields(record, snapshot_id, lookup_key, index)
        errors.extend(digest_errors)
        errors.extend(_common_validations(record, snapshot_id, lookup_key, index))

        published_at_ms = record.get("published_at_ms", record.get("published_at"))
        if not is_non_negative_int(published_at_ms):
            errors.append(
                rejection(
                    snapshot_id,
                    lookup_key,
                    E_NON_NEGATIVE_INT,
                    "published_at_ms must be a non-negative integer",
                    source_index=index,
                    field="published_at_ms",
                    value=published_at_ms,
                )
            )

        tombstoned = record.get("tombstoned", record.get("tombstone", False))
        if not isinstance(tombstoned, bool):
            errors.append(
                rejection(
                    snapshot_id,
                    lookup_key,
                    E_TOMBSTONE_TYPE,
                    "tombstoned must be a boolean",
                    source_index=index,
                    value=tombstoned,
                )
            )

        metadata_row = metadata.get(lookup_key) if key else None
        if key is not None:
            json_keys_seen.add(key)
            if key in seen:
                errors.append(rejection(snapshot_id, key, E_DUP_KEY, "duplicate canonical artifact key across shards", source_index=index))
            else:
                seen.add(key)
            if metadata_row is None:
                errors.append(
                    rejection(snapshot_id, key, E_METADATA_MISMATCH, "artifact is missing from SQLite metadata", source_index=index)
                )
            else:
                comparisons = {
                    "published_at_ms": published_at_ms,
                    "digest_generation": record.get("digest_generation", record.get("generation")),
                    "algorithm": record.get("algorithm", record.get("digest_algorithm")),
                    "tombstoned": tombstoned,
                }
                for field_name, json_value in comparisons.items():
                    db_value = metadata_row.get(field_name)
                    if field_name == "algorithm":
                        from .digests import normalize_algorithm

                        json_norm, _ = normalize_algorithm(json_value)
                        db_norm, _ = normalize_algorithm(db_value)
                        equal = json_norm == db_norm
                    elif field_name == "tombstoned":
                        equal = bool(json_value) == bool(db_value)
                    else:
                        equal = json_value == db_value
                    if not equal:
                        errors.append(
                            rejection(
                                snapshot_id,
                                key,
                                E_METADATA_MISMATCH,
                                f"JSON shard and SQLite field {field_name} disagree",
                                source_index=index,
                                field=field_name,
                                json_value=json_value,
                                sqlite_value=db_value,
                            )
                        )

        result.rejections.extend(errors)
        if errors:
            continue

        result.claims.append(
            {
                "artifact_key": key,
                "name": key.rsplit("@", 1)[0],
                "version": key.rsplit("@", 1)[1],
                "snapshot_id": snapshot_id,
                "batch_id": manifest.get("batch_id"),
                "source_format": "v2",
                "source_index": index,
                "shard_id": record.get("_shard_id"),
                "signature_status": record["signature_status"],
                "published_at_ms": published_at_ms,
                "time_conversion": None,
                "relative_seconds": None,
                "digest_generation": record.get("digest_generation", record.get("generation")),
                "tombstoned": tombstoned,
                "content_path": record.get("content_path"),
                **digest_fields,
            }
        )

    for db_key, row in metadata.items():
        if db_key not in json_keys_seen:
            result.rejections.append(
                rejection(
                    snapshot_id,
                    db_key,
                    E_METADATA_MISMATCH,
                    "SQLite metadata key is absent from JSON shards",
                    sqlite_value=row,
                )
            )

    return result


def load_snapshot(path: str | Path, clock: dict[str, Any] | None = None) -> ParseResult:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        raise SnapshotParseError(E_FILE_NOT_FOUND, f"snapshot not found: {path}", path)

    if snapshot_path.is_dir():
        manifest = snapshot_path / "v2_manifest.json"
        if not manifest.exists():
            manifests = list(snapshot_path.glob("*manifest*.json"))
            if not manifests:
                raise SnapshotParseError(E_FILE_NOT_FOUND, "directory has no v2 manifest", path)
            manifest = manifests[0]
        return parse_v2_manifest(manifest, clock)

    try:
        payload = read_json(snapshot_path)
    except json.JSONDecodeError as exc:
        raise SnapshotParseError(E_PARSE_JSON, str(exc), snapshot_path) from exc

    if not isinstance(payload, dict):
        raise SnapshotParseError(E_PARSE_JSON, "snapshot root must be a JSON object", snapshot_path)

    schema = payload.get("schema", "")
    if schema == SCHEMA_V2_MANIFEST or "shards" in payload and "metadata_db" in payload:
        return parse_v2_manifest(snapshot_path, clock)
    if clock is None:
        raise SnapshotParseError("E_CLOCK_REQUIRED", "v1 snapshot conversion requires a loaded observation clock", snapshot_path)
    snapshot_id = payload.get("snapshot_id", snapshot_path.stem)
    result = parse_v1(payload, snapshot_id, clock)
    result.path = str(snapshot_path)
    return result


def load_dependencies(path: str | Path) -> dict[str, Any]:
    dependency_path = Path(path)
    try:
        payload = read_json(dependency_path)
    except FileNotFoundError as exc:
        raise SnapshotParseError(E_FILE_NOT_FOUND, f"dependencies not found: {path}", path) from exc
    except json.JSONDecodeError as exc:
        raise SnapshotParseError(E_PARSE_JSON, str(exc), path) from exc

    snapshot_id = payload.get("snapshot_id", dependency_path.stem)
    result = ParseResult(snapshot_id, "dependencies", str(dependency_path))
    raw_edges = payload.get("dependencies", payload.get("edges", []))
    edges = []
    if not isinstance(raw_edges, list):
        result.fatal_error = {"code": E_PARSE_JSON, "message": "dependencies must be a list"}
        return {"snapshot_id": snapshot_id, "edges": [], "rejections": result.rejections, "fatal_error": result.fatal_error}

    seen_edges = set()
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            result.rejections.append(rejection(snapshot_id, "<edge>", E_DEPENDENCY_KEY, "dependency edge must be an object", source_index=index))
            continue
        dependent = item.get("dependent_key", item.get("dependent", item.get("source_key")))
        dependency = item.get("dependency_key", item.get("dependency", item.get("target_key")))
        constraint = item.get("version_constraint", item.get("constraint", "*"))
        edge_errors = []
        for label, value in (("dependent_key", dependent), ("dependency_key", dependency)):
            from .versions import validate_canonical_key

            ok, code = validate_canonical_key(value)
            if not ok:
                edge_errors.append(
                    rejection(snapshot_id, str(value), code or E_DEPENDENCY_KEY, f"{label} is invalid", source_index=index)
                )
        constraint_ok, constraint_code = validate_constraint_expression(constraint)
        if not constraint_ok:
            edge_errors.append(
                rejection(snapshot_id, str(dependency), constraint_code, "invalid version constraint expression", source_index=index)
            )
        result.rejections.extend(edge_errors)
        if edge_errors:
            continue
        edge = {
            "dependent_key": dependent.strip(),
            "dependency_key": dependency.strip(),
            "version_constraint": constraint,
            "edge_id": f"{dependent.strip()}->{dependency.strip()}",
        }
        if edge["edge_id"] not in seen_edges:
            seen_edges.add(edge["edge_id"])
            edges.append(edge)
    return {
        "snapshot_id": snapshot_id,
        "schema": payload.get("schema", SCHEMA_DEPENDENCIES),
        "edges": edges,
        "rejections": result.rejections,
        "fatal_error": None,
    }


def load_revocation_batch(path: str | Path) -> dict[str, Any]:
    batch_path = Path(path)
    try:
        payload = read_json(batch_path)
    except FileNotFoundError as exc:
        raise SnapshotParseError(E_FILE_NOT_FOUND, f"revocation batch not found: {path}", path) from exc
    except json.JSONDecodeError as exc:
        raise SnapshotParseError(E_PARSE_JSON, str(exc), path) from exc
    if not isinstance(payload, dict):
        raise SnapshotParseError(E_PARSE_JSON, "revocation batch must be an object", batch_path)
    batch_id = payload.get("batch_id", batch_path.stem)
    entries = payload.get("revocations", [])
    if not isinstance(entries, list):
        raise SnapshotParseError(E_PARSE_JSON, "revocations must be a list", batch_path)
    clean = []
    rejections = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            rejections.append(rejection(batch_id, "<revocation>", E_PARSE_JSON, "revocation must be an object", source_index=index))
            continue
        key = entry.get("artifact_key")
        from .versions import validate_canonical_key

        ok, code = validate_canonical_key(key)
        if not ok:
            rejections.append(rejection(batch_id, str(key), code or E_CANONICAL_KEY, "invalid revocation key", source_index=index))
            continue
        revoked_at_ms = entry.get("revoked_at_ms")
        if revoked_at_ms is not None and not is_non_negative_int(revoked_at_ms):
            rejections.append(rejection(batch_id, key, E_NON_NEGATIVE_INT, "revoked_at_ms must be non-negative integer", source_index=index))
            continue
        clean.append(
            {
                "artifact_key": key.strip(),
                "reason": entry.get("reason", "unspecified"),
                "revoked_at_ms": revoked_at_ms,
                "batch_id": batch_id,
            }
        )
    return {"batch_id": batch_id, "revocations": clean, "rejections": rejections}
