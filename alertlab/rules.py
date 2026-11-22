from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .errors import AlertLabError, invalid
from .topology import Topology
from .util import (
    canonical_json,
    read_json,
    require_int,
    require_str_list,
    sha256_bytes,
    sha256_json,
    sha256_text,
)

SEVERITIES = (
    "critical",
    "error",
    "high",
    "warning",
    "medium",
    "low",
    "info",
)
SEVERITY_RANK = {
    "critical": 7,
    "error": 6,
    "high": 5,
    "warning": 4,
    "medium": 4,
    "low": 3,
    "info": 2,
}


def _fail(code: str, message: str, **details: Any) -> AlertLabError:
    return invalid(code, message, details=details or None)


@dataclass(frozen=True)
class RuleCandidate:
    rule_name: str
    service: str
    instances: tuple[str, ...]
    severity: str
    effective_at_ms: int
    upstream_dependencies: tuple[str, ...]
    silence_labels: tuple[str, ...]
    tombstone: bool
    source_version: str
    source_clock_ms: int
    generation: int
    format_priority: int
    original_time: dict[str, Any]
    migration: dict[str, Any]
    source_ref: str

    def audit_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "service": self.service,
            "instances": list(self.instances),
            "severity": self.severity,
            "effective_at_ms": self.effective_at_ms,
            "upstream_dependencies": list(self.upstream_dependencies),
            "silence_labels": list(self.silence_labels),
            "tombstone": self.tombstone,
            "source_version": self.source_version,
            "source_clock_ms": self.source_clock_ms,
            "generation": self.generation,
            "format_priority": self.format_priority,
            "original_time": self.original_time,
            "source_ref": self.source_ref,
        }

    def decision_key(self) -> tuple[int, int, int, int, str]:
        return (
            self.source_clock_ms,
            self.generation,
            self.format_priority,
            1 if self.source_version == "v2" else 0,
            self.source_ref,
        )


@dataclass(frozen=True)
class SourceSnapshot:
    source_version: str
    snapshot_id: str
    snapshot_version: int
    logical_clock_ms: int
    format_priority: int
    candidates: tuple[RuleCandidate, ...]
    migrations: tuple[dict[str, Any], ...]
    fingerprint: str
    document: dict[str, Any]


@dataclass(frozen=True)
class MergedRule:
    rule_name: str
    service: str
    instances: tuple[str, ...]
    severity: str
    effective_at_ms: int
    upstream_dependencies: tuple[str, ...]
    silence_labels: tuple[str, ...]
    tombstone: bool
    source_version: str
    source_clock_ms: int
    generation: int
    format_priority: int
    original_time: dict[str, Any]
    migration: dict[str, Any]
    adjudication: dict[str, Any]
    candidates: tuple[RuleCandidate, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "service": self.service,
            "instances": list(self.instances),
            "severity": self.severity,
            "effective_at_ms": self.effective_at_ms,
            "upstream_dependencies": list(self.upstream_dependencies),
            "silence_labels": list(self.silence_labels),
            "tombstone": self.tombstone,
            "source_version": self.source_version,
            "source_clock_ms": self.source_clock_ms,
            "generation": self.generation,
            "format_priority": self.format_priority,
            "original_time": self.original_time,
            "migration": self.migration,
            "adjudication": self.adjudication,
        }


@dataclass(frozen=True)
class MergedSnapshot:
    rules: tuple[MergedRule, ...]
    migrations: tuple[dict[str, Any], ...]
    snapshot_version: int
    logical_clock_ms: int
    injected_clock_ms: int
    fingerprint: str
    source_fingerprint: str
    sources: tuple[dict[str, Any], ...]

    @property
    def active_rules(self) -> dict[str, MergedRule]:
        return {rule.rule_name: rule for rule in self.rules if not rule.tombstone}

    @property
    def all_rules(self) -> dict[str, MergedRule]:
        return {rule.rule_name: rule for rule in self.rules}


def _validate_common(
    raw: dict[str, Any],
    topology: Topology,
    *,
    source: str,
    codes: dict[str, str],
    require_bool_tombstone: bool,
) -> dict[str, Any]:
    name = raw.get("canonical_name", raw.get("name"))
    service = raw.get("service")
    instances = raw.get("instances")
    severity = raw.get("severity")
    dependencies = raw.get("upstream_dependencies", raw.get("dependencies", []))
    silence_labels = raw.get("silence_labels", [])
    tombstone = raw.get("tombstone", False)

    if not isinstance(name, str) or not name:
        raise _fail(codes["name"], "rule name must be a non-empty string", source=source)
    if not isinstance(service, str) or service not in topology.services:
        raise _fail(
            codes["service"],
            "rule service does not exist in topology",
            source=source,
            rule=name,
            service=service,
        )
    if not require_str_list(instances, non_empty=True):
        raise _fail(
            codes["instances"],
            "instances must be a non-empty unique string list",
            source=source,
            rule=name,
        )
    for instance_id in instances:
        if instance_id not in topology.instance_to_service:
            raise _fail(
                codes["instance_unknown"],
                "rule instance does not exist in topology",
                source=source,
                rule=name,
                instance_id=instance_id,
            )
        if topology.instance_to_service[instance_id] != service:
            raise _fail(
                codes["instance_service"],
                "instance belongs to another service",
                source=source,
                rule=name,
                instance_id=instance_id,
                expected_service=topology.instance_to_service[instance_id],
            )
    if severity not in SEVERITIES:
        raise _fail(
            codes["severity"],
            "severity is not a legal enum value",
            source=source,
            rule=name,
            allowed=list(SEVERITIES),
        )
    if not require_str_list(dependencies):
        raise _fail(
            codes["dependencies"],
            "upstream_dependencies must be a unique string list",
            source=source,
            rule=name,
        )
    for dependency in dependencies:
        if dependency not in topology.services:
            raise _fail(
                codes["dependency_unknown"],
                "rule upstream dependency cannot be resolved in topology",
                source=source,
                rule=name,
                dependency=dependency,
            )
        if dependency == service:
            raise _fail(
                codes["dependency_unknown"],
                "rule cannot depend on its own service",
                source=source,
                rule=name,
                dependency=dependency,
            )
    if not require_str_list(silence_labels):
        raise _fail(
            codes["silence"],
            "silence_labels must be a unique string list",
            source=source,
            rule=name,
        )
    if require_bool_tombstone:
        if not isinstance(tombstone, bool):
            raise _fail(
                codes["tombstone"],
                "tombstone must be a JSON boolean",
                source=source,
                rule=name,
            )
    elif not isinstance(tombstone, bool):
        raise _fail(
            codes["tombstone"],
            "tombstone must be a boolean",
            source=source,
            rule=name,
        )
    return {
        "name": name,
        "service": service,
        "instances": tuple(instances),
        "severity": severity,
        "dependencies": tuple(dependencies),
        "silence_labels": tuple(silence_labels),
        "tombstone": tombstone,
    }


def _snapshot_fields(
    document: dict[str, Any],
    *,
    source: str,
    fallback_priority: int,
) -> tuple[int, int, int, str]:
    snapshot = document.get("snapshot", document)
    if not isinstance(snapshot, dict):
        raise _fail("E_SNAPSHOT_SCHEMA", "snapshot metadata must be an object", source=source)
    snapshot_id = snapshot.get("snapshot_id", source)
    version = snapshot.get("snapshot_version")
    clock = snapshot.get("logical_clock_ms")
    priority = snapshot.get("format_priority", fallback_priority)
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise _fail("E_SNAPSHOT_SCHEMA", "snapshot_id must be a non-empty string", source=source)
    if not require_int(version, minimum=1):
        raise _fail(
            "E_SNAPSHOT_SCHEMA",
            "snapshot_version must be a positive integer",
            source=source,
        )
    if not require_int(clock, minimum=0):
        raise _fail(
            "E_SNAPSHOT_SCHEMA",
            "logical_clock_ms must be a non-negative integer",
            source=source,
        )
    if not require_int(priority, minimum=0):
        raise _fail(
            "E_SNAPSHOT_SCHEMA",
            "format_priority must be a non-negative integer",
            source=source,
        )
    return version, clock, priority, snapshot_id


def _migration_for_v1(
    rule_name: str,
    seconds: int,
    snapshot_clock_ms: int,
    injected_clock_ms: int,
    generation: int,
) -> tuple[int, dict[str, Any]]:
    effective = injected_clock_ms + seconds * 1000
    migration = {
        "source_version": "v1",
        "rule_name": rule_name,
        "generation": generation,
        "source_clock_ms": snapshot_clock_ms,
        "injected_clock_ms": injected_clock_ms,
        "original_time": {"kind": "relative_seconds", "value": seconds},
        "effective_at_ms": effective,
        "scale_ms_per_unit": 1000,
        "rounding_direction": "exact",
        "decision": "relative_to_absolute",
    }
    return effective, migration


def _migration_for_v2(
    rule_name: str,
    effective_at_ms: int,
    snapshot_clock_ms: int,
    injected_clock_ms: int,
    generation: int,
) -> dict[str, Any]:
    return {
        "source_version": "v2",
        "rule_name": rule_name,
        "generation": generation,
        "source_clock_ms": snapshot_clock_ms,
        "injected_clock_ms": injected_clock_ms,
        "original_time": {"kind": "absolute_epoch_ms", "value": effective_at_ms},
        "effective_at_ms": effective_at_ms,
        "scale_ms_per_unit": 1,
        "rounding_direction": "not_applicable",
        "decision": "absolute_preserved",
    }


def load_v1(
    path: str | Path,
    topology: Topology,
    *,
    injected_clock_ms: int | None = None,
) -> SourceSnapshot:
    try:
        document = read_json(path)
    except FileNotFoundError:
        raise _fail("E_V1_FILE", "v1 rule file does not exist", path=str(path))
    except (OSError, ValueError) as exc:
        raise _fail("E_V1_JSON", "v1 rule file is not valid JSON", error=str(exc))
    if not isinstance(document, dict):
        raise _fail("E_V1_SCHEMA", "v1 document must be an object")

    version, clock, priority, snapshot_id = _snapshot_fields(
        document, source="v1", fallback_priority=10
    )
    conversion_clock = clock if injected_clock_ms is None else injected_clock_ms
    if not require_int(conversion_clock, minimum=0):
        raise _fail("E_V1_CLOCK", "injected logical clock must be a non-negative integer")

    raw_rules = document.get("rules")
    service_rules = document.get("service_rules")
    if not isinstance(raw_rules, list):
        raise _fail("E_V1_SCHEMA", "v1 rules must be a list")
    if not isinstance(service_rules, dict):
        raise _fail("E_V1_SCHEMA", "v1 service_rules must be an object")

    ordered_names: list[str] = []
    seen_names: set[str] = set()
    candidates: list[RuleCandidate] = []
    migrations: list[dict[str, Any]] = []
    declared_services: dict[str, str] = {}

    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise _fail("E_V1_SCHEMA", "each v1 rule must be an object")
        name = raw.get("name")
        if isinstance(name, str):
            if name in seen_names:
                raise _fail(
                    "E_V1_DUPLICATE_RULE",
                    "v1 rule names must be unique",
                    rule=name,
                )
            seen_names.add(name)
            ordered_names.append(name)
        validated = _validate_common(
            raw,
            topology,
            source="v1",
            codes={
                "name": "E_V1_RULE_NAME",
                "service": "E_V1_SERVICE",
                "instances": "E_V1_INSTANCES",
                "instance_unknown": "E_V1_INSTANCE_UNKNOWN",
                "instance_service": "E_V1_INSTANCE_SERVICE",
                "severity": "E_V1_SEVERITY",
                "dependencies": "E_V1_DEPENDENCIES",
                "dependency_unknown": "E_V1_DEPENDENCY_UNKNOWN",
                "silence": "E_V1_SILENCE",
                "tombstone": "E_V1_TOMBSTONE",
            },
            require_bool_tombstone=False,
        )
        relative_seconds = raw.get("effective_after_seconds")
        if not require_int(relative_seconds, minimum=0):
            raise _fail(
                "E_V1_RELATIVE_SECONDS",
                "effective_after_seconds must be a non-negative integer",
                rule=validated["name"],
            )
        declared_services[validated["name"]] = validated["service"]
        effective, migration = _migration_for_v1(
            validated["name"], relative_seconds, clock, conversion_clock, 1
        )
        migrations.append(migration)
        candidates.append(
            RuleCandidate(
                rule_name=validated["name"],
                service=validated["service"],
                instances=validated["instances"],
                severity=validated["severity"],
                effective_at_ms=effective,
                upstream_dependencies=validated["dependencies"],
                silence_labels=validated["silence_labels"],
                tombstone=False,
                source_version="v1",
                source_clock_ms=clock,
                generation=1,
                format_priority=priority,
                original_time={"kind": "relative_seconds", "value": relative_seconds},
                migration=migration,
                source_ref=f"v1:{snapshot_id}:{validated['name']}",
            )
        )

    mapped: set[tuple[str, str]] = set()
    for service, rule_names in service_rules.items():
        if not isinstance(service, str) or service not in topology.services:
            raise _fail(
                "E_V1_MAPPING",
                "service_rules key is not a topology service",
                service=service,
            )
        if not require_str_list(rule_names):
            raise _fail(
                "E_V1_MAPPING",
                "service_rules values must be unique string lists",
                service=service,
            )
        for rule_name in rule_names:
            if rule_name not in declared_services:
                raise _fail(
                    "E_V1_MAPPING",
                    "service mapping references an undeclared rule",
                    service=service,
                    rule=rule_name,
                )
            if declared_services[rule_name] != service:
                raise _fail(
                    "E_V1_MAPPING",
                    "service mapping disagrees with rule service",
                    service=service,
                    rule=rule_name,
                )
            mapped.add((service, rule_name))
    for rule_name, service in declared_services.items():
        if (service, rule_name) not in mapped:
            raise _fail(
                "E_V1_MAPPING",
                "rule declaration is absent from flat service mapping",
                service=service,
                rule=rule_name,
            )

    return SourceSnapshot(
        source_version="v1",
        snapshot_id=snapshot_id,
        snapshot_version=version,
        logical_clock_ms=clock,
        format_priority=priority,
        candidates=tuple(candidates),
        migrations=tuple(migrations),
        fingerprint=sha256_json(document),
        document=document,
    )


V2_REQUIRED_META_COLUMNS = {
    "canonical_name",
    "service",
    "instances_json",
    "severity",
    "effective_at_ms",
    "generation",
    "upstream_dependencies_json",
    "silence_labels_json",
    "tombstone",
    "shard_name",
    "record_hash",
}


def _parse_json_list(raw: Any, *, code: str, field: str, rule: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise _fail(code, f"{field} is not valid JSON", rule=rule, error=str(exc))
    if not require_str_list(value):
        raise _fail(code, f"{field} must be a unique string list", rule=rule)
    return tuple(value)


def _read_v2_metadata(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise _fail("E_V2_SQLITE_OPEN", "v2 metadata database does not exist", path=str(path))
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise _fail("E_V2_SQLITE_OPEN", "cannot open v2 metadata database", error=str(exc))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "v2_meta" not in tables or "v2_rules" not in tables:
            raise _fail(
                "E_V2_METADATA_SCHEMA",
                "v2 database must contain v2_meta and v2_rules tables",
            )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(v2_rules)")
        }
        missing = sorted(V2_REQUIRED_META_COLUMNS - columns)
        if missing:
            raise _fail(
                "E_V2_METADATA_SCHEMA",
                "v2_rules table is missing required columns",
                missing=missing,
            )
        meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM v2_meta")
        }
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT rowid AS metadata_rowid,
                       canonical_name, service, instances_json, severity,
                       effective_at_ms, generation, upstream_dependencies_json,
                       silence_labels_json, tombstone, shard_name, record_hash
                  FROM v2_rules
                 ORDER BY canonical_name, metadata_rowid
                """
            )
        ]
    except sqlite3.DatabaseError as exc:
        raise _fail("E_V2_SQLITE_OPEN", "cannot read v2 metadata database", error=str(exc))
    finally:
        connection.close()
    return meta, rows


def load_v2(
    index_path: str | Path,
    topology: Topology,
    *,
    injected_clock_ms: int | None = None,
) -> SourceSnapshot:
    index_path = Path(index_path)
    try:
        index = read_json(index_path)
    except FileNotFoundError:
        raise _fail("E_V2_FILE", "v2 index does not exist", path=str(index_path))
    except (OSError, ValueError) as exc:
        raise _fail("E_V2_JSON", "v2 index is not valid JSON", error=str(exc))
    if not isinstance(index, dict):
        raise _fail("E_V2_SCHEMA", "v2 index must be an object")

    version, clock, priority, snapshot_id = _snapshot_fields(
        index, source="v2", fallback_priority=20
    )
    if injected_clock_ms is not None and not require_int(injected_clock_ms, minimum=0):
        raise _fail("E_V2_CLOCK", "injected logical clock must be a non-negative integer")
    conversion_clock = clock if injected_clock_ms is None else injected_clock_ms

    shards = index.get("shards")
    if not isinstance(shards, list) or not shards:
        raise _fail("E_V2_SCHEMA", "v2 index shards must be a non-empty list")

    shard_records: dict[tuple[str, int], dict[str, Any]] = {}
    shard_bytes: dict[str, bytes] = {}
    for descriptor in shards:
        if not isinstance(descriptor, dict):
            raise _fail("E_V2_SCHEMA", "each shard descriptor must be an object")
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if not isinstance(relative, str) or not relative:
            raise _fail("E_V2_SCHEMA", "shard path must be a non-empty string")
        if "/" in relative or "\\" in relative or relative in {".", ".."}:
            raise _fail("E_V2_SCHEMA", "shard path must be a plain file name", shard=relative)
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise _fail("E_V2_SHARD_HASH", "sha256 must be a 64 character string", shard=relative)
        shard_path = index_path.parent / relative
        try:
            data = shard_path.read_bytes()
        except OSError as exc:
            raise _fail(
                "E_V2_SHARD_MISSING",
                "cannot read v2 shard",
                shard=relative,
                error=str(exc),
            )
        actual_hash = sha256_bytes(data)
        if actual_hash != expected_hash:
            raise _fail(
                "E_V2_SHARD_HASH",
                "v2 shard hash does not match index",
                shard=relative,
                expected=expected_hash,
                actual=actual_hash,
            )
        shard_bytes[relative] = data
        try:
            shard_document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _fail(
                "E_V2_SHARD_JSON", "v2 shard is not valid UTF-8 JSON", shard=relative, error=str(exc)
            )
        if not isinstance(shard_document, dict) or not isinstance(
            shard_document.get("rules"), list
        ):
            raise _fail(
                "E_V2_SHARD_SCHEMA",
                "v2 shard must contain a rules list",
                shard=relative,
            )
        for raw in shard_document["rules"]:
            if not isinstance(raw, dict):
                raise _fail("E_V2_SHARD_SCHEMA", "shard rule must be an object", shard=relative)
            validated = _validate_common(
                raw,
                topology,
                source="v2",
                codes={
                    "name": "E_V2_RULE_NAME",
                    "service": "E_V2_SERVICE",
                    "instances": "E_V2_INSTANCES",
                    "instance_unknown": "E_V2_INSTANCE_UNKNOWN",
                    "instance_service": "E_V2_INSTANCE_SERVICE",
                    "severity": "E_V2_SEVERITY",
                    "dependencies": "E_V2_DEPENDENCIES",
                    "dependency_unknown": "E_V2_DEPENDENCY_UNRESOLVED",
                    "silence": "E_V2_SILENCE",
                    "tombstone": "E_V2_TOMBSTONE_TYPE",
                },
                require_bool_tombstone=True,
            )
            generation = raw.get("generation")
            effective_at_ms = raw.get("effective_at_ms")
            if not require_int(generation, minimum=1):
                raise _fail(
                    "E_V2_GENERATION",
                    "generation must be a positive integer",
                    rule=validated["name"],
                )
            if not require_int(effective_at_ms, minimum=0):
                raise _fail(
                    "E_V2_EFFECTIVE_TS",
                    "effective_at_ms must be an integer",
                    rule=validated["name"],
                )
            key = (validated["name"], generation)
            if key in shard_records:
                raise _fail(
                    "E_V2_DUPLICATE_RULE",
                    "duplicate canonical_name and generation in shards",
                    rule=validated["name"],
                    generation=generation,
                )
            payload = {
                "canonical_name": validated["name"],
                "service": validated["service"],
                "instances": list(validated["instances"]),
                "severity": validated["severity"],
                "effective_at_ms": effective_at_ms,
                "generation": generation,
                "upstream_dependencies": list(validated["dependencies"]),
                "silence_labels": list(validated["silence_labels"]),
                "tombstone": validated["tombstone"],
                "shard_name": relative,
            }
            record_hash = raw.get("record_hash")
            calculated_hash = sha256_json(payload)
            if record_hash is not None and record_hash != calculated_hash:
                raise _fail(
                    "E_V2_RECORD_HASH",
                    "shard record hash does not match record content",
                    rule=validated["name"],
                    generation=generation,
                )
            shard_records[key] = {
                **payload,
                "record_hash": calculated_hash,
            }

    if not shard_records:
        raise _fail("E_V2_SCHEMA", "v2 shards contain no rules")

    database_name = index.get("metadata_db")
    if not isinstance(database_name, str) or not database_name:
        raise _fail("E_V2_SCHEMA", "v2 index must declare metadata_db")
    if "/" in database_name or "\\" in database_name or database_name in {".", ".."}:
        raise _fail("E_V2_SCHEMA", "metadata_db must be a plain file name")
    meta, rows = _read_v2_metadata(index_path.parent / database_name)

    for field, expected in (
        ("logical_clock_ms", clock),
        ("snapshot_version", version),
        ("format_priority", priority),
        ("snapshot_id", snapshot_id),
    ):
        actual = meta.get(field)
        if field in {"logical_clock_ms", "snapshot_version", "format_priority"}:
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                raise _fail(
                    "E_V2_METADATA_MISMATCH",
                    "v2 metadata field does not match index",
                    field=field,
                    index_value=expected,
                    database_value=meta.get(field),
                )
        if actual != expected:
            raise _fail(
                "E_V2_METADATA_MISMATCH",
                "v2 metadata field does not match index",
                field=field,
                index_value=expected,
                database_value=actual,
            )

    metadata_records: dict[tuple[str, int], dict[str, Any]] = {}
    generations: dict[str, list[int]] = {}
    for row in rows:
        rule_name = row["canonical_name"]
        generation = row["generation"]
        if not isinstance(rule_name, str) or not rule_name:
            raise _fail("E_V2_METADATA_SCHEMA", "metadata rule name must be a string")
        if not require_int(generation, minimum=1):
            raise _fail(
                "E_V2_GENERATION",
                "metadata generation must be a positive integer",
                rule=rule_name,
            )
        generations.setdefault(rule_name, []).append(generation)
        if not require_int(row["effective_at_ms"], minimum=0):
            raise _fail(
                "E_V2_EFFECTIVE_TS",
                "metadata effective_at_ms must be an integer",
                rule=rule_name,
                generation=generation,
            )
        if row["tombstone"] not in (0, 1) or isinstance(row["tombstone"], bool):
            raise _fail(
                "E_V2_TOMBSTONE_TYPE",
                "metadata tombstone must be the boolean-like integer 0 or 1",
                rule=rule_name,
                generation=generation,
            )
        dependencies = _parse_json_list(
            row["upstream_dependencies_json"],
            code="E_V2_DEPENDENCIES",
            field="upstream_dependencies_json",
            rule=rule_name,
        )
        for dependency in dependencies:
            if dependency not in topology.services:
                raise _fail(
                    "E_V2_DEPENDENCY_UNRESOLVED",
                    "metadata upstream dependency cannot be resolved",
                    rule=rule_name,
                    dependency=dependency,
                )
        instances = _parse_json_list(
            row["instances_json"],
            code="E_V2_INSTANCES",
            field="instances_json",
            rule=rule_name,
        )
        for instance_id in instances:
            if instance_id not in topology.instance_to_service:
                raise _fail(
                    "E_V2_INSTANCE_UNKNOWN",
                    "metadata instance does not exist",
                    rule=rule_name,
                    instance_id=instance_id,
                )
        silence_labels = _parse_json_list(
            row["silence_labels_json"],
            code="E_V2_SILENCE",
            field="silence_labels_json",
            rule=rule_name,
        )
        if row["severity"] not in SEVERITIES:
            raise _fail(
                "E_V2_SEVERITY",
                "metadata severity is invalid",
                rule=rule_name,
                generation=generation,
            )
        payload = {
            "canonical_name": rule_name,
            "service": row["service"],
            "instances": list(instances),
            "severity": row["severity"],
            "effective_at_ms": row["effective_at_ms"],
            "generation": generation,
            "upstream_dependencies": list(dependencies),
            "silence_labels": list(silence_labels),
            "tombstone": bool(row["tombstone"]),
            "shard_name": row["shard_name"],
        }
        calculated_hash = sha256_json(payload)
        if row["record_hash"] != calculated_hash:
            raise _fail(
                "E_V2_RECORD_HASH",
                "metadata record hash does not match record content",
                rule=rule_name,
                generation=generation,
            )
        key = (rule_name, generation)
        if key in metadata_records:
            raise _fail(
                "E_V2_DUPLICATE_RULE",
                "duplicate canonical_name and generation in metadata",
                rule=rule_name,
                generation=generation,
            )
        metadata_records[key] = {**payload, "record_hash": calculated_hash}

    for rule_name, values in generations.items():
        if values != sorted(set(values)):
            raise _fail(
                "E_V2_GENERATION_NONMONOTONIC",
                "rule generations must be unique and strictly increasing",
                rule=rule_name,
                generations=values,
            )

    if set(metadata_records) != set(shard_records):
        raise _fail(
            "E_V2_METADATA_MISMATCH",
            "shard and metadata rule identities differ",
            shard_only=sorted(f"{name}@{gen}" for name, gen in set(shard_records) - set(metadata_records)),
            metadata_only=sorted(
                f"{name}@{gen}" for name, gen in set(metadata_records) - set(shard_records)
            ),
        )
    for key in sorted(metadata_records):
        shard_comparable = {
            field: value
            for field, value in shard_records[key].items()
            if field != "source_ref"
        }
        if metadata_records[key] != shard_comparable:
            differing_fields = sorted(
                field
                for field in set(metadata_records[key]) | set(shard_comparable)
                if metadata_records[key].get(field) != shard_comparable.get(field)
            )
            raise _fail(
                "E_V2_METADATA_MISMATCH",
                "shard and metadata rule content differ",
                rule=key[0],
                generation=key[1],
                fields=differing_fields,
            )

    candidates: list[RuleCandidate] = []
    migrations: list[dict[str, Any]] = []
    for (rule_name, generation), record in sorted(shard_records.items()):
        migration = _migration_for_v2(
            rule_name,
            record["effective_at_ms"],
            clock,
            conversion_clock,
            generation,
        )
        migrations.append(migration)
        candidates.append(
            RuleCandidate(
                rule_name=rule_name,
                service=record["service"],
                instances=tuple(record["instances"]),
                severity=record["severity"],
                effective_at_ms=record["effective_at_ms"],
                upstream_dependencies=tuple(record["upstream_dependencies"]),
                silence_labels=tuple(record["silence_labels"]),
                tombstone=record["tombstone"],
                source_version="v2",
                source_clock_ms=clock,
                generation=generation,
                format_priority=priority,
                original_time={
                    "kind": "absolute_epoch_ms",
                    "value": record["effective_at_ms"],
                },
                migration=migration,
                source_ref=(
                    f"v2:{snapshot_id}:{record['shard_name']}:"
                    f"{rule_name}:{generation}"
                ),
            )
        )

    return SourceSnapshot(
        source_version="v2",
        snapshot_id=snapshot_id,
        snapshot_version=version,
        logical_clock_ms=clock,
        format_priority=priority,
        candidates=tuple(candidates),
        migrations=tuple(migrations),
        fingerprint=sha256_json(
            {
                "source": "v2",
                "snapshot_id": snapshot_id,
                "snapshot_version": version,
                "logical_clock_ms": clock,
                "format_priority": priority,
                "shards": {
                    name: sha256_bytes(data) for name, data in sorted(shard_bytes.items())
                },
                "rules": [candidate.audit_dict() for candidate in candidates],
            }
        ),
        document=index,
    )


def merge_snapshots(
    snapshots: Iterable[SourceSnapshot],
    *,
    injected_clock_ms: int | None = None,
) -> MergedSnapshot:
    sources = tuple(sorted(snapshots, key=lambda source: source.source_version))
    if not sources:
        raise _fail("E_SNAPSHOT_EMPTY", "at least one rule snapshot is required")
    by_name: dict[str, list[RuleCandidate]] = {}
    migrations: list[dict[str, Any]] = []
    for source in sources:
        migrations.extend(source.migrations)
        for candidate in source.candidates:
            by_name.setdefault(candidate.rule_name, []).append(candidate)

    merged: list[MergedRule] = []
    for rule_name in sorted(by_name):
        candidates = sorted(
            by_name[rule_name], key=lambda candidate: candidate.decision_key()
        )
        chosen = candidates[-1]
        rejected = [
            {
                "source_ref": candidate.source_ref,
                "source_version": candidate.source_version,
                "logical_clock_ms": candidate.source_clock_ms,
                "generation": candidate.generation,
                "format_priority": candidate.format_priority,
                "effective_at_ms": candidate.effective_at_ms,
                "tombstone": candidate.tombstone,
            }
            for candidate in candidates[:-1]
        ]
        adjudication = {
            "decision": "latest_by_clock_generation_priority",
            "sort_key": [
                "logical_clock_ms",
                "generation",
                "format_priority",
                "source_version",
            ],
            "chosen": chosen.audit_dict(),
            "rejected_candidates": rejected,
            "basis": {
                "logical_clock_ms": chosen.source_clock_ms,
                "generation": chosen.generation,
                "format_priority": chosen.format_priority,
                "source_version": chosen.source_version,
            },
        }
        merged.append(
            MergedRule(
                rule_name=chosen.rule_name,
                service=chosen.service,
                instances=chosen.instances,
                severity=chosen.severity,
                effective_at_ms=chosen.effective_at_ms,
                upstream_dependencies=chosen.upstream_dependencies,
                silence_labels=chosen.silence_labels,
                tombstone=chosen.tombstone,
                source_version=chosen.source_version,
                source_clock_ms=chosen.source_clock_ms,
                generation=chosen.generation,
                format_priority=chosen.format_priority,
                original_time=chosen.original_time,
                migration=chosen.migration,
                adjudication=adjudication,
                candidates=tuple(candidates),
            )
        )

    view_clock = max(source.logical_clock_ms for source in sources)
    conversion_clock = view_clock if injected_clock_ms is None else injected_clock_ms
    if not require_int(conversion_clock, minimum=0):
        raise _fail("E_SNAPSHOT_CLOCK", "injected clock must be a non-negative integer")
    snapshot_version = max(source.snapshot_version for source in sources)
    fingerprint = sha256_json(
        {
            "snapshot_version": snapshot_version,
            "logical_clock_ms": view_clock,
            "injected_clock_ms": conversion_clock,
            "rules": [rule.to_record() for rule in merged],
        }
    )
    source_fingerprint = sha256_json(
        {
            "snapshot_version": snapshot_version,
            "sources": sorted(
                (
                    source.source_version,
                    source.snapshot_id,
                    source.snapshot_version,
                    source.fingerprint,
                )
                for source in sources
            ),
        }
    )
    return MergedSnapshot(
        rules=tuple(merged),
        migrations=tuple(migrations),
        snapshot_version=snapshot_version,
        logical_clock_ms=view_clock,
        injected_clock_ms=conversion_clock,
        fingerprint=fingerprint,
        source_fingerprint=source_fingerprint,
        sources=tuple(
            {
                "source_version": source.source_version,
                "snapshot_id": source.snapshot_id,
                "snapshot_version": source.snapshot_version,
                "logical_clock_ms": source.logical_clock_ms,
                "format_priority": source.format_priority,
                "fingerprint": source.fingerprint,
            }
            for source in sources
        ),
    )


def load_and_merge(
    topology: Topology,
    *,
    v1_path: str | Path | None,
    v2_path: str | Path | None,
    injected_clock_ms: int | None = None,
) -> MergedSnapshot:
    if v1_path is None and v2_path is None:
        raise _fail("E_SNAPSHOT_EMPTY", "at least one of v1_path or v2_path is required")
    snapshots: list[SourceSnapshot] = []
    if v1_path is not None:
        snapshots.append(load_v1(v1_path, topology, injected_clock_ms=injected_clock_ms))
    if v2_path is not None:
        snapshots.append(load_v2(v2_path, topology, injected_clock_ms=injected_clock_ms))
    merged = merge_snapshots(snapshots, injected_clock_ms=injected_clock_ms)
    fingerprint = sha256_json(
        {
            "merged_fingerprint": merged.fingerprint,
            "topology_fingerprint": topology.fingerprint,
        }
    )
    return dataclasses.replace(merged, fingerprint=fingerprint)


def migration_decision_json(migration: dict[str, Any]) -> str:
    return canonical_json(migration)


def migration_decision_key(migration: dict[str, Any]) -> str:
    return sha256_json(
        {
            "source_version": migration["source_version"],
            "rule_name": migration["rule_name"],
            "generation": migration["generation"],
            "source_clock_ms": migration["source_clock_ms"],
            "injected_clock_ms": migration["injected_clock_ms"],
        }
    )


def merged_rule_from_record(record: dict[str, Any]) -> MergedRule:
    """Build the engine-facing rule object from a persisted view record."""

    return MergedRule(
        rule_name=record["rule_name"],
        service=record["service"],
        instances=tuple(record["instances"]),
        severity=record["severity"],
        effective_at_ms=record["effective_at_ms"],
        upstream_dependencies=tuple(record.get("upstream_dependencies", [])),
        silence_labels=tuple(record.get("silence_labels", [])),
        tombstone=bool(record["tombstone"]),
        source_version=record["source_version"],
        source_clock_ms=record["source_clock_ms"],
        generation=record["generation"],
        format_priority=record["format_priority"],
        original_time=dict(record["original_time"]),
        migration=dict(record.get("migration", {})),
        adjudication=dict(record.get("adjudication", {})),
        candidates=(),
    )
