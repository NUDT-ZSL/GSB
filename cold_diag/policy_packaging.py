from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .utils import sha256_file, stable_key_digest, write_json


def write_v2_package(
    package_dir: str | Path,
    records: list[dict[str, Any]],
    *,
    version: int,
    generation_floor: int | None = None,
    update_mode: str = "full",
    base_version: int | None = None,
    extra_manifest: dict[str, Any] | None = None,
) -> Path:
    root = Path(package_dir)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_path = shard_dir / "shard-000.json"
    generations = [int(record["generation"]) for record in records]
    if generations != sorted(generations):
        records = sorted(records, key=lambda item: (int(item["generation"]), str(item["key"])))
        generations = [int(record["generation"]) for record in records]

    write_json(shard_path, {"shard": 0, "records": records})
    shard_hash = sha256_file(shard_path)
    keys = [str(record["key"]) for record in records]
    key_digest = stable_key_digest(keys)
    floor = generation_floor if generation_floor is not None else min(generations, default=1)

    manifest: dict[str, Any] = {
        "format": "v2",
        "version": version,
        "package_version": version,
        "generation_floor": floor,
        "metadata_db": "metadata.sqlite",
        "update_mode": update_mode,
        "key_set_digest": key_digest,
        "shards": [
            {
                "path": "shards/shard-000.json",
                "sha256": shard_hash,
                "generation_min": min(generations),
                "generation_max": max(generations),
            }
        ],
    }
    if base_version is not None:
        manifest["base_version"] = base_version
    if extra_manifest:
        manifest.update(extra_manifest)
    write_json(root / "manifest.json", manifest)

    db_path = root / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE policy_metadata (
                key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0,
                shard_path TEXT NOT NULL,
                shard_hash TEXT NOT NULL,
                PRIMARY KEY(key, generation)
            )
            """
        )
        for record in records:
            conn.execute(
                """
                INSERT INTO policy_metadata(
                    key, generation, effective_at_ms, tombstone,
                    shard_path, shard_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["key"],
                    int(record["generation"]),
                    int(record["effective_at_ms"]),
                    1 if record.get("tombstone") else 0,
                    "shards/shard-000.json",
                    shard_hash,
                ),
            )
        conn.commit()
    return root / "manifest.json"
