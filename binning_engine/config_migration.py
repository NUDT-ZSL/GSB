"""v1 flat JSON and v2 sharded JSON + SQLite snapshot migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .codes import EngineError, ErrorCode
from .config import normalize_config
from .hashing import canonical_hash, read_json, write_atomic_json


V1_SCHEMA = "binning-config-v1"
V2_SCHEMA = "binning-config-v2-sharded"


def relative_seconds_to_absolute_ms(origin_absolute_ms: int, relative_seconds: float) -> int:
    return int(origin_absolute_ms) + int(round(float(relative_seconds) * 1000.0))


def absolute_ms_to_relative_seconds(origin_absolute_ms: int, absolute_ms: int):
    delta_ms = int(absolute_ms) - int(origin_absolute_ms)
    if delta_ms % 1000 == 0:
        return delta_ms // 1000
    return delta_ms / 1000.0


def canonical_to_v1(config: Mapping[str, Any]) -> Dict[str, Any]:
    config = normalize_config(config)
    first_name = config["numeric_columns"][0]
    first_spec = config["column_specs"][first_name]
    origin = int(config["time_origin_absolute_ms"])
    absolute_ms = config.get("absolute_ms")
    if absolute_ms is None:
        relative_seconds = config.get("recorded_at_relative_seconds", 0)
        absolute_ms = relative_seconds_to_absolute_ms(origin, relative_seconds)
    else:
        relative_seconds = absolute_ms_to_relative_seconds(origin, int(absolute_ms))

    column_params = []
    for name in config["numeric_columns"]:
        spec = config["column_specs"][name]
        column_params.append(
            {
                "column": name,
                "bin_count": spec["bin_count"],
                "bin_method": spec["bin_method"],
                "encoding_method": spec["encoding_method"],
                "smoothing": spec["smoothing"],
                "quant_scale": spec["quant_scale"],
                "zero_point": spec["zero_point"],
            }
        )

    return {
        "schema": V1_SCHEMA,
        "config_name": config["config_name"],
        "version": int(config["version"]),
        "seed": config.get("seed"),
        "row_count": config.get("declared_row_count"),
        "time_origin_absolute_ms": origin,
        "recorded_at_relative_seconds": relative_seconds,
        "relative_seconds": relative_seconds,
        "numeric_columns": list(config["numeric_columns"]),
        "categorical_columns": list(config["categorical_columns"]),
        "label_column": config["label_column"],
        "vocabulary": dict(config["vocabulary"]),
        "bin_count": first_spec["bin_count"],
        "bin_method": first_spec["bin_method"],
        "encoding_method": config["encoding"]["default_method"],
        "smoothing": config["encoding"]["smoothing"],
        "quant_scale": first_spec["quant_scale"],
        "zero_point": first_spec["zero_point"],
        "precision_tier": config["precision_tier"],
        "column_params": column_params,
        "scheduler": dict(config["scheduler"]),
        "resource_budget": dict(config["resource_budget"]),
        "priority": list(config["priority"]),
        "degradation_policy": dict(config["degradation_policy"]),
    }


def _shards(config: Mapping[str, Any], absolute_ms: int, relative_seconds: Any):
    core = {
        "normalized": True,
        "schema_version": "2.0",
        "sharded_schema": V2_SCHEMA,
        "config_name": config["config_name"],
        "version": int(config["version"]),
        "seed": config.get("seed"),
        "declared_row_count": config.get("declared_row_count"),
        "time_origin_absolute_ms": int(config["time_origin_absolute_ms"]),
        "absolute_ms": int(absolute_ms),
        "recorded_at_relative_seconds": relative_seconds,
        "relative_seconds": relative_seconds,
        "numeric_columns": list(config["numeric_columns"]),
        "categorical_columns": list(config["categorical_columns"]),
        "label_column": config["label_column"],
        "vocabulary": dict(config["vocabulary"]),
    }
    columns = {"column_specs": dict(config["column_specs"])}
    policy = {
        "encoding": dict(config["encoding"]),
        "precision_tier": config["precision_tier"],
        "scheduler": dict(config["scheduler"]),
        "resource_budget": dict(config["resource_budget"]),
        "priority": list(config["priority"]),
        "degradation_policy": dict(config["degradation_policy"]),
    }
    return [
        ("00_core.json", "core", core),
        ("01_columns.json", "columns", columns),
        ("02_policy.json", "policy", policy),
    ]


def write_v2_snapshot(
    directory: Path,
    config: Mapping[str, Any],
    *,
    absolute_ms: Optional[int] = None,
) -> Dict[str, Any]:
    config = normalize_config(config)
    directory = Path(directory)
    shard_dir = directory / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    origin = int(config["time_origin_absolute_ms"])
    relative_seconds = config.get("recorded_at_relative_seconds", 0)
    if absolute_ms is None:
        absolute_ms = config.get("absolute_ms")
    if absolute_ms is None:
        absolute_ms = relative_seconds_to_absolute_ms(origin, relative_seconds)
    absolute_ms = int(absolute_ms)
    relative_seconds = absolute_ms_to_relative_seconds(origin, absolute_ms)

    shards = _shards(config, absolute_ms, relative_seconds)
    merged: Dict[str, Any] = {}
    shard_rows = []
    for index, (filename, name, data) in enumerate(shards):
        merged.update(data)
        digest = canonical_hash(data)
        envelope = {
            "shard_index": index,
            "shard_name": name,
            "checksum": digest,
            "data": data,
        }
        write_atomic_json(shard_dir / filename, envelope)
        shard_rows.append((index, f"shards/{filename}", digest))

    content_digest = canonical_hash(merged)
    db_path = directory / "metadata.sqlite3"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            """
            CREATE TABLE snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                config_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                origin_absolute_ms INTEGER NOT NULL,
                absolute_ms INTEGER NOT NULL,
                relative_seconds REAL NOT NULL,
                content_digest TEXT NOT NULL,
                shard_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE shards (
                shard_index INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                digest TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO snapshot VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config["config_name"],
                int(config["version"]),
                origin,
                absolute_ms,
                float(relative_seconds),
                content_digest,
                len(shard_rows),
            ),
        )
        connection.executemany("INSERT INTO shards VALUES (?, ?, ?)", shard_rows)
        connection.commit()
    finally:
        connection.close()

    return {
        "config_name": config["config_name"],
        "version": int(config["version"]),
        "origin_absolute_ms": origin,
        "absolute_ms": absolute_ms,
        "relative_seconds": relative_seconds,
        "content_digest": content_digest,
        "shard_count": len(shard_rows),
        "metadata_db": "metadata.sqlite3",
    }


def read_v2_snapshot(directory: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    directory = Path(directory)
    db_path = directory / "metadata.sqlite3"
    if not db_path.exists():
        raise EngineError(
            ErrorCode.E_MIGRATION, "v2 metadata.sqlite3 is missing", {"directory": str(directory)}
        )
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            """
            SELECT config_name, version, origin_absolute_ms, absolute_ms,
                   relative_seconds, content_digest, shard_count
            FROM snapshot WHERE id = 1
            """
        ).fetchone()
        if row is None:
            raise EngineError(ErrorCode.E_MIGRATION, "v2 snapshot table is empty")
        shard_rows = connection.execute(
            "SELECT shard_index, path, digest FROM shards ORDER BY shard_index"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise EngineError(ErrorCode.E_MIGRATION, f"cannot read v2 metadata: {error}")
    finally:
        connection.close()

    config_name, version, origin, absolute_ms, relative_seconds, digest, shard_count = row
    if len(shard_rows) != int(shard_count):
        raise EngineError(ErrorCode.E_MIGRATION, "v2 shard count does not match metadata")

    merged: Dict[str, Any] = {}
    for shard_index, relative_path, expected_digest in shard_rows:
        path = directory / relative_path
        envelope = read_json(path)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        actual_digest = canonical_hash(data)
        if envelope.get("checksum") != expected_digest or actual_digest != expected_digest:
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                "v2 shard checksum mismatch",
                {"shard_index": shard_index, "path": relative_path},
            )
        merged.update(data)

    actual_digest = canonical_hash(merged)
    if actual_digest != digest:
        raise EngineError(
            ErrorCode.E_MIGRATION,
            "v2 merged content digest mismatch",
            {"expected": digest, "actual": actual_digest},
        )
    merged["absolute_ms"] = int(absolute_ms)
    merged["time_origin_absolute_ms"] = int(origin)
    merged["recorded_at_relative_seconds"] = relative_seconds
    return merged, {
        "config_name": config_name,
        "version": int(version),
        "origin_absolute_ms": int(origin),
        "absolute_ms": int(absolute_ms),
        "relative_seconds": relative_seconds,
        "content_digest": digest,
    }


def migrate_v1_to_v2(v1_path: Path, v2_directory: Path) -> Dict[str, Any]:
    v1 = read_json(v1_path)
    if not isinstance(v1, Mapping):
        raise EngineError(ErrorCode.E_MIGRATION, "v1 config must be a JSON object")
    config = normalize_config(v1)
    return write_v2_snapshot(v2_directory, config)


def migrate_v2_to_v1(v2_directory: Path, v1_path: Path) -> Dict[str, Any]:
    config, metadata = read_v2_snapshot(v2_directory)
    v1 = canonical_to_v1(config)
    v1["recorded_at_relative_seconds"] = metadata["relative_seconds"]
    write_atomic_json(v1_path, v1)
    return v1
