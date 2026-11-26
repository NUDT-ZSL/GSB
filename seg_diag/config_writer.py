"""Deterministic writers for delivered v1 JSON and v2 JSON+SQLite snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .hashing import canonical_bytes
from .models import UnifiedConfig


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(payload) + b"\n")


def write_v1_config(
    path: str | Path, config: UnifiedConfig, *, include_mappings: bool = True
) -> Path:
    path = Path(path)
    payload = {
        "schema": "seg-diag/v1",
        "version": "v1",
        "revision": config.revision,
        "relative_effective_seconds": config.relative_effective_seconds
        if config.relative_effective_seconds is not None
        else config.effective_at_ms / 1000.0,
        "time_origin_ms": config.time_origin_ms,
        "downgrade_policy": config.downgrade_policy,
        "categories": [
            {"id": category.id, "name": category.name} for category in config.categories
        ],
    }
    if include_mappings:
        payload["mappings"] = [
            {
                "instance_id": mapping.instance_id,
                "category_id": mapping.category_id,
                "effective_after_seconds": config.relative_effective_seconds
                if config.relative_effective_seconds is not None
                else mapping.effective_at_ms / 1000.0,
            }
            for mapping in config.mappings
        ]
    payload = {
        key: value
        for key, value in payload.items()
        if key not in {"checksum"}
    }
    payload["checksum"] = config.checksum
    _write_json(path, payload)
    return path


def write_v2_config(path: str | Path, config: UnifiedConfig, *, shard_count: int = 2) -> Path:
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")

    mappings = sorted(config.mappings, key=lambda item: item.instance_id)
    buckets: list[list[Any]] = [[] for _ in range(shard_count)]
    for index, mapping in enumerate(mappings):
        buckets[index % shard_count].append(
            {
                "instance_id": mapping.instance_id,
                "category_id": mapping.category_id,
                "confidence_threshold": mapping.confidence_threshold,
                "effective_at_ms": mapping.effective_at_ms,
                "confidence_scope": mapping.confidence_scope,
            }
        )

    for shard_index, rows in enumerate(buckets):
        payload = {
            "schema": "seg-diag/v2-shard",
            "version": "v2",
            "revision": config.revision,
            "shard": shard_index,
            "shard_count": shard_count,
            "categories": [
                {"id": category.id, "name": category.name}
                for category in sorted(config.categories, key=lambda item: item.id)
            ]
            if shard_index == 0
            else [],
            "mappings": rows,
        }
        _write_json(root / f"shard_{shard_index:03d}.json", payload)

    db_path = root / "metadata.sqlite3"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE config_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE category_instance_map (
                instance_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                confidence_threshold REAL NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                confidence_scope TEXT NOT NULL
            )
            """
        )
        meta = {
            "schema": "seg-diag/v2",
            "version": "v2",
            "revision": str(config.revision),
            "default_confidence_threshold": repr(float(config.default_confidence_threshold)),
            "effective_at_ms": str(config.effective_at_ms),
            "time_origin_ms": str(config.time_origin_ms),
            "downgrade_policy": config.downgrade_policy,
            "checksum": config.checksum,
        }
        connection.executemany(
            "INSERT INTO config_meta(key, value) VALUES (?, ?)", list(meta.items())
        )
        connection.executemany(
            """
            INSERT INTO category_instance_map(
                instance_id, category_id, confidence_threshold,
                effective_at_ms, confidence_scope
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    mapping.instance_id,
                    mapping.category_id,
                    float(mapping.confidence_threshold),
                    int(mapping.effective_at_ms),
                    mapping.confidence_scope,
                )
                for mapping in mappings
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return root
