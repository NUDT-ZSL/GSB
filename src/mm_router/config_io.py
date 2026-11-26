"""v1 single-file and v2 sharded JSON + SQLite configuration readers/writers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write_json,
    ensure_timestamp_not_rolling_back,
    format_timestamp_ms,
    parse_timestamp_ms,
    read_json_strict_no_duplicates,
    require_field,
    require_int,
    require_mapping,
    require_number,
    require_sha256,
    require_string,
    require_threshold,
    sha256_file,
)
from .errors import WorkbenchError, corrupt, unsupported
from .migration import upgrade_v1_document
from .models import (
    PRECISIONS,
    THRESHOLD_KEYS,
    LoadResult,
    ProvenanceRecord,
    ThresholdConfig,
    ThresholdEntry,
    Tombstone,
)


V1_FIELDS = {
    "thresholds",
    "route_budget",
    "candidate_plan_count",
    "scheduler_workers",
    "precision",
    "updated_at",
}
V2_MANIFEST_FIELDS = {
    "generation",
    "revision",
    "created_at_ms",
    "updated_at_ms",
    "metadata_db",
    "metadata_checksum",
    "shards",
    "route_budget",
    "candidate_plan_count",
    "scheduler_workers",
    "precision",
}
META_KEYS = {
    "config_generation",
    "revision",
    "created_at_ms",
    "updated_at_ms",
}


def _normalize_version(value: Any) -> int:
    if isinstance(value, bool):
        raise unsupported("boolean version header is invalid", header=value)
    if isinstance(value, int):
        version = value
    elif isinstance(value, str):
        normalized = value.lower().strip()
        if normalized in {"1", "v1"}:
            version = 1
        elif normalized in {"2", "v2"}:
            version = 2
        else:
            raise unsupported("unknown configuration version header", header=value)
    else:
        raise unsupported("configuration version header has invalid type", header=value)
    if version not in (1, 2):
        raise unsupported("unknown configuration version header", header=value)
    return version


def sniff_format_version(document: dict[str, Any]) -> int:
    for header in ("format_version", "version_header", "schema_version"):
        if header in document:
            return _normalize_version(document[header])
    schema = document.get("schema")
    if schema == "mm-threshold-shard" and "entries" in document:
        return 2
    if schema == "mm-threshold-config":
        if (
            "shards" in document
            or "generation" in document
            or "metadata_db" in document
        ):
            return 2
        if any(field in document for field in V1_FIELDS) or "config" in document:
            return 1
    if "config" in document and isinstance(document["config"], dict):
        body = document["config"]
        if set(V1_FIELDS).issubset(body):
            return 1
        if "shards" in body or "generation" in body:
            return 2
    if set(V1_FIELDS).issubset(document):
        return 1
    if "shards" in document and "generation" in document:
        return 2
    raise unsupported(
        "configuration format cannot be inferred from version header or field set",
        fields=sorted(document),
    )


def _read_v1_document(
    document: dict[str, Any],
    *,
    previous_updated_at_ms: int | None,
) -> LoadResult:
    config, records = upgrade_v1_document(
        document,
        previous_updated_at_ms=previous_updated_at_ms,
    )
    return LoadResult(
        format_version=1,
        config=config,
        provenance=records,
    )


def _safe_child(root: Path, relative: str) -> Path:
    if os.path.isabs(relative):
        raise corrupt("v2 shard path must be relative", path=relative)
    resolved_root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise corrupt("v2 shard path escapes configuration directory", path=relative) from exc
    return candidate


def _read_metadata_db(
    path: Path,
    *,
    manifest: dict[str, Any],
    expected_manifest_checksum: str,
) -> tuple[dict[str, int], tuple[Tombstone, ...], str]:
    if not path.is_file():
        raise corrupt("v2 metadata database is missing", path=str(path))
    actual_checksum = sha256_file(path)
    if actual_checksum != expected_manifest_checksum:
        raise corrupt(
            "v2 metadata database checksum mismatch",
            path=str(path),
            expected=expected_manifest_checksum,
            actual=actual_checksum,
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            meta_rows = connection.execute(
                "SELECT key, value FROM config_meta"
            ).fetchall()
            tombstone_rows = connection.execute(
                "SELECT key, revision, invalidated_at_ms FROM tombstones ORDER BY key"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise corrupt(
            "v2 metadata database cannot be read",
            path=str(path),
            reason=str(exc),
        ) from exc

    metadata: dict[str, int] = {}
    for key, value in meta_rows:
        if key in metadata:
            raise corrupt("duplicate metadata key", key=key)
        try:
            metadata[str(key)] = int(str(value))
        except (TypeError, ValueError) as exc:
            raise corrupt("metadata value is not an integer", key=key, value=value) from exc
    if set(metadata) != META_KEYS:
        raise corrupt(
            "v2 metadata key set is not exact",
            missing=sorted(META_KEYS.difference(metadata)),
            extra=sorted(set(metadata).difference(META_KEYS)),
        )

    expected_meta = {
        "config_generation": require_int(
            require_field(manifest, "generation", "manifest"),
            "manifest.generation",
            minimum=1,
        ),
        "revision": require_int(
            require_field(manifest, "revision", "manifest"),
            "manifest.revision",
            minimum=0,
        ),
        "created_at_ms": require_int(
            require_field(manifest, "created_at_ms", "manifest"),
            "manifest.created_at_ms",
            minimum=0,
        ),
        "updated_at_ms": require_int(
            require_field(manifest, "updated_at_ms", "manifest"),
            "manifest.updated_at_ms",
            minimum=0,
        ),
    }
    if metadata != expected_meta:
        raise corrupt(
            "v2 manifest and SQLite metadata disagree",
            manifest=expected_meta,
            sqlite=metadata,
        )

    tombstones: list[Tombstone] = []
    for key, revision, invalidated_at_ms in tombstone_rows:
        tombstones.append(
            Tombstone(
                key=require_string(key, "tombstones.key"),
                revision=require_int(revision, "tombstones.revision", minimum=0),
                invalidated_at_ms=require_int(
                    invalidated_at_ms,
                    "tombstones.invalidated_at_ms",
                    minimum=0,
                ),
            )
        )
    return expected_meta, tuple(tombstones), actual_checksum


def _read_v2_directory(
    directory: Path,
    *,
    previous_updated_at_ms: int | None,
) -> LoadResult:
    manifest_path = directory / "manifest.json"
    manifest = require_mapping(
        read_json_strict_no_duplicates(manifest_path),
        "manifest",
    )
    version = sniff_format_version(manifest)
    if version != 2:
        raise corrupt("v2 directory manifest declares the wrong version", version=version)
    missing = sorted(V2_MANIFEST_FIELDS.difference(manifest))
    if missing:
        raise corrupt("required v2 manifest fields are missing", fields=missing)

    generation = require_int(
        require_field(manifest, "generation", "manifest"),
        "manifest.generation",
        minimum=1,
    )
    revision = require_int(
        require_field(manifest, "revision", "manifest"),
        "manifest.revision",
        minimum=0,
    )
    created_at_ms = require_int(
        require_field(manifest, "created_at_ms", "manifest"),
        "manifest.created_at_ms",
        minimum=0,
    )
    updated_at_ms = require_int(
        require_field(manifest, "updated_at_ms", "manifest"),
        "manifest.updated_at_ms",
        minimum=0,
    )
    if updated_at_ms < created_at_ms:
        raise corrupt("configuration timestamps are internally inconsistent")
    ensure_timestamp_not_rolling_back(
        updated_at_ms,
        previous_updated_at_ms,
        "manifest.updated_at_ms",
    )
    route_budget = require_int(
        require_field(manifest, "route_budget", "manifest"),
        "manifest.route_budget",
        minimum=1,
    )
    candidate_plan_count = require_int(
        require_field(manifest, "candidate_plan_count", "manifest"),
        "manifest.candidate_plan_count",
        minimum=1,
    )
    scheduler_workers = require_int(
        require_field(manifest, "scheduler_workers", "manifest"),
        "manifest.scheduler_workers",
        minimum=1,
    )
    if candidate_plan_count > 8:
        raise corrupt("candidate plan count exceeds supported maximum")
    if scheduler_workers > 64:
        raise corrupt("scheduler worker count exceeds supported maximum")
    precision = require_string(
        require_field(manifest, "precision", "manifest"),
        "manifest.precision",
    )
    if precision not in PRECISIONS:
        raise corrupt("unsupported precision", precision=precision)

    metadata_db_name = require_string(
        require_field(manifest, "metadata_db", "manifest"),
        "manifest.metadata_db",
    )
    metadata_checksum = require_sha256(
        require_field(manifest, "metadata_checksum", "manifest"),
        "manifest.metadata_checksum",
    )
    metadata_path = _safe_child(directory, metadata_db_name)
    metadata, tombstones, actual_metadata_checksum = _read_metadata_db(
        metadata_path,
        manifest=manifest,
        expected_manifest_checksum=metadata_checksum,
    )
    generation = metadata["config_generation"]
    revision = metadata["revision"]
    created_at_ms = metadata["created_at_ms"]
    updated_at_ms = metadata["updated_at_ms"]

    shard_declarations = require_field(manifest, "shards", "manifest")
    if not isinstance(shard_declarations, list) or not shard_declarations:
        raise corrupt("manifest.shards must be a non-empty array")

    entries: list[ThresholdEntry] = []
    shard_checksums: list[str] = []
    seen_paths: set[str] = set()
    last_entry_timestamp: int | None = None
    for index, raw_declaration in enumerate(shard_declarations):
        declaration = require_mapping(raw_declaration, f"manifest.shards[{index}]")
        relative_path = require_string(
            require_field(declaration, "path", f"manifest.shards[{index}]"),
            f"manifest.shards[{index}].path",
        )
        expected_checksum = require_sha256(
            require_field(declaration, "sha256", f"manifest.shards[{index}]"),
            f"manifest.shards[{index}].sha256",
        )
        if relative_path in seen_paths:
            raise corrupt("duplicate v2 shard path", path=relative_path)
        seen_paths.add(relative_path)
        shard_path = _safe_child(directory, relative_path)
        if not shard_path.is_file():
            raise corrupt("v2 shard is missing", path=str(shard_path))
        actual_checksum = sha256_file(shard_path)
        if actual_checksum != expected_checksum:
            raise corrupt(
                "v2 shard checksum mismatch",
                path=str(shard_path),
                expected=expected_checksum,
                actual=actual_checksum,
            )
        shard_checksums.append(actual_checksum)
        shard = require_mapping(
            read_json_strict_no_duplicates(shard_path),
            f"shard[{relative_path}]",
        )
        shard_version = sniff_format_version(shard)
        if shard_version != 2:
            raise corrupt("v2 shard declares the wrong version", path=relative_path)
        raw_entries = require_field(shard, "entries", relative_path)
        if not isinstance(raw_entries, list) or not raw_entries:
            raise corrupt("v2 shard entries must be a non-empty array", path=relative_path)
        for entry_index, raw_entry in enumerate(raw_entries):
            entry_path = f"{relative_path}.entries[{entry_index}]"
            entry = require_mapping(raw_entry, entry_path)
            key = require_string(
                require_field(entry, "key", entry_path),
                f"{entry_path}.key",
            )
            if key not in THRESHOLD_KEYS:
                raise corrupt("unsupported threshold key", path=entry_path, key=key)
            entry_revision = require_int(
                require_field(entry, "revision", entry_path),
                f"{entry_path}.revision",
                minimum=0,
            )
            entry_timestamp = require_int(
                require_field(entry, "updated_at_ms", entry_path),
                f"{entry_path}.updated_at_ms",
                minimum=0,
            )
            if last_entry_timestamp is not None:
                ensure_timestamp_not_rolling_back(
                    entry_timestamp,
                    last_entry_timestamp,
                    f"{entry_path}.updated_at_ms",
                )
            last_entry_timestamp = entry_timestamp
            entries.append(
                ThresholdEntry(
                    key=key,
                    threshold=require_threshold(
                        require_field(entry, "threshold", entry_path),
                        f"{entry_path}.threshold",
                    ),
                    revision=entry_revision,
                    updated_at_ms=entry_timestamp,
                )
            )

    entries.sort(key=lambda item: item.key)
    if [entry.key for entry in entries] != list(THRESHOLD_KEYS):
        raise corrupt(
            "v2 shards do not provide exactly one text, image, and joint threshold",
            keys=[entry.key for entry in entries],
        )
    if any(entry.revision > revision for entry in entries):
        raise corrupt("entry revision exceeds snapshot revision")
    if any(entry.updated_at_ms > updated_at_ms for entry in entries):
        raise corrupt("entry timestamp exceeds snapshot timestamp")
    if any(item.revision > revision for item in tombstones):
        raise corrupt("tombstone revision exceeds snapshot revision")
    if any(item.invalidated_at_ms > updated_at_ms for item in tombstones):
        raise corrupt("tombstone timestamp exceeds snapshot timestamp")

    config = ThresholdConfig(
        schema_version=2,
        generation=generation,
        revision=revision,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        entries=tuple(entries),
        tombstones=tombstones,
        route_budget=route_budget,
        candidate_plan_count=candidate_plan_count,
        scheduler_workers=scheduler_workers,
        precision=precision,
    )
    return LoadResult(
        format_version=2,
        config=config,
        metadata_checksum=actual_metadata_checksum,
        shard_checksums=tuple(shard_checksums),
    )


def read_config(
    path: str | Path,
    *,
    previous_updated_at_ms: int | None = None,
) -> LoadResult:
    location = Path(path)
    if location.is_dir():
        return _read_v2_directory(
            location,
            previous_updated_at_ms=previous_updated_at_ms,
        )
    if not location.is_file():
        raise corrupt("configuration path does not exist", path=str(location))
    document = require_mapping(read_json_strict_no_duplicates(location), "document")
    version = sniff_format_version(document)
    if version == 1:
        return _read_v1_document(
            document,
            previous_updated_at_ms=previous_updated_at_ms,
        )
    raise corrupt(
        "v2 sharded configuration must be loaded from a directory",
        path=str(location),
    )


def _write_sqlite_metadata(
    path: Path,
    *,
    generation: int,
    revision: int,
    created_at_ms: int,
    updated_at_ms: int,
    tombstones: Iterable[Tombstone],
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE config_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE tombstones ("
            "key TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
            "invalidated_at_ms INTEGER NOT NULL)"
        )
        metadata = {
            "config_generation": generation,
            "revision": revision,
            "created_at_ms": created_at_ms,
            "updated_at_ms": updated_at_ms,
        }
        connection.executemany(
            "INSERT INTO config_meta(key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in sorted(metadata.items())],
        )
        connection.executemany(
            "INSERT INTO tombstones(key, revision, invalidated_at_ms) VALUES (?, ?, ?)",
            [
                (item.key, item.revision, item.invalidated_at_ms)
                for item in sorted(tombstones, key=lambda item: item.key)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return sha256_file(path)


def write_v2_config(
    config: ThresholdConfig,
    directory: str | Path,
    *,
    previous_updated_at_ms: int | None = None,
) -> Path:
    if config.tombstones:
        # Tombstones are allowed but only after semantic validation of timestamps.
        pass
    ensure_timestamp_not_rolling_back(
        config.updated_at_ms,
        previous_updated_at_ms,
        "config.updated_at_ms",
    )
    root = Path(directory)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_declarations: list[dict[str, Any]] = []
    for entry in config.entries:
        relative = f"shards/{entry.key}.json"
        shard = {
            "schema": "mm-threshold-shard",
            "format_version": 2,
            "shard": f"{entry.key}_threshold",
            "entries": [entry.to_dict()],
        }
        atomic_write_json(root / relative, shard)
        shard_declarations.append(
            {
                "name": f"{entry.key}_threshold",
                "path": relative,
                "sha256": sha256_file(root / relative),
            }
        )
    metadata_path = root / "metadata.sqlite3"
    metadata_checksum = _write_sqlite_metadata(
        metadata_path,
        generation=config.generation,
        revision=config.revision,
        created_at_ms=config.created_at_ms,
        updated_at_ms=config.updated_at_ms,
        tombstones=config.tombstones,
    )
    manifest = {
        "schema": "mm-threshold-config",
        "format_version": 2,
        "generation": config.generation,
        "revision": config.revision,
        "created_at_ms": config.created_at_ms,
        "updated_at_ms": config.updated_at_ms,
        "metadata_db": "metadata.sqlite3",
        "metadata_checksum": metadata_checksum,
        "shards": shard_declarations,
        "route_budget": config.route_budget,
        "candidate_plan_count": config.candidate_plan_count,
        "scheduler_workers": config.scheduler_workers,
        "precision": config.precision,
    }
    atomic_write_json(root / "manifest.json", manifest)
    return root


def write_v1_config(
    document: dict[str, Any],
    path: str | Path,
) -> Path:
    body = require_mapping(require_field(document, "config", "document"), "document.config")
    if set(body) != V1_FIELDS:
        raise corrupt(
            "refusing to write v1 document with non-v1 fields",
            missing=sorted(V1_FIELDS.difference(body)),
            extra=sorted(set(body).difference(V1_FIELDS)),
        )
    require_string(require_field(document, "schema", "document"), "document.schema")
    if sniff_format_version(document) != 1:
        raise corrupt("document is not a v1 configuration")
    for key in THRESHOLD_KEYS:
        require_number(body["thresholds"][key], f"document.config.thresholds.{key}")
    parse_timestamp_ms(body["updated_at"], "document.config.updated_at")
    destination = Path(path)
    atomic_write_json(destination, document)
    return destination


def config_v1_document(config: ThresholdConfig) -> dict[str, Any]:
    return {
        "schema": "mm-threshold-config",
        "format_version": 1,
        "config": {
            "thresholds": {key: config.threshold(key) for key in THRESHOLD_KEYS},
            "route_budget": config.route_budget,
            "candidate_plan_count": config.candidate_plan_count,
            "scheduler_workers": config.scheduler_workers,
            "precision": config.precision,
            "updated_at": format_timestamp_ms(config.updated_at_ms),
        },
    }


def load_and_migrate_v1(
    path: str | Path,
    *,
    previous_updated_at_ms: int | None = None,
) -> tuple[LoadResult, tuple[ProvenanceRecord, ...]]:
    result = read_config(path, previous_updated_at_ms=previous_updated_at_ms)
    if result.format_version != 1:
        raise WorkbenchError(
            "E_VERSION_UNSUPPORTED",
            "load_and_migrate_v1 requires a v1 source",
            {"version": result.format_version},
        )
    return result, result.provenance

