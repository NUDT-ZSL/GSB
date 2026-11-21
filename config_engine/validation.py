"""Loading and validation for package files, v1 manifests, and v2 shards."""

from __future__ import annotations

import glob
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    canonical_json,
    digest_token,
    is_finite_number,
    is_int,
    load_json_file,
    sha256_bytes,
    sha256_value,
    validate_flat_params,
    validate_name,
)
from .errors import EngineError, error
from .models import content_digest


V1_FORMAT_PRIORITY = 10
V2_FORMAT_PRIORITY = 20
DEFAULT_SOURCE_PRIORITY = {"manual": 20, "auto": 10}


@dataclass
class BaseCandidate:
    """A validated candidate before v1 time materialization."""

    service: str
    environment: str
    logical_clock_ms: int
    generation: int
    source_format: str
    source_priority: int
    version: str
    parsed_params: dict[str, Any]
    upstreams: list[str]
    tombstone: bool
    expires_at_ms: int | None
    relative_effective_after_s: int | None
    absolute_effective_at_ms: int | None
    source_ref: dict[str, Any]

    @property
    def target(self) -> tuple[str, str]:
        return (self.service, self.environment)

    @property
    def params(self) -> dict[str, Any]:
        if self.source_format != "v1":
            return self.parsed_params
        params = dict(self.parsed_params)
        return params

    @property
    def content_digest(self) -> str:
        return content_digest(
            service=self.service,
            environment=self.environment,
            generation=self.generation,
            version=self.version,
            params=self.params,
            upstreams=self.upstreams,
            tombstone=self.tombstone,
            expires_at_ms=self.expires_at_ms,
        )

    def original_time(self) -> dict[str, Any]:
        if self.source_format == "v1":
            return {
                "kind": "relative_seconds",
                "manifest_logical_clock_ms": self.logical_clock_ms,
                "relative_effective_after_s": self.relative_effective_after_s,
            }
        return {"effective_at_ms": self.absolute_effective_at_ms, "kind": "absolute_epoch_ms"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "absolute_effective_at_ms": self.absolute_effective_at_ms,
            "content_digest": self.content_digest,
            "environment": self.environment,
            "expires_at_ms": self.expires_at_ms,
            "generation": self.generation,
            "logical_clock_ms": self.logical_clock_ms,
            "parsed_params": self.parsed_params,
            "relative_effective_after_s": self.relative_effective_after_s,
            "service": self.service,
            "source_format": self.source_format,
            "source_priority": self.source_priority,
            "source_ref": self.source_ref,
            "tombstone": self.tombstone,
            "upstreams": self.upstreams,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BaseCandidate":
        return cls(**value)


@dataclass
class Package:
    path: Path
    spec: dict[str, Any]
    digest: str
    v1_path: Path | None
    v1_candidates: list[BaseCandidate]
    v2_metadata_path: Path | None
    v2_shard_paths: list[Path]
    v2_candidates: list[BaseCandidate]
    event_paths: dict[str, Path]
    clock_steps: dict[str, int]
    default_clock_step: str | None
    initial_clock_ms: int
    clock_tolerance_ms: int
    source_priorities: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def base_candidates(self) -> list[BaseCandidate]:
        return self.v1_candidates + self.v2_candidates

    @property
    def package_digest(self) -> str:
        return self.digest


def _resolve(base: Path, value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise error("SCHEMA_INVALID", f"{field_name} must be a non-empty path", field=field_name)
    path = (base / value).resolve()
    return path


def _require_int(
    value: Any,
    code: str,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not is_int(value):
        raise error(code, f"{field_name} must be an integer", field=field_name, value=value)
    if minimum is not None and value < minimum:
        raise error(
            code,
            f"{field_name} must be >= {minimum}",
            field=field_name,
            value=value,
            minimum=minimum,
        )
    if maximum is not None and value > maximum:
        raise error(
            code,
            f"{field_name} must be <= {maximum}",
            field=field_name,
            value=value,
            maximum=maximum,
        )
    return value


def _version(value: Any, default: str, field_name: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise error("SCHEMA_INVALID", f"{field_name} must be a non-empty string", field=field_name)
    return value


def _expires(value: Any, effective_at: int, field_name: str) -> int | None:
    if value is None:
        return None
    parsed = _require_int(value, "SCHEMA_INVALID", field_name, minimum=0)
    if parsed <= effective_at:
        raise error(
            "SCHEMA_INVALID",
            f"{field_name} must be greater than effective time",
            field=field_name,
            value=parsed,
            effective_at_ms=effective_at,
        )
    return parsed


def parse_v1(path: Path) -> list[BaseCandidate]:
    document = load_json_file(path)
    if not isinstance(document, dict):
        raise error("SCHEMA_INVALID", "v1 manifest must be a JSON object", path=str(path))
    if document.get("schema_version") != 1:
        raise error(
            "SCHEMA_INVALID",
            "v1 manifest schema_version must be 1",
            path=str(path),
            field="schema_version",
        )
    root_clock = _require_int(
        document.get("logical_clock_ms"),
        "SCHEMA_INVALID",
        "logical_clock_ms",
        minimum=0,
    )
    records = document.get("records")
    if not isinstance(records, list):
        raise error("SCHEMA_INVALID", "v1 records must be an array", field="records")

    seen: dict[tuple[str, str], int] = {}
    result: list[BaseCandidate] = []
    for index, record in enumerate(records):
        prefix = f"records[{index}]"
        if not isinstance(record, dict):
            raise error("SCHEMA_INVALID", f"{prefix} must be an object", field=prefix)
        service = validate_name(record.get("service"), f"{prefix}.service")
        environment = validate_name(
            record.get("environment"), f"{prefix}.environment", environment=True
        )
        target = (service, environment)
        if target in seen:
            raise error(
                "DUPLICATE_SERVICE_ENV",
                f"duplicate v1 service/environment: {service}/{environment}",
                service=service,
                environment=environment,
                first_index=seen[target],
                duplicate_index=index,
            )
        seen[target] = index

        replicas = _require_int(record.get("replicas"), "INVALID_REPLICAS", f"{prefix}.replicas", minimum=1)
        timeout_ms = _require_int(
            record.get("timeout_ms"), "INVALID_TIMEOUT", f"{prefix}.timeout_ms", minimum=0
        )
        retry = record.get("retry_factor")
        if not is_finite_number(retry) or float(retry) <= 0:
            raise error(
                "INVALID_RETRY_FACTOR",
                f"{prefix}.retry_factor must be a finite positive number",
                field=f"{prefix}.retry_factor",
                value=retry,
            )
        effective_after_s = _require_int(
            record.get("effective_after_s"),
            "INVALID_EFFECTIVE_AFTER",
            f"{prefix}.effective_after_s",
            minimum=0,
        )
        generation = _require_int(
            record.get("generation", 1),
            "SCHEMA_INVALID",
            f"{prefix}.generation",
            minimum=1,
        )
        clock = record.get("logical_clock_ms", root_clock)
        logical_clock = _require_int(
            clock, "SCHEMA_INVALID", f"{prefix}.logical_clock_ms", minimum=0
        )
        version = _version(record.get("version"), f"v1-g{generation}", f"{prefix}.version")
        declared = validate_flat_params(record.get("params"), f"{prefix}.params")

        reserved = {
            "replicas": replicas,
            "retry_factor": retry,
            "timeout_ms": timeout_ms,
        }
        for key, expected in reserved.items():
            if key in declared and declared[key] != expected:
                raise error(
                    "PARAM_DECLARATION_MISMATCH",
                    f"{prefix}.params.{key} disagrees with declaration",
                    field=f"{prefix}.params.{key}",
                    declared=expected,
                    parameter=declared[key],
                )
        effective_params = {
            "replicas": replicas,
            "retry_factor": retry,
            "timeout_ms": timeout_ms,
            **declared,
        }
        expires_at = _expires(record.get("expires_at_ms"), 0, f"{prefix}.expires_at_ms")
        result.append(
            BaseCandidate(
                service=service,
                environment=environment,
                logical_clock_ms=logical_clock,
                generation=generation,
                source_format="v1",
                source_priority=V1_FORMAT_PRIORITY,
                version=version,
                parsed_params=effective_params,
                upstreams=[],
                tombstone=False,
                expires_at_ms=expires_at,
                relative_effective_after_s=effective_after_s,
                absolute_effective_at_ms=None,
                source_ref={"file": path.name, "index": index},
            )
        )
    return result


def _read_v2_metadata(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists() or not path.is_file():
        raise error(
            "CONFIG_PACKAGE_MISSING",
            f"missing v2 metadata database: {path}",
            path=str(path),
        )
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {"shard_registry", "config_metadata"}
            if not required.issubset(tables):
                raise error(
                    "INVALID_DB",
                    "v2 metadata database lacks required tables",
                    path=str(path),
                    missing=sorted(required - tables),
                )
            registry = [
                dict(row)
                for row in conn.execute(
                    "SELECT shard_path, sha256, logical_clock_ms, schema_version "
                    "FROM shard_registry ORDER BY shard_path"
                ).fetchall()
            ]
            metadata = [
                dict(row)
                for row in conn.execute(
                    "SELECT service, environment, generation, effective_at_ms, "
                    "logical_clock_ms, shard_path, record_digest, tombstone "
                    "FROM config_metadata ORDER BY service, environment, generation, shard_path"
                ).fetchall()
            ]
            return registry, metadata
        finally:
            conn.close()
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise error(
            "INVALID_DB",
            f"cannot read v2 metadata database: {exc}",
            path=str(path),
        ) from exc


def _parse_v2_shard(
    path: Path,
    *,
    metadata_path: Path,
    registry_row: dict[str, Any],
    metadata_rows: list[dict[str, Any]],
) -> list[BaseCandidate]:
    if not path.exists() or not path.is_file():
        raise error("CONFIG_PACKAGE_MISSING", f"missing v2 shard: {path}", path=str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise error("CONFIG_PACKAGE_MISSING", f"cannot read v2 shard: {path}", path=str(path)) from exc
    actual_hash = sha256_bytes(raw)
    expected_hash = digest_token(str(registry_row.get("sha256", "")))
    if actual_hash != expected_hash:
        raise error(
            "SHARD_HASH_MISMATCH",
            f"v2 shard hash mismatch: {path.name}",
            shard=str(path),
            expected=expected_hash,
            actual=actual_hash,
        )
    document = load_json_file(path)
    if not isinstance(document, dict):
        raise error("SCHEMA_INVALID", "v2 shard must be a JSON object", path=str(path))
    if document.get("schema_version") != 2:
        raise error(
            "SCHEMA_INVALID",
            "v2 shard schema_version must be 2",
            path=str(path),
            field="schema_version",
        )
    shard_clock = _require_int(
        document.get("logical_clock_ms"),
        "SCHEMA_INVALID",
        "logical_clock_ms",
        minimum=0,
    )
    registry_clock = _require_int(
        registry_row.get("logical_clock_ms"),
        "INVALID_DB",
        "shard_registry.logical_clock_ms",
        minimum=0,
    )
    if shard_clock != registry_clock:
        raise error(
            "SCHEMA_INVALID",
            "shard logical clock differs from SQLite metadata",
            shard=str(path),
            shard_logical_clock_ms=shard_clock,
            metadata_logical_clock_ms=registry_clock,
        )
    records = document.get("records")
    if not isinstance(records, list):
        raise error("SCHEMA_INVALID", "v2 records must be an array", path=str(path))

    metadata_by_key = {
        (
            row.get("service"),
            row.get("environment"),
            row.get("generation"),
            digest_token(str(row.get("record_digest", ""))),
        ): row
        for row in metadata_rows
    }
    seen_metadata: set[tuple[Any, ...]] = set()
    result: list[BaseCandidate] = []
    for index, record in enumerate(records):
        prefix = f"{path.name}:records[{index}]"
        if not isinstance(record, dict):
            raise error("SCHEMA_INVALID", f"{prefix} must be an object", field=prefix)
        service = validate_name(record.get("service"), f"{prefix}.service")
        environment = validate_name(
            record.get("environment"), f"{prefix}.environment", environment=True
        )
        generation = _require_int(
            record.get("generation"),
            "SCHEMA_INVALID",
            f"{prefix}.generation",
            minimum=1,
        )
        effective_at = _require_int(
            record.get("effective_at_ms"),
            "INVALID_EFFECTIVE_AT",
            f"{prefix}.effective_at_ms",
            minimum=0,
        )
        expires_at = _expires(record.get("expires_at_ms"), effective_at, f"{prefix}.expires_at_ms")
        tombstone = record.get("tombstone", False)
        if not isinstance(tombstone, bool):
            raise error(
                "SCHEMA_INVALID",
                f"{prefix}.tombstone must be boolean",
                field=f"{prefix}.tombstone",
            )
        params = validate_flat_params(record.get("params", {}), f"{prefix}.params")
        upstreams_raw = record.get("upstreams", [])
        if not isinstance(upstreams_raw, list):
            raise error(
                "SCHEMA_INVALID",
                f"{prefix}.upstreams must be an array",
                field=f"{prefix}.upstreams",
            )
        upstreams: list[str] = []
        for upstream_index, upstream in enumerate(upstreams_raw):
            name_value = validate_name(
                upstream, f"{prefix}.upstreams[{upstream_index}]"
            )
            if name_value not in upstreams:
                upstreams.append(name_value)
        version = _version(record.get("version"), f"v2-g{generation}", f"{prefix}.version")
        candidate = BaseCandidate(
            service=service,
            environment=environment,
            logical_clock_ms=shard_clock,
            generation=generation,
            source_format="v2",
            source_priority=V2_FORMAT_PRIORITY,
            version=version,
            parsed_params=params,
            upstreams=sorted(upstreams),
            tombstone=tombstone,
            expires_at_ms=expires_at,
            relative_effective_after_s=None,
            absolute_effective_at_ms=effective_at,
            source_ref={
                "file": path.name,
                "index": index,
                "metadata_db": metadata_path.name,
            },
        )
        key = (service, environment, generation, candidate.content_digest)
        metadata_row = metadata_by_key.get(key)
        if metadata_row is None:
            raise error(
                "SCHEMA_INVALID",
                "v2 shard record has no matching SQLite metadata row",
                shard=str(path),
                service=service,
                environment=environment,
                generation=generation,
                record_digest=candidate.content_digest,
            )
        if Path(str(metadata_row.get("shard_path"))).name != path.name:
            raise error(
                "SCHEMA_INVALID",
                "v2 metadata row points at a different shard",
                expected_shard=str(metadata_row.get("shard_path")),
                actual_shard=path.name,
            )
        if bool(metadata_row.get("tombstone")) != tombstone:
            raise error(
                "SCHEMA_INVALID",
                "v2 metadata tombstone flag differs from shard",
                service=service,
                environment=environment,
                generation=generation,
            )
        if int(metadata_row.get("effective_at_ms")) != effective_at:
            raise error(
                "SCHEMA_INVALID",
                "v2 metadata effective time differs from shard",
                service=service,
                environment=environment,
                generation=generation,
            )
        if int(metadata_row.get("logical_clock_ms")) != shard_clock:
            raise error(
                "SCHEMA_INVALID",
                "v2 metadata logical clock differs from shard",
                service=service,
                environment=environment,
                generation=generation,
            )
        seen_metadata.add(key)
        result.append(candidate)

    extras = set(metadata_by_key) - seen_metadata
    if extras:
        raise error(
            "SCHEMA_INVALID",
            "v2 SQLite metadata contains rows absent from shards",
            shard=str(path),
            rows=[
                {
                    "environment": item[1],
                    "generation": item[2],
                    "record_digest": item[3],
                    "service": item[0],
                }
                for item in sorted(extras)
            ],
        )
    return result


def _validate_v2_generations(candidates: Iterable[BaseCandidate]) -> None:
    grouped: dict[tuple[str, str], list[BaseCandidate]] = {}
    for candidate in candidates:
        if candidate.source_format == "v2":
            grouped.setdefault(candidate.target, []).append(candidate)
    for target, items in sorted(grouped.items()):
        ordered = sorted(
            items,
            key=lambda item: (
                item.logical_clock_ms,
                (
                    item.absolute_effective_at_ms
                    if item.absolute_effective_at_ms is not None
                    else -1
                ),
                item.generation,
                item.content_digest,
            ),
        )
        previous: BaseCandidate | None = None
        seen_generation: dict[int, str] = {}
        for item in ordered:
            if previous is not None and item.generation < previous.generation:
                raise error(
                    "GENERATION_REGRESSION",
                    "v2 configuration generation moves backwards",
                    service=target[0],
                    environment=target[1],
                    previous_generation=previous.generation,
                    generation=item.generation,
                    previous_logical_clock_ms=previous.logical_clock_ms,
                    logical_clock_ms=item.logical_clock_ms,
                )
            if item.generation in seen_generation:
                if seen_generation[item.generation] != item.content_digest:
                    raise error(
                        "GENERATION_CONFLICT",
                        "one v2 generation has conflicting content",
                        service=target[0],
                        environment=target[1],
                        generation=item.generation,
                        first_digest=seen_generation[item.generation],
                        second_digest=item.content_digest,
                    )
            else:
                seen_generation[item.generation] = item.content_digest
            previous = item


def _validate_dependencies(candidates: Iterable[BaseCandidate]) -> None:
    all_candidates = list(candidates)
    declared = {candidate.target for candidate in all_candidates}
    for candidate in all_candidates:
        if candidate.source_format != "v2":
            continue
        for upstream in candidate.upstreams:
            upstream_target = (upstream, candidate.environment)
            if upstream_target not in declared:
                raise error(
                    "MISSING_UPSTREAM",
                    f"upstream {upstream!r} does not exist in environment "
                    f"{candidate.environment!r}",
                    service=candidate.service,
                    environment=candidate.environment,
                    generation=candidate.generation,
                    upstream=upstream,
                    upstream_environment=candidate.environment,
                )
    latest_v2: dict[tuple[str, str], BaseCandidate] = {}
    for candidate in all_candidates:
        if candidate.source_format != "v2":
            continue
        current = latest_v2.get(candidate.target)
        if current is None or (
            candidate.generation,
            candidate.logical_clock_ms,
            candidate.absolute_effective_at_ms or 0,
        ) > (
            current.generation,
            current.logical_clock_ms,
            current.absolute_effective_at_ms or 0,
        ):
            latest_v2[candidate.target] = candidate

    graph: dict[tuple[str, str], list[tuple[str, str]]] = {
        target: [] for target in sorted(declared)
    }
    for target in sorted(latest_v2):
        candidate = latest_v2[target]
        if candidate.tombstone:
            continue
        for upstream in candidate.upstreams:
            upstream_target = (upstream, target[1])
            if upstream_target not in declared:
                raise error(
                    "MISSING_UPSTREAM",
                    f"upstream {upstream!r} does not exist in environment {target[1]!r}",
                    service=target[0],
                    environment=target[1],
                    upstream=upstream,
                    upstream_environment=target[1],
                )
            graph[target].append(upstream_target)

    color: dict[tuple[str, str], int] = {}
    stack: list[tuple[str, str]] = []

    def visit(node: tuple[str, str]) -> None:
        color[node] = 1
        stack.append(node)
        for neighbor in sorted(graph.get(node, [])):
            if color.get(neighbor) == 1:
                start = stack.index(neighbor)
                cycle = stack[start:] + [neighbor]
                raise error(
                    "DEPENDENCY_CYCLE",
                    "dependency graph contains a cycle",
                    cycle=[
                        {"environment": env, "service": service} for service, env in cycle
                    ],
                )
            if color.get(neighbor, 0) == 0:
                visit(neighbor)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            visit(node)


def _file_hash(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise error("CONFIG_PACKAGE_MISSING", f"missing file: {path}", path=str(path))
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise error("CONFIG_PACKAGE_MISSING", f"cannot read file: {path}", path=str(path)) from exc


def load_package(path: str | Path) -> Package:
    package_path = Path(path).resolve()
    spec = load_json_file(package_path)
    if not isinstance(spec, dict):
        raise error("SCHEMA_INVALID", "package specification must be a JSON object")
    if spec.get("schema_version") != 1:
        raise error(
            "SCHEMA_INVALID",
            "package schema_version must be 1",
            path=str(package_path),
        )
    base = package_path.parent

    v1_path: Path | None = None
    v1_candidates: list[BaseCandidate] = []
    v1_spec = spec.get("v1")
    if v1_spec is not None:
        if not isinstance(v1_spec, dict):
            raise error("SCHEMA_INVALID", "package.v1 must be an object", field="v1")
        v1_path = _resolve(base, v1_spec.get("path"), "v1.path")
        v1_candidates = parse_v1(v1_path)

    v2_metadata_path: Path | None = None
    v2_shard_paths: list[Path] = []
    v2_candidates: list[BaseCandidate] = []
    v2_spec = spec.get("v2")
    if v2_spec is not None:
        if not isinstance(v2_spec, dict):
            raise error("SCHEMA_INVALID", "package.v2 must be an object", field="v2")
        v2_metadata_path = _resolve(base, v2_spec.get("metadata_db"), "v2.metadata_db")
        patterns = v2_spec.get("shards")
        if not isinstance(patterns, list) or not patterns:
            raise error(
                "SCHEMA_INVALID",
                "package.v2.shards must be a non-empty array",
                field="v2.shards",
            )
        seen_paths: set[Path] = set()
        for pattern_index, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern:
                raise error(
                    "SCHEMA_INVALID",
                    f"v2.shards[{pattern_index}] must be a non-empty string",
                )
            matches = sorted(Path(item).resolve() for item in glob.glob(str(base / pattern)))
            if not matches:
                raise error(
                    "CONFIG_PACKAGE_MISSING",
                    f"v2 shard pattern matched no files: {pattern}",
                    pattern=pattern,
                )
            for item in matches:
                if item not in seen_paths:
                    v2_shard_paths.append(item)
                    seen_paths.add(item)
        registry_rows, metadata_rows = _read_v2_metadata(v2_metadata_path)
        registry_by_name = {
            Path(str(row.get("shard_path"))).name: row for row in registry_rows
        }
        for shard_path in v2_shard_paths:
            row = registry_by_name.get(shard_path.name)
            if row is None:
                raise error(
                    "SCHEMA_INVALID",
                    "v2 shard is not registered in SQLite metadata",
                    shard=str(shard_path),
                )
            v2_candidates.extend(
                _parse_v2_shard(
                    shard_path,
                    metadata_path=v2_metadata_path,
                    registry_row=row,
                    metadata_rows=[
                        item
                        for item in metadata_rows
                        if Path(str(item.get("shard_path"))).name == shard_path.name
                    ],
                )
            )
        _validate_v2_generations(v2_candidates)

    if not v1_candidates and not v2_candidates:
        raise error("SCHEMA_INVALID", "package declares no configuration records")

    _validate_dependencies(v1_candidates + v2_candidates)

    events_spec = spec.get("events", {})
    if events_spec is None:
        events_spec = {}
    if not isinstance(events_spec, dict):
        raise error("SCHEMA_INVALID", "package.events must be an object", field="events")
    event_paths: dict[str, Path] = {}
    for source, item in sorted(events_spec.items()):
        if not isinstance(source, str) or not source:
            raise error("SCHEMA_INVALID", "event source names must be non-empty strings")
        if isinstance(item, str):
            event_paths[source] = _resolve(base, item, f"events.{source}")
        elif isinstance(item, dict):
            event_paths[source] = _resolve(base, item.get("path"), f"events.{source}.path")
        else:
            raise error("SCHEMA_INVALID", f"events.{source} must be a path or object")
        _file_hash(event_paths[source])

    clock_spec = spec.get("clock", {})
    if not isinstance(clock_spec, dict):
        raise error("SCHEMA_INVALID", "package.clock must be an object", field="clock")
    clock_steps: dict[str, int] = {}
    steps_value = clock_spec.get("steps", {})
    if isinstance(steps_value, dict):
        for name, value in steps_value.items():
            if not isinstance(name, str) or not name:
                raise error("CLOCK_INVALID", "clock step names must be non-empty strings")
            clock_steps[name] = _require_int(
                value, "CLOCK_INVALID", f"clock.steps.{name}", minimum=0
            )
    elif isinstance(steps_value, list):
        for index, step in enumerate(steps_value):
            if not isinstance(step, dict):
                raise error("CLOCK_INVALID", f"clock.steps[{index}] must be an object")
            name = step.get("name")
            if not isinstance(name, str) or not name:
                raise error("CLOCK_INVALID", f"clock.steps[{index}].name is required")
            clock_steps[name] = _require_int(
                step.get("clock_ms"),
                "CLOCK_INVALID",
                f"clock.steps[{index}].clock_ms",
                minimum=0,
            )
    else:
        raise error("CLOCK_INVALID", "package.clock.steps must be an object or array")

    default_clock_step = clock_spec.get("default_step")
    if default_clock_step is not None and default_clock_step not in clock_steps:
        raise error(
            "CLOCK_INVALID",
            f"default clock step is not declared: {default_clock_step}",
            default_step=default_clock_step,
        )
    initial_clock = _require_int(
        clock_spec.get("initial_clock_ms", 0),
        "CLOCK_INVALID",
        "clock.initial_clock_ms",
        minimum=0,
    )
    tolerance = _require_int(
        clock_spec.get("rollback_tolerance_ms", 250),
        "CLOCK_INVALID",
        "clock.rollback_tolerance_ms",
        minimum=0,
    )
    priorities = dict(DEFAULT_SOURCE_PRIORITY)
    priority_spec = clock_spec.get("source_priorities")
    if priority_spec is not None:
        if not isinstance(priority_spec, dict):
            raise error(
                "SCHEMA_INVALID",
                "clock.source_priorities must be an object",
                field="clock.source_priorities",
            )
        for source, priority in priority_spec.items():
            priorities[source] = _require_int(
                priority,
                "SCHEMA_INVALID",
                f"clock.source_priorities.{source}",
                minimum=0,
            )

    digest_payload = {
        "events": {source: _file_hash(item) for source, item in sorted(event_paths.items())},
        "spec": sha256_value(spec),
        "v1": _file_hash(v1_path) if v1_path is not None else None,
        "v2_metadata": _v2_metadata_digest(v2_candidates),
        "v2_shards": {
            item.name: _file_hash(item) for item in sorted(v2_shard_paths)
        },
    }
    digest = sha256_value(digest_payload)
    return Package(
        path=package_path,
        spec=spec,
        digest=digest,
        v1_path=v1_path,
        v1_candidates=v1_candidates,
        v2_metadata_path=v2_metadata_path,
        v2_shard_paths=v2_shard_paths,
        v2_candidates=v2_candidates,
        event_paths=event_paths,
        clock_steps=clock_steps,
        default_clock_step=default_clock_step,
        initial_clock_ms=initial_clock,
        clock_tolerance_ms=tolerance,
        source_priorities=priorities,
        metadata={
            "base_candidate_count": len(v1_candidates) + len(v2_candidates),
            "v1_candidate_count": len(v1_candidates),
            "v2_candidate_count": len(v2_candidates),
        },
    )


def _v2_metadata_digest(candidates: list[BaseCandidate]) -> str:
    items = []
    for candidate in candidates:
        items.append(
            {
                "content_digest": candidate.content_digest,
                "effective_at_ms": candidate.absolute_effective_at_ms,
                "environment": candidate.environment,
                "generation": candidate.generation,
                "logical_clock_ms": candidate.logical_clock_ms,
                "service": candidate.service,
                "source_ref": candidate.source_ref,
                "tombstone": candidate.tombstone,
            }
        )
    return sha256_value(sorted(items, key=canonical_json))


def resolve_clock(
    package: Package,
    *,
    clock_ms: int | None = None,
    clock_step: str | None = None,
) -> tuple[int, str]:
    if clock_ms is not None and clock_step is not None:
        raise error(
            "INVALID_ARGUMENT",
            "clock_ms and clock_step are mutually exclusive",
        )
    if clock_step is not None:
        if clock_step not in package.clock_steps:
            raise error(
                "INVALID_ARGUMENT",
                f"unknown clock step: {clock_step}",
                clock_step=clock_step,
                available=sorted(package.clock_steps),
            )
        return package.clock_steps[clock_step], clock_step
    if clock_ms is not None:
        return _require_int(clock_ms, "CLOCK_INVALID", "clock_ms", minimum=0), "explicit"
    if package.default_clock_step is not None:
        return (
            package.clock_steps[package.default_clock_step],
            package.default_clock_step,
        )
    return package.initial_clock_ms, "initial"


def summarize_package(package: Package) -> dict[str, Any]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in package.base_candidates:
        entry = grouped.setdefault(
            candidate.target,
            {
                "environment": candidate.environment,
                "generations": [],
                "service": candidate.service,
                "source_formats": [],
            },
        )
        if candidate.generation not in entry["generations"]:
            entry["generations"].append(candidate.generation)
        if candidate.source_format not in entry["source_formats"]:
            entry["source_formats"].append(candidate.source_format)
    services = []
    for key in sorted(grouped):
        entry = grouped[key]
        entry["generations"] = sorted(entry["generations"])
        entry["source_formats"] = sorted(entry["source_formats"])
        services.append(entry)
    return {
        "base_candidates": len(package.base_candidates),
        "clock_steps": package.clock_steps,
        "events": {
            source: str(path)
            for source, path in sorted(package.event_paths.items())
        },
        "package_digest": package.digest,
        "package_path": str(package.path),
        "services": services,
        "source_priorities": package.source_priorities,
        "v1_candidates": len(package.v1_candidates),
        "v2_candidates": len(package.v2_candidates),
    }
