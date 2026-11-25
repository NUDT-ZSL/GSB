from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import FeatureConfig, normalize_config
from .errors import E_SNAPSHOT_ROLLBACK, EngineError, snapshot_error
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
)


V2_SHARD_NAMES = (
    "base.json",
    "numeric.json",
    "categorical.json",
    "cross.json",
    "selection.json",
    "resources.json",
)


def _snapshot_wrap(error: Exception, *, source: str, field: str | None = None) -> EngineError:
    if isinstance(error, EngineError):
        detail = copy.deepcopy(error.detail)
        if field is not None:
            detail.setdefault("field", field)
        detail["source"] = source
        return EngineError(E_SNAPSHOT_ROLLBACK, error.message, detail, error.completed_steps)
    return EngineError(
        E_SNAPSHOT_ROLLBACK,
        "snapshot cannot be parsed",
        {"source": source, "field": field, "error": str(error)},
    )


def _sorted_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        fills,
        key=lambda item: (
            str(item.get("field")),
            str(item.get("action")),
            str(item.get("source")),
            canonical_json(item.get("value")),
        ),
    )


def _load_v1_json(path: Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except Exception as error:
        raise _snapshot_wrap(error, source=str(path), field="json") from None
    if not isinstance(raw, dict):
        raise snapshot_error("v1 snapshot root must be an object", source=str(path), field="json")
    return raw


def _checksum_for_v1(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def load_v1(path: Path, *, checksum: str | None = None, require_checksum: bool = True) -> FeatureConfig:
    if not path.exists():
        raise snapshot_error("v1 snapshot does not exist", source=str(path), field="path")
    sidecar = _checksum_for_v1(path)
    expected = checksum
    if expected is None and sidecar.exists():
        try:
            expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        except Exception as error:
            raise _snapshot_wrap(error, source=str(sidecar), field="checksum") from None
    if expected is None:
        if require_checksum:
            raise snapshot_error("v1 checksum sidecar is missing", source=str(path), field="checksum")
    else:
        actual = sha256_file(path)
        if actual != expected:
            raise snapshot_error(
                "v1 snapshot checksum mismatch",
                source=str(path),
                field="checksum",
                expected=expected,
                actual=actual,
            )
    raw = _load_v1_json(path)
    if raw.get("schema") != "feature_config/v1":
        raise snapshot_error(
            "v1 snapshot schema is invalid",
            source=str(path),
            field="schema",
            value=raw.get("schema"),
        )
    fills: list[dict[str, Any]] = []
    try:
        return normalize_config(
            raw,
            expected_schema="feature_config/v1",
            fill_sources=fills,
            default_snapshot_id=path.stem,
        )
    except Exception as error:
        raise _snapshot_wrap(error, source=str(path)) from None


def write_v1(config: FeatureConfig, path: Path) -> dict[str, Any]:
    payload = config.to_v1_dict()
    data = canonical_json(payload, newline=True).encode("utf-8")
    atomic_write_bytes(path, data)
    digest = sha256_bytes(data)
    atomic_write_bytes(_checksum_for_v1(path), (digest + "\n").encode("utf-8"))
    return {
        "path": str(path),
        "checksum": digest,
        "snapshot_version": config.snapshot_version,
        "schema": "feature_config/v1",
    }


def _read_sqlite_meta(database: Path) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0)
        try:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
            shard_rows = connection.execute("SELECT name, file, digest FROM shards").fetchall()
        finally:
            connection.close()
    except Exception as error:
        raise _snapshot_wrap(error, source=str(database), field="metadata") from None
    metadata: dict[str, Any] = {}
    try:
        for key, value in rows:
            metadata[str(key)] = json.loads(value)
    except Exception as error:
        raise _snapshot_wrap(error, source=str(database), field="metadata") from None
    shards = {
        str(name): {"file": str(file_name), "digest": str(digest)}
        for name, file_name, digest in shard_rows
    }
    return metadata, shards


def load_v2(directory: Path, *, checksum: str | None = None, require_checksum: bool = True) -> FeatureConfig:
    database = directory / "index.sqlite"
    if not database.exists():
        raise snapshot_error("v2 metadata database is missing", source=str(directory), field="index.sqlite")
    metadata, shard_meta = _read_sqlite_meta(database)
    required_meta = {
        "schema",
        "snapshot_id",
        "snapshot_version",
        "effective_at_ms",
        "epoch_ms",
        "config_digest",
        "fill_sources",
    }
    missing = sorted(required_meta - set(metadata))
    if missing:
        raise snapshot_error("v2 metadata is incomplete", source=str(database), field="metadata", missing=missing)
    if metadata["schema"] != "feature_config/v2":
        raise snapshot_error("v2 metadata schema is invalid", source=str(database), field="schema")
    if set(shard_meta) != set(V2_SHARD_NAMES):
        raise snapshot_error(
            "v2 shard manifest is incomplete or contains unknown shards",
            source=str(database),
            field="shards",
            expected=sorted(V2_SHARD_NAMES),
            actual=sorted(shard_meta),
        )
    parts: dict[str, Any] = {}
    shard_digests: dict[str, str] = {}
    for name in V2_SHARD_NAMES:
        info = shard_meta[name]
        shard_path = directory / info["file"]
        if not shard_path.exists():
            raise snapshot_error("v2 shard is missing", source=str(shard_path), field=name)
        actual = sha256_file(shard_path)
        if actual != info["digest"]:
            raise snapshot_error(
                "v2 shard checksum mismatch",
                source=str(shard_path),
                field=name,
                expected=info["digest"],
                actual=actual,
            )
        try:
            parts[name] = read_json(shard_path)
        except Exception as error:
            raise _snapshot_wrap(error, source=str(shard_path), field=name) from None
        shard_digests[name] = actual
    config_digest = sha256_json(parts)
    if config_digest != metadata["config_digest"]:
        raise snapshot_error(
            "v2 config digest mismatch",
            source=str(database),
            field="config_digest",
            expected=metadata["config_digest"],
            actual=config_digest,
        )
    if checksum is not None and checksum != config_digest:
        raise snapshot_error(
            "v2 caller checksum mismatch",
            source=str(database),
            field="checksum",
            expected=checksum,
            actual=config_digest,
        )
    merged: dict[str, Any] = {
        "schema": "feature_config/v2",
        "snapshot_id": metadata["snapshot_id"],
        "snapshot_version": metadata["snapshot_version"],
        "effective_at_ms": metadata["effective_at_ms"],
        "epoch_ms": metadata["epoch_ms"],
    }
    try:
        base = parts["base.json"]
        for key, value in base.items():
            merged[key] = value
        merged["numeric"] = parts["numeric.json"]["numeric"]
        merged["categorical"] = parts["categorical.json"]["categorical"]
        merged["cross"] = parts["cross.json"]["cross"]
        merged["selector"] = parts["selection.json"]["selector"]
        merged["limits"] = parts["resources.json"]["limits"]
        merged["resource_policy"] = parts["resources.json"]["resource_policy"]
        if not isinstance(metadata["fill_sources"], list):
            raise ValueError("fill_sources must be an array")
    except Exception as error:
        raise _snapshot_wrap(error, source=str(directory), field="shards") from None
    fills = copy.deepcopy(metadata["fill_sources"])
    try:
        config = normalize_config(
            merged,
            expected_schema="feature_config/v2",
            fill_sources=fills,
            default_snapshot_id=metadata["snapshot_id"],
        )
    except Exception as error:
        raise _snapshot_wrap(error, source=str(directory)) from None
    config.completion_sources = _sorted_fills(fills)
    return config


def _write_v2_database(
    database: Path,
    config: FeatureConfig,
    parts: dict[str, Any],
    shard_digests: dict[str, str],
) -> None:
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(str(database), timeout=0)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("CREATE TABLE shards (name TEXT PRIMARY KEY, file TEXT NOT NULL, digest TEXT NOT NULL)")
        metadata = {
            "schema": "feature_config/v2",
            "snapshot_id": config.snapshot_id,
            "snapshot_version": config.snapshot_version,
            "effective_at_ms": config.effective_at_ms,
            "epoch_ms": config.epoch_ms,
            "config_digest": sha256_json(parts),
            "fill_sources": sorted(config.completion_sources, key=lambda item: item["field"]),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [(key, canonical_json(value)) for key, value in metadata.items()],
        )
        connection.executemany(
            "INSERT INTO shards(name, file, digest) VALUES (?, ?, ?)",
            [(name, name, shard_digests[name]) for name in V2_SHARD_NAMES],
        )
        connection.commit()
    finally:
        connection.close()


def write_v2(config: FeatureConfig, directory: Path) -> dict[str, Any]:
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = directory.parent / f".{directory.name}.tmp-{os.getpid()}"
    backup = directory.parent / f".{directory.name}.bak-{os.getpid()}"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        parts = config.to_v2_parts()
        shard_digests: dict[str, str] = {}
        for name in V2_SHARD_NAMES:
            data = canonical_json(parts[name], newline=True).encode("utf-8")
            atomic_write_bytes(temporary / name, data)
            shard_digests[name] = sha256_bytes(data)
        _write_v2_database(temporary / "index.sqlite", config, parts, shard_digests)
        if directory.exists():
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(directory, backup)
        os.replace(temporary, directory)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and not directory.exists():
            os.replace(backup, directory)
        raise
    final_parts = {name: read_json(directory / name) for name in V2_SHARD_NAMES}
    return {
        "path": str(directory),
        "config_digest": sha256_json(final_parts),
        "snapshot_version": config.snapshot_version,
        "schema": "feature_config/v2",
        "shards": list(V2_SHARD_NAMES),
    }


def _existing_version(path: Path, schema: str) -> int | None:
    try:
        if schema == "feature_config/v2":
            metadata, _ = _read_sqlite_meta(path / "index.sqlite")
            value = metadata.get("snapshot_version")
            return int(value) if isinstance(value, (int, float)) else None
        if path.exists():
            raw = _load_v1_json(path)
            value = raw.get("snapshot_version")
            return int(value) if isinstance(value, (int, float)) else None
    except Exception:
        return None
    return None


def _assert_rollback(
    *,
    source_version: int,
    target_version: int,
    target_path: Path,
    target_schema: str,
) -> None:
    if target_version < source_version:
        raise snapshot_error(
            "snapshot version rollback is forbidden",
            field="snapshot_version",
            source_version=source_version,
            target_version=target_version,
        )
    current = _existing_version(target_path, target_schema)
    if current is not None and target_version < current:
        raise snapshot_error(
            "snapshot version rollback is forbidden",
            field="snapshot_version",
            current_version=current,
            target_version=target_version,
            target=str(target_path),
        )


def migrate_v1_to_v2(
    source_path: Path,
    target_directory: Path,
    *,
    target_version: int | None = None,
    checksum: str | None = None,
) -> tuple[FeatureConfig, dict[str, Any]]:
    config = load_v1(source_path, checksum=checksum, require_checksum=True)
    target_version = 2 if target_version is None else target_version
    _assert_rollback(
        source_version=config.snapshot_version,
        target_version=target_version,
        target_path=target_directory,
        target_schema="feature_config/v2",
    )
    fills = copy.deepcopy(config.completion_sources)
    if not any(item.get("field") == "effective_at_ms" for item in fills):
        fills.append(
            {
                "field": "effective_at_ms",
                "action": "derived",
                "source": "epoch_ms+effective_at_sec*1000",
                "value": config.effective_at_ms,
            }
        )
    fills.append(
        {
            "field": "schema",
            "action": "migrated",
            "source": "feature_config/v1",
            "value": "feature_config/v2",
        }
    )
    fills.append(
        {
            "field": "snapshot_version",
            "action": "migrated",
            "source": "target_version",
            "value": target_version,
        }
    )
    migrated = config.clone()
    migrated.schema = "feature_config/v2"
    migrated.snapshot_version = target_version
    migrated.completion_sources = _sorted_fills(fills)
    result = write_v2(migrated, target_directory)
    result["fill_sources"] = copy.deepcopy(migrated.completion_sources)
    result["effective_at_ms"] = migrated.effective_at_ms
    return migrated, result


def migrate_v2_to_v1(
    source_directory: Path,
    target_path: Path,
    *,
    target_version: int | None = None,
    checksum: str | None = None,
) -> tuple[FeatureConfig, dict[str, Any]]:
    config = load_v2(source_directory, checksum=checksum, require_checksum=True)
    target_version = config.snapshot_version if target_version is None else target_version
    _assert_rollback(
        source_version=config.snapshot_version,
        target_version=target_version,
        target_path=target_path,
        target_schema="feature_config/v1",
    )
    fills = copy.deepcopy(config.completion_sources)
    if not any(item.get("field") == "effective_at_sec" for item in fills):
        fills.append(
            {
                "field": "effective_at_sec",
                "action": "derived",
                "source": "(effective_at_ms-epoch_ms)/1000",
                "value": round(config.effective_at_sec, 9),
            }
        )
    fills.append(
        {
            "field": "schema",
            "action": "migrated",
            "source": "feature_config/v2",
            "value": "feature_config/v1",
        }
    )
    migrated = config.clone()
    migrated.schema = "feature_config/v1"
    migrated.snapshot_version = target_version
    migrated.completion_sources = _sorted_fills(fills)
    result = write_v1(migrated, target_path)
    result["fill_sources"] = copy.deepcopy(migrated.completion_sources)
    result["effective_at_sec"] = round(migrated.effective_at_sec, 9)
    return migrated, result


def configs_semantically_equal(left: FeatureConfig, right: FeatureConfig) -> bool:
    return left.semantic_digest == right.semantic_digest
