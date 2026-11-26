"""Versioned v1/v2 configuration parsing and loss-aware migration."""

from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    DiagnosticError,
    E_CONFIG_ROLLBACK,
    E_VERSION_UNSUPPORTED,
)
from .resources import ResourceLimits
from .util import atomic_write_json, canonical_json, sha256_json, sha256_text


COMPRESSION_ALIASES = {
    "none": "raw",
    "plain": "raw",
    "raw": "raw",
    "deflate": "zlib",
    "zlib": "zlib",
    "gzip": "gzip",
    "delta": "delta",
    "delta_gzip": "delta_gzip",
    "gzip_delta": "delta_gzip",
    "delta_raw": "delta",
    "delta_zlib": "delta_zlib",
}
SUPPORTED_COMPRESSION = frozenset(COMPRESSION_ALIASES.values())
VERSION_HEADER_KEYS = ("version", "schema_version", "config_version")
REQUIRED_V1_FIELDS = (
    "generation",
    "logical_clock",
    "chunk_size",
    "compression",
    "delta_step",
    "vocabulary",
)
REQUIRED_V2_META_FIELDS = (
    "generation",
    "logical_clock",
    "chunk_size",
    "compression",
    "delta_step",
)


def normalize_term(term: str) -> str:
    if not isinstance(term, str):
        raise ValueError("term must be a string")
    normalized = unicodedata.normalize("NFKC", term).strip().casefold()
    if not normalized or any(char.isspace() for char in normalized):
        raise ValueError(f"invalid term {term!r}")
    return normalized


def normalize_vocabulary(values: Iterable[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("vocabulary must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = normalize_term(value)
        if term in seen:
            raise ValueError(f"duplicate vocabulary term {term!r}")
        seen.add(term)
        result.append(term)
    if not result:
        raise ValueError("vocabulary must not be empty")
    return tuple(result)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    field_name: str,
    *,
    minimum: int = 0,
) -> int:
    if not _is_int(value) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _normalize_compression(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("compression must be a string")
    normalized = COMPRESSION_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ValueError(f"unknown compression mode {value!r}")
    return normalized


@dataclass(frozen=True)
class NormalizedConfig:
    """The only configuration shape consumed by index, cache, and ledger code."""

    schema_version: int
    generation: int
    logical_clock: int
    chunk_size: int
    compression: str
    delta_step: int
    vocabulary: tuple[str, ...]
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    shard_clocks: tuple[tuple[int, int], ...] = ()
    field_sources: dict[str, str] = field(default_factory=dict)
    migration_history: tuple[dict[str, Any], ...] = ()

    @property
    def vocabulary_set(self) -> frozenset[str]:
        return frozenset(self.vocabulary)

    @property
    def config_hash(self) -> str:
        return sha256_json(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "compression": self.compression,
            "delta_step": self.delta_step,
            "field_sources": dict(sorted(self.field_sources.items())),
            "generation": self.generation,
            "logical_clock": self.logical_clock,
            "migration_history": list(self.migration_history),
            "resource_limits": self.resource_limits.to_dict(),
            "schema_version": self.schema_version,
            "shard_clocks": [list(item) for item in self.shard_clocks],
            "vocabulary": list(self.vocabulary),
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.payload()
        result["config_hash"] = self.config_hash
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedConfig":
        return cls(
            schema_version=_require_int(value["schema_version"], "schema_version", minimum=1),
            generation=_require_int(value["generation"], "generation"),
            logical_clock=_require_int(value["logical_clock"], "logical_clock"),
            chunk_size=_require_int(value["chunk_size"], "chunk_size", minimum=1),
            compression=_normalize_compression(value["compression"]),
            delta_step=_require_int(value["delta_step"], "delta_step", minimum=1),
            vocabulary=normalize_vocabulary(value["vocabulary"]),
            resource_limits=ResourceLimits.from_mapping(value.get("resource_limits")),
            shard_clocks=tuple(
                (int(item[0]), int(item[1])) for item in value.get("shard_clocks", [])
            ),
            field_sources=dict(value.get("field_sources", {})),
            migration_history=tuple(value.get("migration_history", [])),
        )

    def cache_parameters(self) -> dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "compression": self.compression,
            "delta_step": self.delta_step,
        }

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "config_hash": self.config_hash,
            "generation": self.generation,
            "logical_clock": self.logical_clock,
            "schema_version": self.schema_version,
        }

    @property
    def version(self) -> int:
        """Alias used by callers that treat the schema header as the version."""

        return self.schema_version

    @property
    def config_version(self) -> int:
        return self.schema_version


@dataclass
class LoadedConfig:
    config: NormalizedConfig
    source_path: str
    source_kind: str
    raw_manifest: dict[str, Any]
    sources: dict[str, str]


@dataclass
class MigrationResult:
    action: str
    source_path: str
    target_path: str
    config: NormalizedConfig
    report: dict[str, Any]
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "config": self.config.to_dict(),
            "report": self.report,
            "report_path": self.report_path,
            "source_path": self.source_path,
            "target_path": self.target_path,
        }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"configuration JSON is invalid: {exc}",
            {"path": str(path)},
        ) from exc
    if not isinstance(value, dict):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "configuration root must be a JSON object",
            {"path": str(path)},
        )
    return value


def _version_from_header(document: dict[str, Any], *, path: str) -> int:
    present = [key for key in VERSION_HEADER_KEYS if key in document]
    if not present:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "configuration version header is missing",
            {"path": path},
        )
    values = {document[key] for key in present}
    if len(values) != 1:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "conflicting configuration version headers",
            {"path": path, "headers": {key: document[key] for key in present}},
        )
    version = next(iter(values))
    if not _is_int(version) or version not in (1, 2):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"unsupported configuration version {version!r}",
            {"path": path},
        )
    return version


def _value_with_source(
    document: dict[str, Any],
    key: str,
    sources: dict[str, str],
    *,
    prefix: str,
    required: bool = True,
    default: Any = None,
) -> Any:
    if key in document:
        sources[key] = f"{prefix}:{key}"
        return document[key]
    if required:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"required configuration field {key!r} is missing",
            {"field": key},
        )
    sources[key] = "default"
    return default


def _load_v1(path: Path, document: dict[str, Any]) -> LoadedConfig:
    sources: dict[str, str] = {}
    chunk = document.get("chunk")
    if chunk is not None and not isinstance(chunk, dict):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v1 chunk field must be an object when present",
            {"path": str(path)},
        )
    merged = dict(document)
    if isinstance(chunk, dict):
        for key in ("chunk_size", "compression", "delta_step"):
            if key not in merged and key in chunk:
                merged[key] = chunk[key]
                sources[key] = f"v1:chunk.{key}"

    try:
        generation = _require_int(
            _value_with_source(merged, "generation", sources, prefix="v1"),
            "generation",
        )
        logical_clock = _require_int(
            _value_with_source(merged, "logical_clock", sources, prefix="v1"),
            "logical_clock",
        )
        chunk_size = _require_int(
            _value_with_source(merged, "chunk_size", sources, prefix="v1"),
            "chunk_size",
            minimum=1,
        )
        compression = _normalize_compression(
            _value_with_source(merged, "compression", sources, prefix="v1")
        )
        delta_step = _require_int(
            _value_with_source(merged, "delta_step", sources, prefix="v1"),
            "delta_step",
            minimum=1,
        )
        vocabulary = normalize_vocabulary(
            _value_with_source(merged, "vocabulary", sources, prefix="v1")
        )
        resource_value = merged.get("resource_limits")
        if resource_value is None:
            sources["resource_limits"] = "default"
            for resource_field in (
                "max_batch_docs",
                "max_postings_bytes",
                "max_comparisons",
                "degrade_policy",
            ):
                sources[f"resource_limits.{resource_field}"] = "default"
        else:
            sources["resource_limits"] = "v1:resource_limits"
            if isinstance(resource_value, dict):
                for resource_field in (
                    "max_batch_docs",
                    "max_postings_bytes",
                    "max_comparisons",
                    "degrade_policy",
                ):
                    if resource_field not in resource_value:
                        sources[f"resource_limits.{resource_field}"] = "default"
        resource_limits = ResourceLimits.from_mapping(resource_value)
    except DiagnosticError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"invalid v1 configuration: {exc}",
            {"path": str(path)},
        ) from exc

    config = NormalizedConfig(
        schema_version=1,
        generation=generation,
        logical_clock=logical_clock,
        chunk_size=chunk_size,
        compression=compression,
        delta_step=delta_step,
        vocabulary=vocabulary,
        resource_limits=resource_limits,
        field_sources={key: sources[key] for key in sorted(sources)},
    )
    return LoadedConfig(
        config=config,
        source_path=str(path),
        source_kind="v1",
        raw_manifest=document,
        sources=config.field_sources,
    )


def _sqlite_connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 metadata database is missing",
            {"path": str(path)},
        )
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"cannot open v2 metadata database: {exc}",
            {"path": str(path)},
        ) from exc


def _read_meta(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    try:
        rows = connection.execute("SELECT key, value FROM config_meta").fetchall()
    except sqlite3.Error as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"v2 metadata table config_meta is invalid: {exc}",
            {"path": str(path)},
        ) from exc
    result: dict[str, Any] = {}
    for key, value in rows:
        try:
            result[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                f"v2 metadata value for {key!r} is not canonical JSON",
                {"path": str(path), "key": key},
            ) from exc
    return result


def _read_shard_rows(
    connection: sqlite3.Connection, path: Path
) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT shard_index, path, shard_clock, checksum "
            "FROM config_shards ORDER BY shard_index"
        ).fetchall()
    except sqlite3.Error as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"v2 metadata table config_shards is invalid: {exc}",
            {"path": str(path)},
        ) from exc
    return [
        {
            "shard_index": int(index),
            "path": str(shard_path),
            "shard_clock": int(clock),
            "checksum": str(checksum),
        }
        for index, shard_path, clock, checksum in rows
    ]


def _read_provenance(
    connection: sqlite3.Connection, path: Path
) -> dict[str, str]:
    try:
        rows = connection.execute(
            "SELECT field, source FROM config_provenance ORDER BY field"
        ).fetchall()
    except sqlite3.Error as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"v2 provenance table is invalid: {exc}",
            {"path": str(path)},
        ) from exc
    return {str(field): str(source) for field, source in rows}


def _load_v2(path: Path, manifest: dict[str, Any]) -> LoadedConfig:
    base = path.parent
    metadata_name = manifest.get("metadata_db", manifest.get("meta_db"))
    if not isinstance(metadata_name, str) or not metadata_name:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 metadata_db field is missing or invalid",
            {"path": str(path)},
        )
    shard_specs = manifest.get("shards")
    if not isinstance(shard_specs, list) or not shard_specs:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 shards field is missing or empty",
            {"path": str(path)},
        )
    metadata_path = (base / metadata_name).resolve()
    connection = _sqlite_connect_readonly(metadata_path)
    try:
        meta = _read_meta(connection, metadata_path)
        shard_rows = _read_shard_rows(connection, metadata_path)
        provenance = _read_provenance(connection, metadata_path)
    finally:
        connection.close()

    db_version = meta.get("schema_version", meta.get("version_is_2"))
    if db_version is not None and db_version != 2:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 metadata database version header is invalid",
            {"path": str(metadata_path), "version": db_version},
        )

    sources: dict[str, str] = dict(provenance)
    try:
        for field_name in REQUIRED_V2_META_FIELDS:
            if field_name not in meta:
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    f"required v2 metadata field {field_name!r} is missing",
                    {"path": str(metadata_path), "field": field_name},
                )
            sources[field_name] = provenance.get(field_name, f"v2:metadata.{field_name}")
        generation = _require_int(meta["generation"], "generation")
        meta_clock = _require_int(meta["logical_clock"], "logical_clock")
        chunk_size = _require_int(meta["chunk_size"], "chunk_size", minimum=1)
        compression = _normalize_compression(meta["compression"])
        delta_step = _require_int(meta["delta_step"], "delta_step", minimum=1)
        resources_value = meta.get("resource_limits")
        sources["resource_limits"] = provenance.get(
            "resource_limits",
            "v2:metadata.resource_limits" if resources_value is not None else "default",
        )
        resource_limits = ResourceLimits.from_mapping(resources_value)
        if not isinstance(resources_value, dict):
            for resource_field in (
                "max_batch_docs",
                "max_postings_bytes",
                "max_comparisons",
                "degrade_policy",
            ):
                sources.setdefault(
                    f"resource_limits.{resource_field}", "default"
                )
        else:
            for resource_field in (
                "max_batch_docs",
                "max_postings_bytes",
                "max_comparisons",
                "degrade_policy",
            ):
                if resource_field not in resources_value:
                    sources.setdefault(
                        f"resource_limits.{resource_field}", "default"
                    )

        if "generation" in manifest and manifest["generation"] != generation:
            raise ValueError("manifest and metadata generation disagree")
        if "logical_clock" in manifest and manifest["logical_clock"] != meta_clock:
            raise ValueError("manifest and metadata logical_clock disagree")
    except DiagnosticError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"invalid v2 metadata: {exc}",
            {"path": str(metadata_path)},
        ) from exc

    manifest_shards: list[dict[str, Any]] = []
    for index, value in enumerate(shard_specs):
        if isinstance(value, str):
            manifest_shards.append({"shard_index": index, "path": value})
        elif isinstance(value, dict) and isinstance(value.get("path"), str):
            manifest_shards.append(dict(value))
        else:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "v2 shard entry must be a path string or object with path",
                {"path": str(path), "index": index},
            )
    db_by_index = {row["shard_index"]: row for row in shard_rows}
    vocabulary: list[str] = []
    shard_clocks: list[tuple[int, int]] = []
    for index, shard_spec in enumerate(manifest_shards):
        shard_path = (base / shard_spec["path"]).resolve()
        try:
            shard = _read_json_object(shard_path)
        except FileNotFoundError as exc:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "v2 shard file is missing",
                {"path": str(shard_path), "index": index},
            ) from exc
        if _version_from_header(shard, path=str(shard_path)) != 2:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "v2 shard has a non-v2 version header",
                {"path": str(shard_path)},
            )
        if shard.get("shard_index", index) != index:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "v2 shard_index does not match manifest order",
                {"path": str(shard_path), "expected": index},
            )
        shard_clock = shard.get("shard_clock", meta_clock)
        try:
            shard_clock = _require_int(shard_clock, "shard_clock")
            terms = normalize_vocabulary(shard.get("vocabulary"))
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                f"invalid v2 shard: {exc}",
                {"path": str(shard_path)},
            ) from exc
        row = db_by_index.get(index)
        if row is not None:
            if row["checksum"] != sha256_text(canonical_json(shard)):
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    "v2 shard checksum mismatch",
                    {"path": str(shard_path), "index": index},
                )
            metadata_shard_path = (base / row["path"]).resolve()
            if metadata_shard_path != shard_path:
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    "v2 shard path disagrees with metadata",
                    {
                        "path": str(shard_path),
                        "metadata_path": str(metadata_shard_path),
                    },
                )
            if row["shard_clock"] != shard_clock:
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    "v2 shard clock disagrees with metadata",
                    {"path": str(shard_path), "index": index},
                )
        for term in terms:
            if term in vocabulary:
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    f"duplicate vocabulary term {term!r} across v2 shards",
                    {"path": str(shard_path)},
                )
            vocabulary.append(term)
        shard_clocks.append((index, shard_clock))

    if not vocabulary:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 configuration has no vocabulary terms",
            {"path": str(path)},
        )
    normalized_clock = max([meta_clock, *(clock for _, clock in shard_clocks)])
    sources["logical_clock"] = (
        "v2:max(logical_clock,shard_clock)"
        if normalized_clock != meta_clock
        else sources.get("logical_clock", "v2:metadata.logical_clock")
    )
    sources["vocabulary"] = "v2:shards"
    migration_history = meta.get("migration_history", [])
    if not isinstance(migration_history, list):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "v2 migration_history must be an array",
            {"path": str(metadata_path)},
        )
    config = NormalizedConfig(
        schema_version=2,
        generation=generation,
        logical_clock=normalized_clock,
        chunk_size=chunk_size,
        compression=compression,
        delta_step=delta_step,
        vocabulary=tuple(vocabulary),
        resource_limits=resource_limits,
        shard_clocks=tuple(shard_clocks),
        field_sources={key: sources[key] for key in sorted(sources)},
        migration_history=tuple(migration_history),
    )
    return LoadedConfig(
        config=config,
        source_path=str(path),
        source_kind="v2",
        raw_manifest=manifest,
        sources=config.field_sources,
    )


class ConfigLoader:
    """Dispatch by version header and enforce optional generation rollback floors."""

    @staticmethod
    def load(
        path: str | os.PathLike[str],
        *,
        generation_floor: int | None = None,
    ) -> LoadedConfig:
        source = Path(path)
        if source.is_dir():
            candidates = [source / "manifest.json", source / "config.json"]
            source = next((item for item in candidates if item.exists()), candidates[0])
        manifest = _read_json_object(source)
        version = _version_from_header(manifest, path=str(source))
        loaded = (
            _load_v1(source, manifest)
            if version == 1
            else _load_v2(source, manifest)
        )
        if generation_floor is not None and loaded.config.generation < generation_floor:
            raise DiagnosticError(
                E_CONFIG_ROLLBACK,
                "configuration generation would roll back",
                {
                    "actual": loaded.config.generation,
                    "floor": generation_floor,
                    "path": str(source),
                },
            )
        return loaded


def load_config(
    path: str | os.PathLike[str],
    *,
    generation_floor: int | None = None,
) -> NormalizedConfig:
    return ConfigLoader.load(path, generation_floor=generation_floor).config


def _sqlite_write_v2(
    metadata_path: Path,
    config: NormalizedConfig,
    shard_entries: list[dict[str, Any]],
    *,
    report: dict[str, Any],
) -> None:
    temporary = metadata_path.with_name(metadata_path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("CREATE TABLE config_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE config_shards ("
            "shard_index INTEGER PRIMARY KEY, path TEXT NOT NULL, "
            "shard_clock INTEGER NOT NULL, checksum TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE config_provenance (field TEXT PRIMARY KEY, source TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE migration_history (seq INTEGER PRIMARY KEY, entry_json TEXT NOT NULL)"
        )
        metadata = {
            "schema_version": 2,
            "generation": config.generation,
            "logical_clock": config.logical_clock,
            "chunk_size": config.chunk_size,
            "compression": config.compression,
            "delta_step": config.delta_step,
            "resource_limits": config.resource_limits.to_dict(),
            "migration_history": list(config.migration_history),
        }
        connection.executemany(
            "INSERT INTO config_meta(key, value) VALUES (?, ?)",
            [(key, canonical_json(value)) for key, value in sorted(metadata.items())],
        )
        connection.executemany(
            "INSERT INTO config_shards(shard_index, path, shard_clock, checksum) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    entry["shard_index"],
                    entry["path"],
                    entry["shard_clock"],
                    entry["checksum"],
                )
                for entry in shard_entries
            ],
        )
        connection.executemany(
            "INSERT INTO config_provenance(field, source) VALUES (?, ?)",
            sorted(config.field_sources.items()),
        )
        connection.executemany(
            "INSERT INTO migration_history(seq, entry_json) VALUES (?, ?)",
            [
                (index, canonical_json(entry))
                for index, entry in enumerate(config.migration_history)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, metadata_path)


def upgrade_v1_to_v2(
    source_path: str | os.PathLike[str],
    target_dir: str | os.PathLike[str],
    *,
    shard_count: int = 2,
    write_report: bool = True,
) -> MigrationResult:
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count <= 0:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "shard_count must be a positive integer",
            {"shard_count": shard_count},
        )
    loaded = ConfigLoader.load(source_path)
    if loaded.config.schema_version != 1:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "upgrade source must use v1 format",
            {"actual_version": loaded.config.schema_version},
        )
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    shards_dir = target / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    actual_shards = min(shard_count, len(loaded.config.vocabulary))
    bucket_size = (len(loaded.config.vocabulary) + actual_shards - 1) // actual_shards
    shard_entries: list[dict[str, Any]] = []
    shard_paths: list[str] = []
    for index in range(actual_shards):
        terms = loaded.config.vocabulary[
            index * bucket_size : (index + 1) * bucket_size
        ]
        if not terms:
            continue
        shard_name = f"shards/shard_{index:03d}.json"
        shard_document = {
            "version": 2,
            "shard_index": index,
            "shard_clock": loaded.config.logical_clock,
            "vocabulary": list(terms),
        }
        shard_path = target / shard_name
        atomic_write_json(shard_path, shard_document)
        shard_paths.append(shard_name)
        shard_entries.append(
            {
                "shard_index": index,
                "path": shard_name,
                "shard_clock": loaded.config.logical_clock,
                "checksum": sha256_text(canonical_json(shard_document)),
            }
        )

    defaulted_v2_fields = sorted(
        {
            *(
                field_name
                for field_name, source in loaded.sources.items()
                if source == "default"
            ),
            "migration_history",
            "config_shards",
            "config_provenance",
        }
    )
    migration_entry = {
        "action": "upgrade_v1_to_v2",
        "defaulted_fields": defaulted_v2_fields,
        "from_version": 1,
        "shard_count": len(shard_entries),
        "to_version": 2,
    }
    new_sources = dict(loaded.sources)
    for field_name in defaulted_v2_fields:
        new_sources.setdefault(field_name, "migration:v1_to_v2_default")
    new_sources["vocabulary"] = "v2:shards"
    new_sources["logical_clock"] = "v1:logical_clock"
    new_config = NormalizedConfig(
        schema_version=2,
        generation=loaded.config.generation,
        logical_clock=loaded.config.logical_clock,
        chunk_size=loaded.config.chunk_size,
        compression=loaded.config.compression,
        delta_step=loaded.config.delta_step,
        vocabulary=loaded.config.vocabulary,
        resource_limits=loaded.config.resource_limits,
        shard_clocks=tuple(
            (entry["shard_index"], entry["shard_clock"]) for entry in shard_entries
        ),
        field_sources={key: new_sources[key] for key in sorted(new_sources)},
        migration_history=(migration_entry,),
    )

    report = {
        "action": "upgrade_v1_to_v2",
        "completed_default_fields": defaulted_v2_fields,
        "config_hash": new_config.config_hash,
        "dropped_fields": [],
        "flattened_fields": [],
        "source": str(Path(source_path).resolve()),
        "target": str(target.resolve()),
    }
    metadata_path = target / "metadata.sqlite"
    _sqlite_write_v2(metadata_path, new_config, shard_entries, report=report)
    manifest = {
        "version": 2,
        "generation": new_config.generation,
        "logical_clock": new_config.logical_clock,
        "metadata_db": "metadata.sqlite",
        "shards": shard_paths,
    }
    manifest_path = target / "manifest.json"
    atomic_write_json(manifest_path, manifest)

    persisted = ConfigLoader.load(manifest_path)
    if (
        persisted.config.generation != loaded.config.generation
        or persisted.config.logical_clock != loaded.config.logical_clock
        or persisted.config.chunk_size != loaded.config.chunk_size
        or persisted.config.compression != loaded.config.compression
        or persisted.config.delta_step != loaded.config.delta_step
        or persisted.config.vocabulary != loaded.config.vocabulary
        or persisted.config.resource_limits != loaded.config.resource_limits
    ):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "upgraded v2 configuration failed round-trip validation",
            {"path": str(manifest_path)},
        )
    report["target_config_hash"] = persisted.config.config_hash
    report_path: str | None = None
    if write_report:
        report_path = str(target / "migration_report.json")
        atomic_write_json(report_path, report)
    return MigrationResult(
        action="upgrade",
        source_path=str(Path(source_path).resolve()),
        target_path=str(manifest_path.resolve()),
        config=persisted.config,
        report=report,
        report_path=report_path,
    )


def downgrade_v2_to_v1(
    source_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    *,
    write_report: bool = True,
) -> MigrationResult:
    loaded = ConfigLoader.load(source_path)
    if loaded.config.schema_version != 2:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "downgrade source must use v2 format",
            {"actual_version": loaded.config.schema_version},
        )
    shard_clock_max = max(
        (clock for _, clock in loaded.config.shard_clocks),
        default=loaded.config.logical_clock,
    )
    flattened = []
    if len(set(clock for _, clock in loaded.config.shard_clocks)) > 1:
        flattened.append(
            {
                "field": "logical_clock",
                "rule": "max(shard_clock)",
                "value": max(shard_clock_max, loaded.config.logical_clock),
            }
        )
    dropped = [
        "config_shards",
        "field_sources",
        "metadata_db",
        "migration_history",
        "shard_clocks",
        "shard_checksums",
    ]
    target = Path(target_path)
    document = {
        "version": 1,
        "generation": loaded.config.generation,
        "logical_clock": max(shard_clock_max, loaded.config.logical_clock),
        "chunk_size": loaded.config.chunk_size,
        "compression": loaded.config.compression,
        "delta_step": loaded.config.delta_step,
        "vocabulary": list(loaded.config.vocabulary),
        "resource_limits": loaded.config.resource_limits.to_dict(),
    }
    atomic_write_json(target, document)
    persisted = ConfigLoader.load(target)
    if (
        persisted.config.generation != loaded.config.generation
        or persisted.config.chunk_size != loaded.config.chunk_size
        or persisted.config.compression != loaded.config.compression
        or persisted.config.delta_step != loaded.config.delta_step
        or persisted.config.vocabulary != loaded.config.vocabulary
    ):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "downgraded v1 configuration failed round-trip validation",
            {"path": str(target)},
        )
    report = {
        "action": "downgrade_v2_to_v1",
        "completed_default_fields": [],
        "config_hash": persisted.config.config_hash,
        "dropped_fields": dropped,
        "flattened_fields": flattened,
        "source": str(Path(source_path).resolve()),
        "target": str(target.resolve()),
    }
    report_path: str | None = None
    if write_report:
        report_path = str(target.with_name(target.name + ".migration.json"))
        atomic_write_json(report_path, report)
    return MigrationResult(
        action="downgrade",
        source_path=str(Path(source_path).resolve()),
        target_path=str(target.resolve()),
        config=persisted.config,
        report=report,
        report_path=report_path,
    )
