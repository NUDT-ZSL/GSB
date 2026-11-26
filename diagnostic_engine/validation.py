from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, read_json, sha256_hex
from .clock import ClockProfile
from .errors import DiagnosticError, ErrorCode


FIXTURE_LAYOUT = {
    "clock_profile": "clock_profile.json",
    "manifest": "manifest/object_manifest.json",
    "events": "events/write_events.json",
    "v1": "snapshots/v1/metadata-v1.json",
    "v2_manifest": "snapshots/v2/v2_manifest.json",
    "v2_dir": "snapshots/v2",
    "v2_db": "snapshots/v2/metadata.sqlite",
    "injections": "injections/fault_injections.json",
}

OBJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SIGNATURE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

POLICIES = {"metadata_first", "write_log", "data_first", "direct_reversed"}
STAGES = {
    "plan",
    "data_staging",
    "data_write",
    "data_rename",
    "metadata_commit",
    "log_append",
    "log_persist",
    "log_replay",
    "data_commit",
}
FAULT_TYPES = {
    "force_kill",
    "write_interrupt",
    "generation_rollback",
    "log_lost",
}


def valid_object_key(key: Any) -> bool:
    if not isinstance(key, str) or not OBJECT_KEY_RE.fullmatch(key):
        return False
    if "//" in key or key.endswith("/"):
        return False
    segments = key.split("/")
    return all(segment not in {"", ".", ".."} for segment in segments)


def validate_signature(value: Any, *, field_name: str = "expected_signature") -> str:
    if not isinstance(value, str) or not SIGNATURE_RE.fullmatch(value):
        raise DiagnosticError(
            ErrorCode.SIGNATURE_INVALID,
            f"{field_name} must match sha256:<64 lowercase hex chars>",
            details={"field": field_name, "value": value},
        )
    return value


def _load_json_file(path: Path, code: ErrorCode, label: str) -> Any:
    if not path.is_file():
        raise DiagnosticError(
            ErrorCode.FIXTURES_NOT_FOUND,
            f"{label} does not exist",
            details={"path": str(path)},
        )
    try:
        return read_json(path)
    except Exception as exc:  # JSONDecodeError/OSError are both input errors.
        raise DiagnosticError(
            code,
            f"{label} is not valid JSON",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def parse_v1_flat_value(value: Any, *, object_key: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise DiagnosticError(
            ErrorCode.V1_FLAT_VALUE_INVALID,
            "v1 flat record must be a traceable string",
            object_key=object_key,
            details={"value": value},
        )
    fields: dict[str, str] = {}
    for part in value.split("|"):
        if "=" not in part:
            raise DiagnosticError(
                ErrorCode.V1_FLAT_VALUE_INVALID,
                "v1 flat record must contain key=value fields separated by |",
                object_key=object_key,
                details={"value": value},
            )
        name, raw = part.split("=", 1)
        fields[name] = raw
    required = {"version", "generation", "clock", "signature"}
    missing = sorted(required - set(fields))
    if missing:
        raise DiagnosticError(
            ErrorCode.V1_FLAT_VALUE_INVALID,
            "v1 flat record is missing trace fields",
            object_key=object_key,
            details={"missing": missing, "value": value},
        )
    try:
        version = int(fields["version"])
        generation = int(fields["generation"])
        clock = int(fields["clock"])
    except ValueError as exc:
        raise DiagnosticError(
            ErrorCode.V1_FLAT_VALUE_INVALID,
            "v1 version/generation/clock must be integers",
            object_key=object_key,
            details={"value": value},
        ) from exc
    if version <= 0 or generation <= 0 or clock < 0:
        raise DiagnosticError(
            ErrorCode.V1_FLAT_VALUE_INVALID,
            "v1 trace has invalid version, generation, or clock",
            object_key=object_key,
            logical_clock=clock,
            details={"version": version, "generation": generation},
        )
    validate_signature(fields["signature"], field_name=f"v1:{object_key}:signature")
    return {
        "key": object_key,
        "content_version": version,
        "cache_generation": generation,
        "logical_clock": clock,
        "signature": fields["signature"],
        "trace": value,
    }


def shard_hash_payload(shard: dict[str, Any]) -> dict[str, Any]:
    return {
        "shard_id": shard.get("shard_id"),
        "index": shard.get("index"),
        "generation": shard.get("generation"),
        "declared_clock": shard.get("declared_clock"),
        "records": shard.get("records"),
    }


@dataclass
class FixtureBundle:
    root: Path
    seed: int
    clock_profile: ClockProfile
    clock_profile_dict: dict[str, Any]
    manifest: dict[str, Any]
    objects: list[dict[str, Any]]
    object_by_key: dict[str, dict[str, Any]]
    events_document: dict[str, Any]
    events: list[dict[str, Any]]
    event_by_seq: dict[int, dict[str, Any]]
    v1: dict[str, Any]
    v1_records: dict[str, dict[str, Any]]
    v2_manifest: dict[str, Any]
    v2_shards: list[dict[str, Any]]
    v2_records: dict[str, dict[str, Any]]
    v2_rows: list[dict[str, Any]]
    injections_document: dict[str, Any]
    injections: list[dict[str, Any]]
    injection_by_id: dict[str, dict[str, Any]]
    paths: dict[str, Path] = field(default_factory=dict)

    def injection_case(self, injection_id: str) -> dict[str, Any]:
        if injection_id not in self.injection_by_id:
            raise DiagnosticError(
                ErrorCode.INJECTION_CASE_UNKNOWN,
                "injection case is not present in fixed seed manifest",
                details={
                    "injection_id": injection_id,
                    "available": sorted(self.injection_by_id),
                },
            )
        return self.injection_by_id[injection_id]


def _validate_manifest(document: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(document, dict):
        raise DiagnosticError(ErrorCode.MANIFEST_INVALID, "manifest must be a JSON object")
    raw_objects = document.get("objects")
    seed = document.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise DiagnosticError(ErrorCode.MANIFEST_INVALID, "manifest seed must be an integer")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise DiagnosticError(ErrorCode.MANIFEST_INVALID, "objects must be a non-empty list")

    seen: set[str] = set()
    objects: list[dict[str, Any]] = []
    for index, item in enumerate(raw_objects):
        if not isinstance(item, dict):
            raise DiagnosticError(
                ErrorCode.MANIFEST_INVALID,
                "each object manifest entry must be an object",
                details={"index": index},
            )
        key = item.get("key")
        if not valid_object_key(key):
            raise DiagnosticError(
                ErrorCode.OBJECT_KEY_INVALID,
                "object key has an illegal format",
                object_key=key if isinstance(key, str) else None,
                details={"index": index, "key": key},
            )
        if key in seen:
            raise DiagnosticError(
                ErrorCode.OBJECT_KEY_DUPLICATE,
                "object keys must be unique",
                object_key=key,
                details={"index": index},
            )
        seen.add(key)

        version = item.get("content_version")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise DiagnosticError(
                ErrorCode.CONTENT_VERSION_INVALID,
                "content_version must be a positive integer",
                object_key=key,
                details={"content_version": version},
            )
        size = item.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DiagnosticError(
                ErrorCode.MANIFEST_INVALID,
                "size_bytes must be a non-negative integer",
                object_key=key,
                details={"size_bytes": size},
            )
        validate_signature(item.get("expected_signature"))
        objects.append(dict(item))
    return objects, seed


def _validate_events(document: Any, object_by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise DiagnosticError(
            ErrorCode.EVENT_STREAM_INVALID,
            "write event stream must contain an events list",
        )
    events = document["events"]
    previous_seq: int | None = None
    result: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise DiagnosticError(
                ErrorCode.EVENT_STREAM_INVALID,
                "each write event must be an object",
                details={"index": index},
            )
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise DiagnosticError(
                ErrorCode.EVENT_STREAM_INVALID,
                "event seq must be a positive integer",
                details={"index": index, "seq": seq},
            )
        if previous_seq is not None and seq <= previous_seq:
            raise DiagnosticError(
                ErrorCode.EVENT_SEQ_NOT_STRICT,
                "event seq must be strictly increasing",
                details={"previous_seq": previous_seq, "seq": seq},
            )
        previous_seq = seq

        clock = event.get("logical_clock")
        if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
            raise DiagnosticError(
                ErrorCode.LOGICAL_CLOCK_NEGATIVE,
                "logical_clock must be a non-negative integer",
                details={"seq": seq, "logical_clock": clock},
            )
        key = event.get("key")
        if not valid_object_key(key):
            raise DiagnosticError(
                ErrorCode.OBJECT_KEY_INVALID,
                "event object key has an illegal format",
                object_key=key if isinstance(key, str) else None,
                details={"seq": seq},
            )
        if key not in object_by_key:
            raise DiagnosticError(
                ErrorCode.EVENT_KEY_UNDECLARED,
                "event key is not present in object manifest",
                object_key=key,
                details={"seq": seq},
            )
        op = event.get("op")
        if op not in {"WRITE", "UPDATE", "DELETE"}:
            raise DiagnosticError(
                ErrorCode.EVENT_OP_INVALID,
                "op must be WRITE, UPDATE, or DELETE",
                object_key=key,
                logical_clock=clock,
                details={"seq": seq, "op": op},
            )
        version = event.get("content_version")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise DiagnosticError(
                ErrorCode.CONTENT_VERSION_INVALID,
                "event content_version must be a positive integer",
                object_key=key,
                logical_clock=clock,
                details={"seq": seq, "content_version": version},
            )
        payload_size = event.get("payload_size")
        if not isinstance(payload_size, int) or isinstance(payload_size, bool) or payload_size < 0:
            raise DiagnosticError(
                ErrorCode.EVENT_STREAM_INVALID,
                "payload_size must be a non-negative integer",
                object_key=key,
                logical_clock=clock,
                details={"seq": seq, "payload_size": payload_size},
            )
        validate_signature(event.get("expected_signature"))
        result.append(dict(event))
    if not result:
        raise DiagnosticError(ErrorCode.EVENT_STREAM_INVALID, "events must not be empty")
    return result


def _validate_v1(document: Any, object_by_key: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(document, dict) or document.get("format") != "metadata-snapshot-v1":
        raise DiagnosticError(ErrorCode.V1_SNAPSHOT_INVALID, "not a metadata-snapshot-v1 document")
    clock = document.get("declared_clock")
    if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
        raise DiagnosticError(
            ErrorCode.LOGICAL_CLOCK_NEGATIVE,
            "v1 declared_clock must be a non-negative integer",
        )
    generation = document.get("cache_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise DiagnosticError(
            ErrorCode.V1_SNAPSHOT_INVALID,
            "v1 cache_generation must be a positive integer",
        )
    # Validation performs decimal parsing and rejects negative values.
    from .clock import convert_relative_seconds

    convert_relative_seconds(document.get("effective_relative_seconds"))
    raw_records = document.get("flat_records")
    if not isinstance(raw_records, dict) or not raw_records:
        raise DiagnosticError(ErrorCode.V1_SNAPSHOT_INVALID, "v1 flat_records must be a non-empty object")
    records: dict[str, dict[str, Any]] = {}
    for key, value in sorted(raw_records.items()):
        if not valid_object_key(key):
            raise DiagnosticError(ErrorCode.OBJECT_KEY_INVALID, "v1 key is illegal", object_key=key)
        if key not in object_by_key:
            raise DiagnosticError(
                ErrorCode.EVENT_KEY_UNDECLARED,
                "v1 key is not declared in object manifest",
                object_key=key,
            )
        record = parse_v1_flat_value(value, object_key=key)
        records[key] = record
    return records


def _validate_v2(
    v2_manifest: Any,
    shards: list[dict[str, Any]],
    db_path: Path,
    object_by_key: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(v2_manifest, dict) or v2_manifest.get("format") != "metadata-snapshot-v2-manifest":
        raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "invalid v2 manifest")
    raw_shards = v2_manifest.get("shards")
    if not isinstance(raw_shards, list) or len(raw_shards) != len(shards):
        raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 manifest shard list does not match files")

    records: dict[str, dict[str, Any]] = {}
    previous_generation = 0
    for expected_index, shard in enumerate(shards):
        if not isinstance(shard, dict) or shard.get("format") != "metadata-snapshot-v2-shard":
            raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "invalid v2 shard", details={"index": expected_index})
        index = shard.get("index")
        if index != expected_index:
            raise DiagnosticError(
                ErrorCode.V2_SNAPSHOT_INVALID,
                "v2 shard indices must be contiguous",
                details={"expected": expected_index, "actual": index},
            )
        generation = shard.get("generation")
        clock = shard.get("declared_clock")
        if not isinstance(generation, int) or generation <= 0:
            raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 generation must be positive", details={"index": index})
        if generation < previous_generation:
            raise DiagnosticError(
                ErrorCode.V2_GENERATION_NOT_MONOTONIC,
                "v2 shard cache generations must be non-decreasing",
                details={"index": index, "previous_generation": previous_generation, "generation": generation},
            )
        previous_generation = generation
        if not isinstance(clock, int) or clock < 0:
            raise DiagnosticError(ErrorCode.LOGICAL_CLOCK_NEGATIVE, "v2 declared_clock must be non-negative", details={"index": index})
        expected_hash = shard.get("body_sha256")
        actual_hash = sha256_hex(canonical_bytes(shard_hash_payload(shard)))
        if expected_hash != actual_hash:
            raise DiagnosticError(
                ErrorCode.V2_SHARD_HASH_MISMATCH,
                "v2 shard body hash mismatch",
                details={"index": index, "expected": expected_hash, "actual": actual_hash},
            )
        shard_records = shard.get("records")
        if not isinstance(shard_records, list):
            raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 shard records must be a list", details={"index": index})
        for record in shard_records:
            if not isinstance(record, dict):
                raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 record must be an object")
            key = record.get("key")
            if not valid_object_key(key) or key not in object_by_key:
                raise DiagnosticError(ErrorCode.OBJECT_KEY_INVALID, "v2 key is illegal or undeclared", object_key=key)
            version = record.get("content_version")
            gen = record.get("generation")
            absolute_ms = record.get("absolute_clock_ms")
            if not isinstance(version, int) or version <= 0:
                raise DiagnosticError(ErrorCode.CONTENT_VERSION_INVALID, "v2 content_version must be positive", object_key=key)
            if not isinstance(gen, int) or gen <= 0:
                raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 record generation must be positive", object_key=key)
            if not isinstance(absolute_ms, int) or absolute_ms < 0:
                raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 absolute_clock_ms must be non-negative", object_key=key)
            validate_signature(record.get("signature"))
            if key in records:
                raise DiagnosticError(ErrorCode.V2_SNAPSHOT_INVALID, "v2 contains duplicate key across shards", object_key=key)
            normalized = dict(record)
            normalized.setdefault("tombstone", False)
            records[key] = normalized

    rows = _read_and_validate_v2_db(db_path, records)
    return records, rows


def _read_and_validate_v2_db(db_path: Path, shard_records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not db_path.is_file():
        raise DiagnosticError(ErrorCode.V2_DATABASE_INVALID, "v2 SQLite metadata database is missing", details={"path": str(db_path)})
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if {"metadata", "metadata_history"} - names:
                raise DiagnosticError(
                    ErrorCode.V2_DATABASE_INVALID,
                    "v2 database must contain metadata and metadata_history tables",
                    details={"tables": sorted(names)},
                )
            rows = [dict(row) for row in connection.execute(
                "SELECT key, content_version, signature, absolute_clock_ms, generation, tombstone "
                "FROM metadata ORDER BY key"
            )]
            history = [dict(row) for row in connection.execute(
                "SELECT history_id, key, content_version, generation, absolute_clock_ms, tombstone "
                "FROM metadata_history ORDER BY key, history_id"
            )]
    except DiagnosticError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DiagnosticError(ErrorCode.V2_DATABASE_INVALID, "cannot read v2 SQLite database", details={"reason": str(exc)}) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    db_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("key")
        if not valid_object_key(key):
            raise DiagnosticError(ErrorCode.V2_DATABASE_INVALID, "v2 database has illegal key", details={"key": key})
        row["tombstone"] = bool(row["tombstone"])
        db_by_key[key] = row

    if set(db_by_key) != set(shard_records):
        raise DiagnosticError(
            ErrorCode.V2_DATABASE_INVALID,
            "v2 SQLite keys do not match shard records",
            details={
                "db_only": sorted(set(db_by_key) - set(shard_records)),
                "shard_only": sorted(set(shard_records) - set(db_by_key)),
            },
        )
    for key, row in db_by_key.items():
        shard = shard_records[key]
        for field_name in ("content_version", "signature", "absolute_clock_ms", "generation"):
            if row.get(field_name) != shard.get(field_name):
                raise DiagnosticError(
                    ErrorCode.V2_DATABASE_INVALID,
                    "v2 SQLite field does not match shard",
                    object_key=key,
                    details={"field": field_name, "db": row.get(field_name), "shard": shard.get(field_name)},
                )
        if bool(row.get("tombstone")) != bool(shard.get("tombstone")):
            raise DiagnosticError(
                ErrorCode.V2_DATABASE_INVALID,
                "v2 SQLite tombstone does not match shard",
                object_key=key,
            )

    history_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in history:
        history_by_key.setdefault(item["key"], []).append(item)
    for key, items in history_by_key.items():
        previous = 0
        for item in items:
            generation = item["generation"]
            if not isinstance(generation, int) or generation < previous:
                raise DiagnosticError(
                    ErrorCode.V2_GENERATION_NOT_MONOTONIC,
                    "v2 metadata_history generation must be non-decreasing",
                    object_key=key,
                    details={"history_id": item.get("history_id"), "previous": previous, "generation": generation},
                )
            previous = generation
    return rows


def _validate_injections(document: Any, event_by_seq: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise DiagnosticError(ErrorCode.INJECTION_MANIFEST_INVALID, "injection manifest must contain cases list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in document["cases"]:
        if not isinstance(case, dict):
            raise DiagnosticError(ErrorCode.INJECTION_MANIFEST_INVALID, "injection case must be an object")
        injection_id = case.get("injection_id")
        if not isinstance(injection_id, str) or not injection_id or injection_id in seen:
            raise DiagnosticError(ErrorCode.INJECTION_MANIFEST_INVALID, "injection_id must be a unique non-empty string")
        seen.add(injection_id)
        seq = case.get("event_seq")
        if seq not in event_by_seq:
            raise DiagnosticError(
                ErrorCode.INJECTION_MANIFEST_INVALID,
                "injection event_seq must reference an existing event",
                injection_id=injection_id,
                details={"event_seq": seq},
            )
        policy = case.get("policy")
        if policy not in POLICIES:
            raise DiagnosticError(
                ErrorCode.INJECTION_MANIFEST_INVALID,
                "injection policy is invalid",
                injection_id=injection_id,
                details={"policy": policy, "allowed": sorted(POLICIES)},
            )
        stage = case.get("stage")
        if stage not in STAGES:
            raise DiagnosticError(
                ErrorCode.INJECTION_MANIFEST_INVALID,
                "injection stage is invalid",
                injection_id=injection_id,
                details={"stage": stage, "allowed": sorted(STAGES)},
            )
        fault_type = case.get("fault_type")
        if fault_type not in FAULT_TYPES:
            raise DiagnosticError(
                ErrorCode.INJECTION_MANIFEST_INVALID,
                "fault_type is invalid",
                injection_id=injection_id,
                details={"fault_type": fault_type, "allowed": sorted(FAULT_TYPES)},
            )
        normalized = dict(case)
        normalized.setdefault("hard", False)
        normalized.setdefault("after_persist", True)
        result.append(normalized)
    return result


def load_fixtures(root: str | Path) -> FixtureBundle:
    root_path = Path(root)
    if not root_path.is_dir():
        raise DiagnosticError(
            ErrorCode.FIXTURES_NOT_FOUND,
            "fixture directory does not exist; run the fixed seed generator first",
            details={"root": str(root_path)},
        )
    paths = {name: root_path / relative for name, relative in FIXTURE_LAYOUT.items()}
    for required in ("manifest", "events", "v1", "v2_manifest", "v2_db", "injections"):
        if not paths[required].exists():
            raise DiagnosticError(
                ErrorCode.FIXTURES_NOT_FOUND,
                "required fixture file is missing",
                details={"name": required, "path": str(paths[required])},
            )

    clock_document = read_json(paths["clock_profile"]) if paths["clock_profile"].exists() else {}
    clock_profile = ClockProfile.from_dict(clock_document)
    manifest = _load_json_file(paths["manifest"], ErrorCode.MANIFEST_INVALID, "object manifest")
    objects, seed = _validate_manifest(manifest)
    object_by_key = {item["key"]: item for item in objects}

    events_document = _load_json_file(paths["events"], ErrorCode.EVENT_STREAM_INVALID, "write events")
    events = _validate_events(events_document, object_by_key)
    event_by_seq = {event["seq"]: event for event in events}

    v1 = _load_json_file(paths["v1"], ErrorCode.V1_SNAPSHOT_INVALID, "v1 snapshot")
    v1_records = _validate_v1(v1, object_by_key)

    v2_manifest = _load_json_file(paths["v2_manifest"], ErrorCode.V2_SNAPSHOT_INVALID, "v2 manifest")
    shard_files = sorted((paths["v2_dir"] / "shards").glob("shard-*.json"))
    shards = [_load_json_file(path, ErrorCode.V2_SNAPSHOT_INVALID, f"v2 shard {path.name}") for path in shard_files]
    v2_records, v2_rows = _validate_v2(v2_manifest, shards, paths["v2_db"], object_by_key)

    injections_document = _load_json_file(
        paths["injections"], ErrorCode.INJECTION_MANIFEST_INVALID, "fault injections"
    )
    injections = _validate_injections(injections_document, event_by_seq)
    injection_by_id = {case["injection_id"]: case for case in injections}

    return FixtureBundle(
        root=root_path,
        seed=seed,
        clock_profile=clock_profile,
        clock_profile_dict=clock_document or {},
        manifest=manifest,
        objects=objects,
        object_by_key=object_by_key,
        events_document=events_document,
        events=events,
        event_by_seq=event_by_seq,
        v1=v1,
        v1_records=v1_records,
        v2_manifest=v2_manifest,
        v2_shards=shards,
        v2_records=v2_records,
        v2_rows=v2_rows,
        injections_document=injections_document,
        injections=injections,
        injection_by_id=injection_by_id,
        paths=paths,
    )
