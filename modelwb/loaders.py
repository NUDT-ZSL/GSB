from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, read_json, sha256_bytes, sha256_file, sha256_json
from .domain import (
    Manifest,
    SHA256_RE,
    normalise_dependencies,
    parse_layers,
    validate_manifest_order,
)
from .errors import WorkbenchError, fail
from .timeconv import absolute_time_provenance, convert_relative_time


SOURCE_PRIORITY = {"v1": 10, "v2": 20, "full-replacement": 0, "patch": 0}


@dataclass
class RegistryBundle:
    configs: dict[str, dict[str, Any]]
    feature_tables: dict[str, dict[str, Any]]
    upstream_models: dict[str, dict[str, Any]]

    def merge(self, other: "RegistryBundle", *, source: str) -> None:
        for target, candidate in (
            (self.configs, other.configs),
            (self.feature_tables, other.feature_tables),
            (self.upstream_models, other.upstream_models),
        ):
            for name, definition in candidate.items():
                if name in target and canonical_json_bytes(target[name]) != canonical_json_bytes(definition):
                    fail(
                        "CONFLICT",
                        "registry definition differs between sources",
                        registry=source,
                        name=name,
                    )
                target[name] = definition

    def to_dict(self) -> dict[str, Any]:
        return {
            "configs": self.configs,
            "feature_tables": self.feature_tables,
            "upstream_models": self.upstream_models,
        }


@dataclass
class Catalog:
    v1_versions: tuple[Manifest, ...]
    v2_versions: tuple[Manifest, ...]
    registries: RegistryBundle
    input_digest: str
    sources: dict[str, str]

    @property
    def all_versions(self) -> tuple[Manifest, ...]:
        return self.v1_versions + self.v2_versions

    def version_ids(self) -> set[str]:
        return {manifest.version for manifest in self.all_versions}

    def manifest(self, version: str, source_format: str | None = None) -> Manifest:
        candidates = [
            manifest
            for manifest in self.all_versions
            if manifest.version == version
            and (source_format is None or manifest.source_format == source_format)
        ]
        if not candidates:
            fail(
                "NOT_FOUND",
                "model version is not present in the loaded catalog",
                version=version,
                source_format=source_format,
            )
        return max(candidates, key=lambda item: _candidate_key(item))

    def validate_references(self) -> None:
        version_ids = self.version_ids()
        tombstones = {
            manifest.version for manifest in self.all_versions if manifest.tombstone
        }
        self._validate_config_registry(version_ids, tombstones)
        for manifest in self.all_versions:
            if manifest.tombstone:
                continue
            self._require_config(manifest.preprocess, expected_kind="preprocess", version=manifest.version)
            self._require_config(manifest.postprocess, expected_kind="postprocess", version=manifest.version)
            for feature in manifest.feature_tables:
                if feature not in self.registries.feature_tables:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "feature table reference is not resolvable",
                        version=manifest.version,
                        source=manifest.source_format,
                        kind="feature_table",
                        reference=feature,
                    )
            for upstream in manifest.upstream_models:
                if upstream in tombstones:
                    fail(
                        "DEPENDENCY_TOMBSTONED",
                        "model version references a tombstoned upstream",
                        version=manifest.version,
                        source=manifest.source_format,
                        upstream=upstream,
                    )
                if upstream not in version_ids and upstream not in self.registries.upstream_models:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "upstream model reference is not resolvable",
                        version=manifest.version,
                        source=manifest.source_format,
                        kind="upstream_model",
                        reference=upstream,
                    )

    def _validate_config_registry(
        self, version_ids: set[str], tombstones: set[str]
    ) -> None:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(config_id: str, trail: tuple[str, ...]) -> None:
            definition = self.registries.configs.get(config_id)
            if definition is None:
                fail(
                    "CONFIG_REFERENCE_MISSING",
                    "configuration reference is not resolvable",
                    config=config_id,
                    trail=list(trail),
                )
            mark = state.get(config_id, 0)
            if mark == 1:
                cycle_start = stack.index(config_id)
                cycle = stack[cycle_start:] + [config_id]
                fail(
                    "CONFIG_REFERENCE_CYCLE",
                    "configuration reference graph contains a cycle",
                    cycle=cycle,
                )
            if mark == 2:
                return
            state[config_id] = 1
            stack.append(config_id)
            for reference in _config_definition_references(definition):
                visit(reference, trail + (config_id,))
            stack.pop()
            state[config_id] = 2
            for feature in definition.get("feature_tables", []):
                if feature not in self.registries.feature_tables:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "configuration feature table reference is not resolvable",
                        config=config_id,
                        kind="feature_table",
                        reference=feature,
                    )
            for upstream in definition.get("upstream_models", []):
                if upstream in tombstones:
                    fail(
                        "DEPENDENCY_TOMBSTONED",
                        "configuration references a tombstoned model version",
                        config=config_id,
                        kind="upstream_model",
                        reference=upstream,
                    )
                if upstream not in self.registries.upstream_models and upstream not in version_ids:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "configuration upstream model reference is not resolvable",
                        config=config_id,
                        kind="upstream_model",
                        reference=upstream,
                    )

        for config_id in sorted(self.registries.configs):
            visit(config_id, ())
        for feature_id, definition in self.registries.feature_tables.items():
            for reference in definition.get("feature_tables", []):
                if reference not in self.registries.feature_tables:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "feature table dependency is not resolvable",
                        feature_table=feature_id,
                        reference=reference,
                    )
            for upstream in definition.get("upstream_models", []):
                if upstream in tombstones:
                    fail(
                        "DEPENDENCY_TOMBSTONED",
                        "feature table references a tombstoned model version",
                        feature_table=feature_id,
                        reference=upstream,
                    )
                if upstream not in self.registries.upstream_models and upstream not in version_ids:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "feature table upstream reference is not resolvable",
                        feature_table=feature_id,
                        reference=upstream,
                    )
        for upstream_id, definition in self.registries.upstream_models.items():
            for feature in definition.get("feature_tables", []):
                if feature not in self.registries.feature_tables:
                    fail(
                        "DEPENDENCY_REFERENCE_MISSING",
                        "upstream model feature table reference is not resolvable",
                        upstream_model=upstream_id,
                        reference=feature,
                    )

    def _require_config(self, config_id: str, *, expected_kind: str, version: str) -> None:
        definition = self.registries.configs.get(config_id)
        if definition is None:
            fail(
                "CONFIG_REFERENCE_MISSING",
                "model configuration reference is not resolvable",
                version=version,
                config=config_id,
                expected_kind=expected_kind,
            )
        actual_kind = definition.get("kind")
        if actual_kind and actual_kind != expected_kind:
            fail(
                "CONFIG_REFERENCE_MISSING",
                "model configuration reference points to the wrong config kind",
                version=version,
                config=config_id,
                expected_kind=expected_kind,
                actual_kind=actual_kind,
            )

    def config_closure(self, config_id: str) -> list[str]:
        result: list[str] = []
        visited: set[str] = set()

        def visit(current: str) -> None:
            if current in visited:
                return
            definition = self.registries.configs.get(current)
            if definition is None:
                fail(
                    "CONFIG_REFERENCE_MISSING",
                    "configuration reference is not resolvable",
                    config=current,
                )
            visited.add(current)
            result.append(current)
            for reference in _config_definition_references(definition):
                visit(reference)

        visit(config_id)
        return result

    def config_binding(self, manifest: Manifest) -> str:
        ids = self.config_closure(manifest.preprocess) + self.config_closure(manifest.postprocess)
        payload = {
            "preprocess": manifest.preprocess,
            "postprocess": manifest.postprocess,
            "configs": {
                config_id: self.registries.configs[config_id]
                for config_id in sorted(set(ids))
            },
        }
        return sha256_json(payload)

    def unified_view(self) -> dict[str, Any]:
        grouped: dict[str, list[Manifest]] = {}
        for manifest in self.all_versions:
            grouped.setdefault(manifest.version, []).append(manifest)
        entries: list[dict[str, Any]] = []
        for version, candidates in grouped.items():
            ordered = sorted(candidates, key=_candidate_key)
            winner = ordered[-1]
            entries.append(
                {
                    "version": version,
                    "winner_source": winner.source_format,
                    "absolute_updated_ms": winner.absolute_updated_ms,
                    "logical_clock": winner.logical_clock,
                    "generation": winner.generation,
                    "revision": winner.revision,
                    "tombstone": winner.tombstone,
                    "active": not winner.tombstone,
                    "directory_digest": winner.directory_digest(),
                    "shard_chain": winner.shard_chain(),
                    "preprocess": winner.preprocess,
                    "postprocess": winner.postprocess,
                    "feature_tables": list(winner.feature_tables),
                    "upstream_models": list(winner.upstream_models),
                    "config_binding": self.config_binding(winner) if not winner.tombstone else None,
                    "time_interop": winner.time_interop(),
                    "candidates": [
                        {
                            "source_format": item.source_format,
                            "absolute_updated_ms": item.absolute_updated_ms,
                            "logical_clock": item.logical_clock,
                            "generation": item.generation,
                            "revision": item.revision,
                            "raw_time": item.raw_time,
                            "time_provenance": item.time_provenance,
                            "selected": item is winner,
                        }
                        for item in ordered
                    ],
                }
            )
        entries.sort(
            key=lambda item: (
                item["absolute_updated_ms"],
                item["logical_clock"],
                item["version"],
            )
        )
        return {
            "versions": entries,
            "active_versions": [item["version"] for item in entries if item["active"]],
            "tombstones": [item["version"] for item in entries if item["tombstone"]],
            "source_priority": SOURCE_PRIORITY,
            "input_digest": self.input_digest,
            "sources": self.sources,
        }


def _candidate_key(manifest: Manifest) -> tuple[Any, ...]:
    return (
        manifest.logical_clock,
        manifest.generation,
        SOURCE_PRIORITY.get(manifest.source_format, 0),
        manifest.absolute_updated_ms,
        manifest.revision,
        manifest.canonical_id,
    )


def _config_definition_references(definition: dict[str, Any]) -> list[str]:
    refs = definition.get("references", definition.get("configs", []))
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        fail("VALIDATION_ERROR", "config references must be a list", definition=definition)
    result = []
    for reference in refs:
        if not isinstance(reference, str) or not reference:
            fail("VALIDATION_ERROR", "config reference must be a non-empty string", reference=reference)
        result.append(reference)
    return result


def _normalise_registries(raw: Any) -> RegistryBundle:
    if raw is None:
        return RegistryBundle({}, {}, {})
    if not isinstance(raw, dict):
        fail("VALIDATION_ERROR", "registries must be an object")
    configs_raw = raw.get("configs", {})
    if not isinstance(configs_raw, dict):
        fail("VALIDATION_ERROR", "configs registry must be an object")
    combined_configs: dict[str, Any] = {}
    for key in ("preprocess", "postprocess", "shared"):
        section = configs_raw.get(key)
        if isinstance(section, dict):
            for config_id, definition in section.items():
                item = dict(definition) if isinstance(definition, dict) else {"definition": definition}
                item.setdefault("kind", "preprocess" if key == "preprocess" else "postprocess" if key == "postprocess" else "shared")
                combined_configs[config_id] = item
    for config_id, definition in configs_raw.items():
        if config_id in ("preprocess", "postprocess", "shared"):
            continue
        if not isinstance(definition, dict):
            fail("VALIDATION_ERROR", "config definition must be an object", config=config_id)
        combined_configs[config_id] = dict(definition)
    configs: dict[str, dict[str, Any]] = {}
    for config_id, definition in combined_configs.items():
        if not isinstance(config_id, str) or not config_id:
            fail("VALIDATION_ERROR", "config id must be a non-empty string")
        if not isinstance(definition, dict):
            fail("VALIDATION_ERROR", "config definition must be an object", config=config_id)
        config = dict(definition)
        config.setdefault("references", [])
        config.setdefault("feature_tables", [])
        config.setdefault("upstream_models", [])
        configs[config_id] = config
    feature_tables = _definition_map(raw.get("feature_tables", {}), "feature_tables")
    upstream_models = _definition_map(raw.get("upstream_models", {}), "upstream_models")
    return RegistryBundle(configs=configs, feature_tables=feature_tables, upstream_models=upstream_models)


def _definition_map(raw: Any, field_name: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        fail("VALIDATION_ERROR", f"{field_name} registry must be an object", field=field_name)
    result: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name:
            fail("VALIDATION_ERROR", f"{field_name} key must be a non-empty string")
        if isinstance(definition, dict):
            result[name] = dict(definition)
        else:
            result[name] = {"value": definition}
    return result


def _v1_time_config(data: dict[str, Any]) -> tuple[int, Any, str]:
    time_config = data.get("time", {})
    if time_config is None:
        time_config = {}
    if not isinstance(time_config, dict):
        fail("TIME_CONVERSION_INVALID", "v1 time configuration must be an object")
    base_ms = data.get("time_base_ms", time_config.get("base_ms"))
    scale_ms = data.get("time_scale_ms", time_config.get("scale_ms", "1"))
    rounding = data.get("time_rounding", time_config.get("rounding", "half_up"))
    if base_ms is None:
        fail("TIME_CONVERSION_INVALID", "v1 manifest must declare a base timestamp")
    if not isinstance(rounding, str):
        fail("TIME_CONVERSION_INVALID", "time rounding must be a string")
    return base_ms, scale_ms, rounding


def _parse_v1_manifest(
    entry: dict[str, Any],
    *,
    index: int,
    source_path: Path,
    base_ms: int,
    scale_ms: Any,
    rounding: str,
) -> Manifest:
    path = f"{source_path}:versions[{index}]"
    if not isinstance(entry, dict):
        fail("VALIDATION_ERROR", "v1 version entry must be an object", path=path)
    version = entry.get("version", entry.get("id"))
    if not isinstance(version, str) or not version:
        fail("VALIDATION_ERROR", "version must be a non-empty string", path=path)
    relative = entry.get(
        "updated_relative",
        entry.get("updated_relative_ms", entry.get("relative_updated_ms")),
    )
    if relative is None:
        fail("VALIDATION_ERROR", "v1 version is missing updated_relative", path=path, version=version)
    absolute, provenance = convert_relative_time(
        relative, base_ms=base_ms, scale_ms=scale_ms, rounding=rounding
    )
    logical_clock = entry.get("logical_clock", 0)
    generation = entry.get("generation", 1)
    if not isinstance(logical_clock, int) or isinstance(logical_clock, bool) or logical_clock < 0:
        fail("VALIDATION_ERROR", "logical_clock must be a non-negative integer", path=path)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        fail("VALIDATION_ERROR", "generation must be a non-negative integer", path=path)
    dependencies = normalise_dependencies(entry.get("dependencies"), path=path)
    preprocess = entry.get("preprocess", entry.get("preprocess_config"))
    postprocess = entry.get("postprocess", entry.get("postprocess_config"))
    if not isinstance(preprocess, str) or not preprocess:
        fail("VALIDATION_ERROR", "v1 preprocess reference is missing", path=path, version=version)
    if not isinstance(postprocess, str) or not postprocess:
        fail("VALIDATION_ERROR", "v1 postprocess reference is missing", path=path, version=version)
    return Manifest(
        version=version,
        source_format="v1",
        revision=_entry_revision(entry, index),
        logical_clock=logical_clock,
        generation=generation,
        absolute_updated_ms=absolute,
        raw_time={"kind": "relative", "value": str(relative)},
        time_provenance=provenance,
        layers=parse_layers(entry.get("layers"), path=path),
        preprocess=preprocess,
        postprocess=postprocess,
        feature_tables=dependencies["feature_tables"],
        upstream_models=dependencies["upstream_models"],
        tombstone=False,
        canonical_id=str(entry.get("canonical_id", f"v1:{version}")),
        patch_digest=str(entry.get("patch_digest", "")),
        source_path=str(source_path),
        source_dir=str(source_path.parent),
    )


def _entry_revision(entry: dict[str, Any], index: int) -> int:
    for key in ("revision", "sequence", "version_number"):
        if key in entry:
            value = entry[key]
            if not isinstance(value, int) or isinstance(value, bool):
                fail("VALIDATION_ERROR", f"{key} must be an integer", value=value)
            return value
    return index


def load_v1(path: str | Path) -> tuple[tuple[Manifest, ...], RegistryBundle, str]:
    source_path = Path(path).resolve()
    try:
        data = read_json(source_path)
    except FileNotFoundError:
        fail("NOT_FOUND", "v1 manifest file does not exist", path=str(source_path))
    except (OSError, json.JSONDecodeError) as exc:
        fail("IO_ERROR", "cannot read v1 manifest", path=str(source_path), detail=str(exc))
    if not isinstance(data, dict) or data.get("format", "v1") != "v1":
        fail("VALIDATION_ERROR", "not a v1 manifest", path=str(source_path))
    versions_raw = data.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        fail("VALIDATION_ERROR", "v1 manifest must contain versions", path=str(source_path))
    base_ms, scale_ms, rounding = _v1_time_config(data)
    seen: set[str] = set()
    versions: list[Manifest] = []
    for index, entry in enumerate(versions_raw):
        manifest = _parse_v1_manifest(
            entry,
            index=index,
            source_path=source_path,
            base_ms=base_ms,
            scale_ms=scale_ms,
            rounding=rounding,
        )
        if manifest.version in seen:
            fail(
                "VERSION_DUPLICATE",
                "version is duplicated within a v1 source",
                version=manifest.version,
                path=str(source_path),
            )
        seen.add(manifest.version)
        versions.append(manifest)
    validate_manifest_order(versions, source="v1")
    registries = _normalise_registries(data.get("registries", {}))
    digest = sha256_file(source_path)
    return tuple(versions), registries, digest


def _open_v2_db(path: str | Path) -> sqlite3.Connection:
    source = Path(path).resolve()
    if not source.exists():
        fail("NOT_FOUND", "v2 metadata database does not exist", path=str(source))
    try:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.Error as exc:
        fail("IO_ERROR", "cannot open v2 metadata database", path=str(source), detail=str(exc))


def load_v2(
    db_path: str | Path, v2_dir: str | Path
) -> tuple[tuple[Manifest, ...], RegistryBundle, str]:
    db_source = Path(db_path).resolve()
    directory = Path(v2_dir).resolve()
    connection = _open_v2_db(db_source)
    try:
        try:
            rows = connection.execute("SELECT * FROM versions ORDER BY revision, version").fetchall()
        except sqlite3.Error as exc:
            fail("VALIDATION_ERROR", "v2 metadata database lacks a valid versions table", detail=str(exc))
        registries = _load_v2_registries(connection)
        dependencies: dict[str, dict[str, list[str]]] = {}
        try:
            for row in connection.execute("SELECT version, kind, ref FROM dependencies"):
                dependencies.setdefault(row["version"], {}).setdefault(row["kind"], []).append(row["ref"])
        except sqlite3.Error:
            dependencies = {}
        seen: set[str] = set()
        versions: list[Manifest] = []
        for index, row in enumerate(rows):
            version = row["version"]
            path = f"{db_source}:versions[{index}]"
            if not isinstance(version, str) or not version:
                fail("VALIDATION_ERROR", "v2 version id is invalid", path=path)
            if version in seen:
                fail("VERSION_DUPLICATE", "version is duplicated within a v2 source", version=version)
            seen.add(version)
            shard_file = directory / row["shard_file"]
            if not shard_file.exists():
                fail(
                    "SHARD_MISSING",
                    "v2 shard JSON declared by metadata database is missing",
                    version=version,
                    path=str(shard_file),
                )
            actual_json_digest = sha256_file(shard_file)
            declared_json_digest = row["shard_json_sha256"]
            if declared_json_digest and actual_json_digest != declared_json_digest:
                fail(
                    "SHARD_DIGEST_MISMATCH",
                    "v2 shard JSON digest does not match metadata",
                    version=version,
                    expected=declared_json_digest,
                    actual=actual_json_digest,
                    path=str(shard_file),
                )
            try:
                shard_data = read_json(shard_file)
            except (OSError, json.JSONDecodeError) as exc:
                fail("IO_ERROR", "cannot parse v2 shard JSON", path=str(shard_file), detail=str(exc))
            if not isinstance(shard_data, dict) or shard_data.get("version") != version:
                fail(
                    "VALIDATION_ERROR",
                    "v2 shard JSON version does not match metadata",
                    version=version,
                    path=str(shard_file),
                )
            tombstone = bool(row["tombstone"])
            layers = () if tombstone else parse_layers(shard_data.get("layers"), path=str(shard_file))
            if not tombstone:
                _cross_validate_v2_shards(connection, version, layers)
            db_dependencies = dependencies.get(version, {})
            json_dependencies = shard_data.get("dependencies")
            if json_dependencies is not None:
                normalized_json_dependencies = normalise_dependencies(
                    json_dependencies, path=str(shard_file)
                )
                for kind in ("feature_tables", "upstream_models"):
                    if sorted(normalized_json_dependencies[kind]) != sorted(db_dependencies.get(kind, [])):
                        fail(
                            "VALIDATION_ERROR",
                            "v2 shard dependencies disagree with SQLite metadata",
                            version=version,
                            kind=kind,
                            json=list(normalized_json_dependencies[kind]),
                            sqlite=db_dependencies.get(kind, []),
                        )
            preprocess = str(row["preprocess"])
            postprocess = str(row["postprocess"])
            canonical_id = row["canonical_id"]
            if not isinstance(canonical_id, str) or not canonical_id:
                fail(
                    "VALIDATION_ERROR",
                    "v2 canonical_id must be a non-empty string",
                    version=version,
                )
            if not tombstone and (not preprocess or not postprocess):
                fail(
                    "VALIDATION_ERROR",
                    "v2 version must declare preprocess and postprocess references",
                    version=version,
                )
            revision = _sqlite_int(row["revision"], "revision", version=version, minimum=0)
            logical_clock = _sqlite_int(
                row["logical_clock"], "logical_clock", version=version, minimum=0
            )
            generation = _sqlite_int(
                row["generation"], "generation", version=version, minimum=0
            )
            if shard_data.get("preprocess") not in (None, preprocess):
                fail("VALIDATION_ERROR", "v2 preprocess disagrees with metadata", version=version)
            if shard_data.get("postprocess") not in (None, postprocess):
                fail("VALIDATION_ERROR", "v2 postprocess disagrees with metadata", version=version)
            absolute, provenance = absolute_time_provenance(row["absolute_updated_ms"])
            patch_digest = row["patch_digest"] or ""
            if patch_digest and not SHA256_RE.match(patch_digest):
                fail(
                    "VALIDATION_ERROR",
                    "v2 patch digest is not a SHA-256 digest",
                    version=version,
                    patch_digest=patch_digest,
                )
            versions.append(
                Manifest(
                    version=version,
                    source_format="v2",
                    revision=revision,
                    logical_clock=logical_clock,
                    generation=generation,
                    absolute_updated_ms=absolute,
                    raw_time={"kind": "absolute", "value": row["absolute_updated_ms"]},
                    time_provenance=provenance,
                    layers=layers,
                    preprocess=preprocess,
                    postprocess=postprocess,
                    feature_tables=tuple(db_dependencies.get("feature_tables", [])),
                    upstream_models=tuple(db_dependencies.get("upstream_models", [])),
                    tombstone=tombstone,
                    canonical_id=canonical_id,
                    patch_digest=str(patch_digest),
                    shard_json_sha256=actual_json_digest,
                    source_path=str(shard_file),
                    source_dir=str(directory),
                )
            )
        validate_manifest_order(versions, source="v2")
    finally:
        connection.close()
    digest = sha256_bytes(db_source.read_bytes() + b"\0" + _directory_digest(directory))
    return tuple(versions), registries, digest


def _directory_digest(directory: Path) -> bytes:
    if not directory.exists():
        return b""
    items = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        items.append(
            canonical_json_bytes(
                {
                    "path": str(path.relative_to(directory)),
                    "sha256": sha256_file(path),
                }
            )
        )
    return sha256_bytes(b"\n".join(items)).encode("ascii")


def _sqlite_int(value: Any, field: str, *, version: str, minimum: int) -> int:
    if isinstance(value, bool):
        fail(
            "VALIDATION_ERROR",
            f"v2 {field} must be an integer",
            version=version,
            value=value,
        )
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(
            "VALIDATION_ERROR",
            f"v2 {field} must be an integer",
            version=version,
            value=value,
        )
    if number < minimum:
        fail(
            "VALIDATION_ERROR",
            f"v2 {field} must be >= {minimum}",
            version=version,
            value=value,
        )
    return number


def _cross_validate_v2_shards(
    connection: sqlite3.Connection, version: str, layers: tuple[Any, ...]
) -> None:
    try:
        rows = connection.execute(
            "SELECT layer, shard_id, shard_index, shard_count, sha256, size, path"
            " FROM shards WHERE version=?",
            (version,),
        ).fetchall()
    except sqlite3.Error:
        return
    if not rows:
        return
    db_shards = {
        (row["layer"], row["shard_id"]): {
            "index": row["shard_index"],
            "shard_count": row["shard_count"],
            "sha256": str(row["sha256"]).lower(),
            "size": row["size"],
            "path": row["path"],
        }
        for row in rows
    }
    json_shards = {
        (layer.name, shard.shard_id): {
            "index": shard.index,
            "shard_count": shard.shard_count,
            "sha256": shard.sha256,
            "size": shard.size,
            "path": shard.path,
        }
        for layer in layers
        for shard in layer.shards
    }
    if set(db_shards) != set(json_shards):
        fail(
            "SHARD_MISSING",
            "v2 shard JSON and SQLite shard metadata declare different shard sets",
            version=version,
            json_only=sorted(set(json_shards) - set(db_shards)),
            sqlite_only=sorted(set(db_shards) - set(json_shards)),
        )
    for key in sorted(json_shards):
        if json_shards[key]["sha256"] != db_shards[key]["sha256"]:
            fail(
                "SHARD_DIGEST_MISMATCH",
                "v2 shard JSON and SQLite shard metadata have different digests",
                version=version,
                layer=key[0],
                shard_id=key[1],
                json=json_shards[key]["sha256"],
                sqlite=db_shards[key]["sha256"],
            )
        if (
            json_shards[key]["index"] != db_shards[key]["index"]
            or json_shards[key]["shard_count"] != db_shards[key]["shard_count"]
        ):
            fail(
                "SHARD_MISSING",
                "v2 shard JSON and SQLite shard metadata have different coverage indexes",
                version=version,
                layer=key[0],
                shard_id=key[1],
            )
        if json_shards[key]["size"] != db_shards[key]["size"]:
            fail(
                "SHARD_DIGEST_MISMATCH",
                "v2 shard JSON and SQLite shard metadata have different sizes",
                version=version,
                layer=key[0],
                shard_id=key[1],
                json=json_shards[key]["size"],
                sqlite=db_shards[key]["size"],
            )
        if json_shards[key]["path"] != db_shards[key]["path"]:
            fail(
                "VALIDATION_ERROR",
                "v2 shard JSON and SQLite shard metadata have different paths",
                version=version,
                layer=key[0],
                shard_id=key[1],
                json=json_shards[key]["path"],
                sqlite=db_shards[key]["path"],
            )


def _load_v2_registries(connection: sqlite3.Connection) -> RegistryBundle:
    raw: dict[str, Any] = {"configs": {}, "feature_tables": {}, "upstream_models": {}}
    try:
        for row in connection.execute("SELECT id, kind, definition_json FROM configs"):
            definition = json.loads(row["definition_json"])
            if not isinstance(definition, dict):
                definition = {"value": definition}
            definition.setdefault("kind", row["kind"])
            raw["configs"][row["id"]] = definition
    except sqlite3.Error:
        pass
    for table in ("feature_tables", "upstream_models"):
        try:
            for row in connection.execute(f"SELECT id, definition_json FROM {table}"):
                raw[table][row["id"]] = json.loads(row["definition_json"])
        except sqlite3.Error:
            pass
    return _normalise_registries(raw)


def load_catalog(
    *,
    v1_path: str | Path | None = None,
    v2_db_path: str | Path | None = None,
    v2_dir: str | Path | None = None,
    registries_path: str | Path | None = None,
) -> Catalog:
    if not v1_path and not v2_db_path:
        fail("VALIDATION_ERROR", "at least one of v1 or v2 inputs is required")
    v1_versions: tuple[Manifest, ...] = ()
    v2_versions: tuple[Manifest, ...] = ()
    registries = RegistryBundle({}, {}, {})
    digest_parts: list[str] = []
    sources: dict[str, str] = {}
    if v1_path:
        v1_versions, v1_registries, v1_digest = load_v1(v1_path)
        registries.merge(v1_registries, source="v1")
        digest_parts.append(f"v1:{v1_digest}")
        sources["v1"] = str(Path(v1_path).resolve())
    if v2_db_path:
        if not v2_dir:
            v2_dir = Path(v2_db_path).resolve().parent
        v2_versions, v2_registries, v2_digest = load_v2(v2_db_path, v2_dir)
        registries.merge(v2_registries, source="v2")
        digest_parts.append(f"v2:{v2_digest}")
        sources["v2"] = str(Path(v2_db_path).resolve())
    if registries_path:
        registry_file = Path(registries_path).resolve()
        try:
            raw = read_json(registry_file)
        except (OSError, json.JSONDecodeError) as exc:
            fail("IO_ERROR", "cannot read registries file", path=str(registry_file), detail=str(exc))
        registries.merge(_normalise_registries(raw), source="registries")
        digest_parts.append(f"registries:{sha256_file(registry_file)}")
        sources["registries"] = str(registry_file)
    catalog = Catalog(
        v1_versions=v1_versions,
        v2_versions=v2_versions,
        registries=registries,
        input_digest=sha256_bytes("\n".join(digest_parts).encode("utf-8")),
        sources=sources,
    )
    _validate_source_uniqueness(catalog)
    catalog.validate_references()
    return catalog


def _validate_source_uniqueness(catalog: Catalog) -> None:
    for source, manifests in (("v1", catalog.v1_versions), ("v2", catalog.v2_versions)):
        seen: set[str] = set()
        for manifest in manifests:
            if manifest.version in seen:
                fail(
                    "VERSION_DUPLICATE",
                    "version is duplicated within a source",
                    source=source,
                    version=manifest.version,
                )
            seen.add(manifest.version)


def load_full_replacement(
    path: str | Path,
    *,
    catalog: Catalog | None = None,
    target_version: str | None = None,
) -> Manifest:
    source_path = Path(path).resolve()
    try:
        data = read_json(source_path)
    except (OSError, json.JSONDecodeError) as exc:
        fail("IO_ERROR", "cannot parse full replacement manifest", path=str(source_path), detail=str(exc))
    if not isinstance(data, dict):
        fail("VALIDATION_ERROR", "full replacement manifest must be an object", path=str(source_path))
    version = data.get("version", target_version)
    if not isinstance(version, str) or not version:
        fail("VALIDATION_ERROR", "full replacement manifest has no version", path=str(source_path))
    catalog_manifest: Manifest | None = None
    if catalog is not None:
        try:
            catalog_manifest = catalog.manifest(version, source_format="v2")
        except WorkbenchError as exc:
            if exc.code != "NOT_FOUND":
                raise
            catalog_manifest = None
    absolute_raw = data.get("absolute_updated_ms")
    logical_clock = data.get("logical_clock")
    generation = data.get("generation")
    revision = _entry_revision(data, 0)
    preprocess = data.get("preprocess")
    postprocess = data.get("postprocess")
    dependencies = normalise_dependencies(data.get("dependencies"), path=str(source_path))
    if absolute_raw is None and catalog_manifest is not None:
        absolute_raw = catalog_manifest.absolute_updated_ms
    if absolute_raw is None:
        fail("VALIDATION_ERROR", "full replacement manifest lacks absolute_updated_ms", path=str(source_path))
    absolute, provenance = absolute_time_provenance(absolute_raw)
    if logical_clock is None and catalog_manifest is not None:
        logical_clock = catalog_manifest.logical_clock
    if generation is None and catalog_manifest is not None:
        generation = catalog_manifest.generation
    if revision == 0 and catalog_manifest is not None:
        revision = catalog_manifest.revision
    if preprocess is None and catalog_manifest is not None:
        preprocess = catalog_manifest.preprocess
    if postprocess is None and catalog_manifest is not None:
        postprocess = catalog_manifest.postprocess
    if not isinstance(preprocess, str) or not preprocess:
        fail("VALIDATION_ERROR", "full replacement manifest lacks preprocess", path=str(source_path))
    if not isinstance(postprocess, str) or not postprocess:
        fail("VALIDATION_ERROR", "full replacement manifest lacks postprocess", path=str(source_path))
    if not isinstance(logical_clock, int) or isinstance(logical_clock, bool):
        fail("VALIDATION_ERROR", "full replacement logical_clock must be an integer", path=str(source_path))
    if not isinstance(generation, int) or isinstance(generation, bool):
        fail("VALIDATION_ERROR", "full replacement generation must be an integer", path=str(source_path))
    return Manifest(
        version=version,
        source_format="full-replacement",
        revision=revision,
        logical_clock=logical_clock,
        generation=generation,
        absolute_updated_ms=absolute,
        raw_time={"kind": "absolute", "value": str(absolute_raw)},
        time_provenance=provenance,
        layers=parse_layers(data.get("layers"), path=str(source_path)),
        preprocess=preprocess,
        postprocess=postprocess,
        feature_tables=dependencies["feature_tables"],
        upstream_models=dependencies["upstream_models"],
        canonical_id=str(data.get("canonical_id", catalog_manifest.canonical_id if catalog_manifest else version)),
        patch_digest=str(data.get("patch_digest", "")),
        source_path=str(source_path),
        source_dir=str(source_path.parent),
    )


def reg_bundle_from_dict(raw: Any) -> RegistryBundle:
    return _normalise_registries(raw)
