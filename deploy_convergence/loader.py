"""Load and validate v1 YAML and v2 sharded JSON/SQLite manifests."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .canonical import (
    canonical_json,
    digest_file,
    digest_value,
    is_int,
    is_number,
    normalize_digest,
    scaled_milliseconds,
)
from .errors import EngineError
from .injection import InjectionController
from .validation import (
    CORE_PARAMETERS,
    fail,
    validate_manifest_version,
    validate_nonnegative_int,
    validate_parameter_type_schema,
    validate_params,
    validate_resource_limit,
    validate_retry_factor,
    validate_service_name,
)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class LoadedManifests:
    payload: dict[str, Any]
    source_fingerprint: str


class ManifestLoader:
    def __init__(
        self,
        *,
        v1_path: str | Path,
        v2_meta_path: str | Path,
        v2_shards_dir: str | Path,
        expected_environments: list[str],
    ) -> None:
        self.v1_path = Path(v1_path).resolve()
        self.v2_meta_path = Path(v2_meta_path).resolve()
        self.v2_shards_dir = Path(v2_shards_dir).resolve()
        self.expected_environments = tuple(expected_environments)
        if len(set(self.expected_environments)) != len(self.expected_environments):
            raise ValueError("expected_environments must be unique")

    def source_fingerprint(self) -> str:
        parts: list[dict[str, Any]] = []
        for path in sorted(
            [self.v1_path, self.v2_meta_path],
            key=lambda item: str(item),
        ):
            if not path.is_file():
                fail(
                    "E_IO",
                    "manifest source file is missing",
                    stage="parse",
                    path=str(path),
                )
            parts.append({"path": path.name, "digest": digest_file(path)})
        if not self.v2_shards_dir.is_dir():
            fail(
                "E_IO",
                "v2 shard directory is missing",
                stage="parse",
                path=str(self.v2_shards_dir),
            )
        for path in sorted(self.v2_shards_dir.glob("*.json"), key=lambda item: item.name):
            parts.append({"path": path.name, "digest": digest_file(path)})
        return digest_value(parts)

    def load(
        self,
        *,
        clock_ms: int,
        injections: InjectionController,
    ) -> LoadedManifests:
        fingerprint = self.source_fingerprint()
        v1, v1_entries = self._load_v1(clock_ms=clock_ms, injections=injections)
        v2, v2_entries = self._load_v2(injections)
        entries = sorted(
            v1_entries + v2_entries,
            key=lambda item: (
                self.expected_environments.index(item["environment"]),
                item["service"],
                item["source_priority"],
                item["candidate_id"],
            ),
        )
        parameter_digest = digest_value(
            [
                {
                    "environment": entry["environment"],
                    "service": entry["service"],
                    "source_version": entry["source_version"],
                    "params": entry["params"],
                    "tombstone": entry["tombstone"],
                }
                for entry in entries
            ]
        )
        payload = {
            "clock_ms": clock_ms,
            "v1": v1,
            "v2": v2,
            "entries": entries,
            "source_fingerprint": fingerprint,
            "descriptor": {
                "manifest_version": v2["manifest_version"],
                "generation_min": v2["generation_min"],
                "shard_hashes": v2["shard_hashes"],
                "parameter_digest": parameter_digest,
                "input_digest": digest_value(
                    {
                        "source_fingerprint": fingerprint,
                        "v1_manifest_version": v1["manifest_version"],
                    }
                ),
            },
        }
        return LoadedManifests(payload=payload, source_fingerprint=fingerprint)

    def _load_v1(
        self,
        *,
        clock_ms: int,
        injections: InjectionController,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            raw = yaml.load(
                self.v1_path.read_text(encoding="utf-8"),
                Loader=UniqueKeyLoader,
            )
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            fail(
                "E_BAD_MANIFEST",
                "v1 YAML is malformed",
                stage="parse",
                path=str(self.v1_path),
                line=(mark.line + 1) if mark is not None else None,
                column=(mark.column + 1) if mark is not None else None,
                reason=getattr(exc, "problem", str(exc)),
            )
        except OSError as exc:
            fail(
                "E_IO",
                "unable to read v1 manifest",
                stage="parse",
                path=str(self.v1_path),
                reason=str(exc),
            )
        if not isinstance(raw, dict):
            fail(
                "E_BAD_MANIFEST",
                "v1 manifest root must be an object",
                stage="parse",
                path=str(self.v1_path),
            )
        raw = copy.deepcopy(raw)
        malformed_injection = injections.find("parse", "malformed_yaml_field")
        if malformed_injection is not None:
            environment = malformed_injection.parameters.get("environment")
            service = malformed_injection.parameters.get("service")
            field = malformed_injection.parameters.get("field", "timeout_ms")
            replacement = malformed_injection.parameters.get("value", -1)
            try:
                services = raw["environments"][environment]["services"]
                target = next(item for item in services if item.get("name") == service)
                target[field] = replacement
                if field in target.get("params", {}):
                    target["params"][field] = replacement
            except (KeyError, StopIteration, TypeError, AttributeError):
                fail(
                    "E_BAD_MANIFEST",
                    "malformed_yaml_field injection target was not found",
                    stage="parse",
                    injection=malformed_injection.id,
                    **malformed_injection.parameters,
                )
        if raw.get("api_version") != "deployment-manifest/v1":
            fail(
                "E_BAD_MANIFEST",
                "unsupported v1 api_version",
                stage="parse",
                path=str(self.v1_path),
                value=raw.get("api_version"),
            )
        manifest_version = validate_manifest_version(
            raw.get("manifest_version"),
            stage="parse",
            source=str(self.v1_path),
        )
        scale_ms = raw.get("clock_scale_ms", 1000)
        if not is_int(scale_ms) or scale_ms <= 0:
            fail(
                "E_BAD_MANIFEST",
                "clock_scale_ms must be a positive integer",
                stage="parse",
                value=scale_ms,
            )
        rounding = raw.get("rounding", "floor")
        if rounding not in {"floor", "ceil", "nearest", "toward_zero"}:
            fail(
                "E_BAD_MANIFEST",
                "rounding must be floor, ceil, nearest or toward_zero",
                stage="parse",
                value=rounding,
            )
        schema = validate_parameter_type_schema(
            raw.get("parameter_types"),
            stage="parse",
            source=str(self.v1_path),
        )
        default_ttl_ms = validate_nonnegative_int(
            raw.get("ttl_ms", 0),
            "ttl_ms",
            stage="parse",
            location={"path": str(self.v1_path)},
        )
        environments = raw.get("environments")
        if not isinstance(environments, dict) or not environments:
            fail(
                "E_BAD_MANIFEST",
                "v1 environments must be a non-empty object",
                stage="parse",
                path=str(self.v1_path),
            )
        unexpected_envs = sorted(set(environments) - set(self.expected_environments))
        if unexpected_envs:
            fail(
                "E_BAD_MANIFEST",
                "v1 manifest contains unexpected environments",
                stage="parse",
                environments=unexpected_envs,
            )
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for environment_name in self.expected_environments:
            environment = environments.get(environment_name)
            if not isinstance(environment, dict):
                fail(
                    "E_BAD_MANIFEST",
                    "v1 manifest is missing a declared environment",
                    stage="parse",
                    environment=environment_name,
                )
            logical_clock = environment.get("logical_clock")
            if logical_clock is not None and (
                not is_number(logical_clock) or logical_clock < 0
            ):
                fail(
                    "E_BAD_MANIFEST",
                    "v1 environment logical_clock must be a finite non-negative number",
                    stage="parse",
                    environment=environment_name,
                    value=logical_clock,
                )
            if logical_clock is None:
                effective_at_ms = clock_ms
                original_time = {
                    "kind": "injected_clock_ms",
                    "unit": "milliseconds",
                    "raw": str(clock_ms),
                    "scale_ms": 1,
                    "rounding": "floor",
                    "derived_ms": clock_ms,
                }
            else:
                effective_at_ms = scaled_milliseconds(logical_clock, scale_ms, rounding)
                original_time = {
                    "kind": "scaled_logical_time",
                    "unit": "seconds",
                    "raw": str(logical_clock),
                    "scale_ms": scale_ms,
                    "rounding": rounding,
                    "derived_ms": effective_at_ms,
                }
            environment_ttl = environment.get("ttl_ms", default_ttl_ms)
            validate_nonnegative_int(
                environment_ttl,
                "ttl_ms",
                stage="parse",
                location={"environment": environment_name},
            )
            services = environment.get("services")
            if not isinstance(services, list) or not services:
                fail(
                    "E_BAD_MANIFEST",
                    "v1 environment services must be a non-empty list",
                    stage="parse",
                    environment=environment_name,
                )
            for service in services:
                if not isinstance(service, dict):
                    fail(
                        "E_BAD_MANIFEST",
                        "v1 service entry must be an object",
                        stage="parse",
                        environment=environment_name,
                    )
                name = validate_service_name(
                    service.get("name"),
                    stage="parse",
                    location={"environment": environment_name},
                )
                if (environment_name, name) in seen:
                    fail(
                        "E_BAD_MANIFEST",
                        "service name must be unique within a v1 environment",
                        stage="parse",
                        environment=environment_name,
                        service=name,
                    )
                seen.add((environment_name, name))
                params = validate_params(
                    service.get("params"),
                    schema,
                    stage="parse",
                    location={"environment": environment_name, "service": name},
                )
                for field in CORE_PARAMETERS:
                    if field not in service:
                        fail(
                            "E_BAD_MANIFEST",
                            "v1 service is missing a declared core field",
                            stage="parse",
                            environment=environment_name,
                            service=name,
                            field=field,
                        )
                    if canonical_json(service[field]) != canonical_json(params[field]):
                        fail(
                            "E_BAD_MANIFEST",
                            "v1 flat params disagree with service fields",
                            stage="parse",
                            environment=environment_name,
                            service=name,
                            field=field,
                            service_value=service[field],
                            params_value=params[field],
                        )
                validated_resource = validate_resource_limit(
                    params["resource_limit"],
                    stage="parse",
                    location={"environment": environment_name, "service": name},
                )
                validate_retry_factor(
                    params["retry_factor"],
                    stage="parse",
                    location={"environment": environment_name, "service": name},
                )
                ttl_ms = validate_nonnegative_int(
                    service.get("ttl_ms", environment_ttl),
                    "ttl_ms",
                    stage="parse",
                    location={"environment": environment_name, "service": name},
                )
                candidate_id = digest_value(
                    {
                        "source": "v1",
                        "environment": environment_name,
                        "service": name,
                        "source_file": self.v1_path.name,
                        "effective_at_ms": effective_at_ms,
                    }
                )[:24]
                entries.append(
                    {
                        "candidate_id": candidate_id,
                        "source_version": "v1",
                        "source_priority": 1,
                        "source_file": f"v1/{self.v1_path.name}",
                        "environment": environment_name,
                        "service": name,
                        "effective_at_ms": effective_at_ms,
                        "generation": 0,
                        "params": copy.deepcopy(params),
                        "upstreams": [],
                        "ttl_ms": ttl_ms,
                        "tombstone": False,
                        "original_time": original_time,
                        "compatibility_defaults": [],
                        "resource_limit": validated_resource,
                    }
                )
        v1_meta = {
            "manifest_version": manifest_version,
            "source_file": str(self.v1_path),
            "source_digest": digest_file(self.v1_path),
            "clock_scale_ms": scale_ms,
            "rounding": rounding,
            "parameter_types": schema,
            "entry_count": len(entries),
        }
        return v1_meta, entries

    def _load_v2(
        self,
        injections: InjectionController,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            uri = f"{self.v2_meta_path.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            meta_rows = connection.execute(
                "SELECT manifest_version, generation_min, created_at_ms, parameter_schema_json, "
                "compatibility_defaults_json "
                "FROM package_meta"
            ).fetchall()
            shard_rows = connection.execute(
                "SELECT shard_id, relative_path, digest, generation, environment, priority "
                "FROM shards ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            fail(
                "E_BAD_MANIFEST",
                "v2 metadata database cannot be read",
                stage="parse",
                path=str(self.v2_meta_path),
                reason=str(exc),
            )
        finally:
            if "connection" in locals():
                connection.close()
        if len(meta_rows) != 1:
            fail(
                "E_BAD_MANIFEST",
                "v2 metadata must contain exactly one package_meta row",
                stage="parse",
                path=str(self.v2_meta_path),
                row_count=len(meta_rows),
            )
        meta = meta_rows[0]
        manifest_version = validate_manifest_version(
            meta["manifest_version"],
            stage="parse",
            source=str(self.v2_meta_path),
        )
        generation_min = meta["generation_min"]
        if not is_int(generation_min) or generation_min < 0:
            fail(
                "E_BAD_MANIFEST",
                "v2 generation_min must be a non-negative integer",
                stage="parse",
                value=generation_min,
            )
        created_at_ms = meta["created_at_ms"]
        if not is_int(created_at_ms) or created_at_ms < 0:
            fail(
                "E_BAD_MANIFEST",
                "v2 created_at_ms must be a non-negative integer millisecond timestamp",
                stage="parse",
                value=created_at_ms,
            )
        try:
            schema = json.loads(meta["parameter_schema_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            fail(
                "E_BAD_MANIFEST",
                "v2 parameter_schema_json is malformed",
                stage="parse",
                reason=str(exc),
            )
        schema = validate_parameter_type_schema(
            schema,
            stage="parse",
            source=str(self.v2_meta_path),
        )
        try:
            compatibility_defaults = json.loads(meta["compatibility_defaults_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            fail(
                "E_BAD_MANIFEST",
                "v2 compatibility_defaults_json is malformed",
                stage="parse",
                reason=str(exc),
            )
        if not isinstance(compatibility_defaults, dict):
            fail(
                "E_BAD_MANIFEST",
                "v2 compatibility defaults must be an object",
                stage="parse",
            )
        unknown_defaults = sorted(set(compatibility_defaults) - set(schema))
        if unknown_defaults:
            fail(
                "E_BAD_MANIFEST",
                "v2 compatibility defaults reference undeclared parameters",
                stage="parse",
                parameters=unknown_defaults,
            )
        if not shard_rows:
            fail(
                "E_BAD_MANIFEST",
                "v2 metadata contains no shards",
                stage="parse",
            )
        entries: list[dict[str, Any]] = []
        shard_hashes: dict[str, str] = {}
        seen_services: set[tuple[str, str]] = set()
        previous_generation: int | None = None
        for shard in shard_rows:
            shard_id = shard["shard_id"]
            if not isinstance(shard_id, str) or not shard_id:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard_id must be a non-empty string",
                    stage="parse",
                )
            if shard_id in shard_hashes:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard ids must be unique",
                    stage="parse",
                    shard_id=shard_id,
                )
            generation = shard["generation"]
            if not is_int(generation) or generation < generation_min:
                fail(
                    "E_GENERATION_CONFLICT",
                    "v2 shard generation is below generation_min",
                    stage="parse",
                    shard_id=shard_id,
                    generation=generation,
                    generation_min=generation_min,
                )
            if previous_generation is not None and generation < previous_generation:
                fail(
                    "E_GENERATION_CONFLICT",
                    "v2 shard generations must be monotonic in metadata order",
                    stage="parse",
                    shard_id=shard_id,
                    generation=generation,
                    previous_generation=previous_generation,
                )
            previous_generation = generation
            environment = shard["environment"]
            if environment not in self.expected_environments:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard environment is not declared",
                    stage="parse",
                    shard_id=shard_id,
                    environment=environment,
                )
            relative_path = shard["relative_path"]
            if not isinstance(relative_path, str) or not relative_path:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard relative_path must be a non-empty string",
                    stage="parse",
                    shard_id=shard_id,
                )
            shard_path = (self.v2_shards_dir / relative_path).resolve()
            try:
                shard_path.relative_to(self.v2_shards_dir)
            except ValueError:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard path escapes the shard directory",
                    stage="parse",
                    shard_id=shard_id,
                    relative_path=relative_path,
                )
            if not shard_path.is_file():
                fail(
                    "E_IO",
                    "v2 shard file is missing",
                    stage="parse",
                    shard_id=shard_id,
                    path=str(shard_path),
                )
            actual_digest = digest_file(shard_path)
            expected_digest = normalize_digest(shard["digest"])
            mismatch_injection = injections.find("parse", "shard_digest_mismatch")
            if mismatch_injection is not None:
                expected_digest = "0" * 64
            if actual_digest != expected_digest:
                fail(
                    "E_DIGEST_MISMATCH",
                    "v2 shard digest does not match metadata",
                    stage="parse",
                    shard_id=shard_id,
                    path=str(shard_path),
                    expected=expected_digest,
                    actual=actual_digest,
                    injection=(
                        mismatch_injection.id if mismatch_injection is not None else None
                    ),
                )
            shard_hashes[shard_id] = actual_digest
            try:
                shard_payload = json.loads(shard_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard JSON is malformed",
                    stage="parse",
                    shard_id=shard_id,
                    path=str(shard_path),
                    reason=str(exc),
                )
            if not isinstance(shard_payload, dict):
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard root must be an object",
                    stage="parse",
                    shard_id=shard_id,
                )
            if shard_payload.get("shard_id") != shard_id:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard_id disagrees with metadata",
                    stage="parse",
                    expected=shard_id,
                    actual=shard_payload.get("shard_id"),
                )
            if shard_payload.get("environment") != environment:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard environment disagrees with metadata",
                    stage="parse",
                    shard_id=shard_id,
                    expected=environment,
                    actual=shard_payload.get("environment"),
                )
            if shard_payload.get("generation") != generation:
                fail(
                    "E_GENERATION_CONFLICT",
                    "v2 shard generation disagrees with metadata",
                    stage="parse",
                    shard_id=shard_id,
                    expected=generation,
                    actual=shard_payload.get("generation"),
                )
            if shard_payload.get("manifest_version") != manifest_version:
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard manifest_version disagrees with metadata",
                    stage="parse",
                    shard_id=shard_id,
                    expected=manifest_version,
                    actual=shard_payload.get("manifest_version"),
                )
            services = shard_payload.get("services")
            if not isinstance(services, list):
                fail(
                    "E_BAD_MANIFEST",
                    "v2 shard services must be a list",
                    stage="parse",
                    shard_id=shard_id,
                )
            for raw_service in services:
                if not isinstance(raw_service, dict):
                    fail(
                        "E_BAD_MANIFEST",
                        "v2 service entry must be an object",
                        stage="parse",
                        shard_id=shard_id,
                    )
                name = validate_service_name(
                    raw_service.get("name"),
                    stage="parse",
                    location={"shard_id": shard_id, "environment": environment},
                )
                if (environment, name) in seen_services:
                    fail(
                        "E_BAD_MANIFEST",
                        "v2 service name must be unique within an environment",
                        stage="parse",
                        environment=environment,
                        service=name,
                    )
                seen_services.add((environment, name))
                effective_at_ms = raw_service.get("effective_at_ms")
                if not is_int(effective_at_ms) or effective_at_ms < 0:
                    fail(
                        "E_BAD_MANIFEST",
                        "v2 effective_at_ms must be a non-negative integer millisecond timestamp",
                        stage="parse",
                        environment=environment,
                        service=name,
                        value=effective_at_ms,
                    )
                entry_generation = raw_service.get("generation")
                if not is_int(entry_generation) or entry_generation != generation:
                    fail(
                        "E_GENERATION_CONFLICT",
                        "v2 entry generation must equal its shard generation",
                        stage="parse",
                        environment=environment,
                        service=name,
                        shard_generation=generation,
                        entry_generation=entry_generation,
                    )
                tombstone = raw_service.get("tombstone", False)
                if not isinstance(tombstone, bool):
                    fail(
                        "E_BAD_MANIFEST",
                        "v2 tombstone must be boolean",
                        stage="parse",
                        environment=environment,
                        service=name,
                    )
                if tombstone:
                    if raw_service.get("params") not in ({}, None):
                        fail(
                            "E_BAD_MANIFEST",
                            "v2 tombstone must not carry parameters",
                            stage="parse",
                            environment=environment,
                            service=name,
                        )
                    params: dict[str, Any] = {}
                    upstreams: list[str] = []
                    ttl_ms = 0
                else:
                    params = validate_params(
                        raw_service.get("params"),
                        schema,
                        stage="parse",
                        location={"environment": environment, "service": name},
                    )
                    upstreams_raw = raw_service.get("upstreams", [])
                    if not isinstance(upstreams_raw, list) or any(
                        not isinstance(item, str) or not item for item in upstreams_raw
                    ):
                        fail(
                            "E_BAD_MANIFEST",
                            "v2 upstreams must be a list of service names",
                            stage="parse",
                            environment=environment,
                            service=name,
                        )
                    if len(set(upstreams_raw)) != len(upstreams_raw):
                        fail(
                            "E_BAD_MANIFEST",
                            "v2 upstream service names must be unique",
                            stage="parse",
                            environment=environment,
                            service=name,
                        )
                    upstreams = sorted(upstreams_raw)
                    for upstream in upstreams:
                        validate_service_name(
                            upstream,
                            stage="parse",
                            location={"environment": environment, "service": name},
                        )
                    ttl_ms = validate_nonnegative_int(
                        raw_service.get("ttl_ms", 0),
                        "ttl_ms",
                        stage="parse",
                        location={"environment": environment, "service": name},
                    )
                candidate_id = digest_value(
                    {
                        "source": "v2",
                        "environment": environment,
                        "service": name,
                        "source_file": relative_path,
                        "effective_at_ms": effective_at_ms,
                        "generation": entry_generation,
                    }
                )[:24]
                entries.append(
                    {
                        "candidate_id": candidate_id,
                        "source_version": "v2",
                        "source_priority": 2,
                        "source_file": f"v2/{relative_path}",
                        "shard_id": shard_id,
                        "environment": environment,
                        "service": name,
                        "effective_at_ms": effective_at_ms,
                        "generation": entry_generation,
                        "params": copy.deepcopy(params),
                        "upstreams": upstreams,
                        "ttl_ms": ttl_ms,
                        "tombstone": tombstone,
                        "original_time": {
                            "kind": "absolute_ms",
                            "raw": str(effective_at_ms),
                            "value": effective_at_ms,
                        },
                        "compatibility_defaults": [],
                    }
                )
        v2_meta = {
            "manifest_version": manifest_version,
            "generation_min": generation_min,
            "created_at_ms": created_at_ms,
            "meta_file": str(self.v2_meta_path),
            "meta_digest": digest_file(self.v2_meta_path),
            "parameter_schema": schema,
            "compatibility_defaults": compatibility_defaults,
            "shard_hashes": shard_hashes,
            "entry_count": len(entries),
        }
        return v2_meta, entries
