from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .checkpoints import table_summary
from .errors import AppError, ErrorCode
from .models import CheckpointVersion
from .util import atomic_write_json, canonical_json, sha256_obj


def prepare_v2_version(version: CheckpointVersion) -> None:
    """Materialize deterministic shard files and a manifest for a checkpoint."""
    summary = table_summary(version.snapshot_rows)
    shard_records: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    for table in sorted(summary):
        payload = {
            "format": "walcheckpoint-v2-shard",
            "generation": version.generation,
            "replay_generation": version.replay_generation,
            "table": table,
            "row_count": summary[table]["row_count"],
            "tombstone_count": summary[table]["tombstone_count"],
            "row_versions": summary[table]["row_versions"],
            "tombstones": summary[table]["tombstones"],
            "table_digest": summary[table]["content_digest"],
        }
        payload["shard_hash"] = sha256_obj(payload)
        name = f"gen-{version.generation:04d}-{table}.json"
        shard_records.append(
            {
                "name": name,
                "shard_hash": payload["shard_hash"],
                "table": table,
                "row_count": summary[table]["row_count"],
                "table_digest": summary[table]["content_digest"],
            }
        )
        shards.append(
            {
                "name": name,
                "shard_hash": payload["shard_hash"],
                "table": table,
                "row_count": summary[table]["row_count"],
                "content_digest": summary[table]["content_digest"],
                "payload": payload,
            }
        )
    replay_floor = int(
        version.manifest.get("replay_generation_floor", version.replay_generation)
    )
    manifest = {
        "format": "walcheckpoint-v2-manifest",
        "manifest_version": version.manifest_version,
        "generation": version.generation,
        "replay_generation": version.replay_generation,
        "replay_generation_floor": replay_floor,
        "applied_watermark": version.applied_watermark,
        "saved_at_ms": version.saved_at_ms,
        "base_generation": version.base_generation,
        "base_manifest_hash": version.base_manifest_hash,
        "shard_root_hash": sha256_obj(
            [record["shard_hash"] for record in shard_records]
        ),
        "shards": shard_records,
    }
    manifest_hash = sha256_obj(manifest)
    manifest["manifest_hash"] = manifest_hash
    version.manifest = manifest
    version.manifest_hash = manifest_hash
    version.shards = shards
    version.tables = {
        table: {
            "row_count": summary[table]["row_count"],
            "content_digest": summary[table]["content_digest"],
            "row_versions": summary[table]["row_versions"],
            "tombstones": summary[table]["tombstones"],
        }
        for table in summary
    }


def write_v2_package(
    out_dir: Path,
    versions: list[CheckpointVersion],
    *,
    overwrite_same: bool = True,
) -> dict[str, Any]:
    if not versions:
        raise AppError(
            ErrorCode.CHECKPOINT_INVALID,
            "cannot build an empty v2 checkpoint package",
        )
    for version in versions:
        prepare_v2_version(version)
    versions = sorted(versions, key=lambda item: item.generation)
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=str(out_dir.parent))
    )
    shard_dir = tmp_dir / "shards"
    shard_dir.mkdir()
    db_path = tmp_dir / "metadata.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE checkpoint_meta (
                generation INTEGER PRIMARY KEY,
                replay_generation INTEGER NOT NULL,
                replay_generation_floor INTEGER NOT NULL,
                applied_watermark INTEGER NOT NULL,
                saved_at_ms INTEGER NOT NULL,
                manifest_version INTEGER NOT NULL,
                base_generation INTEGER NOT NULL,
                base_manifest_hash TEXT,
                manifest_hash TEXT NOT NULL,
                manifest_json TEXT NOT NULL
            );
            CREATE TABLE shards (
                generation INTEGER NOT NULL,
                shard_name TEXT NOT NULL,
                shard_hash TEXT NOT NULL,
                payload_path TEXT NOT NULL,
                table_name TEXT NOT NULL,
                PRIMARY KEY (generation, shard_name)
            );
            CREATE TABLE snapshot_rows (
                generation INTEGER NOT NULL,
                table_name TEXT NOT NULL,
                row_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                txn_id INTEGER NOT NULL,
                deleted INTEGER NOT NULL,
                value_json TEXT,
                PRIMARY KEY (generation, table_name, row_key)
            );
            """
        )
        for version in versions:
            for shard in version.shards:
                atomic_write_json(shard_dir / shard["name"], shard["payload"])
                connection.execute(
                    """
                    INSERT INTO shards
                    (generation, shard_name, shard_hash, payload_path,
                     table_name)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        version.generation,
                        shard["name"],
                        shard["shard_hash"],
                        f"shards/{shard['name']}",
                        shard["table"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO checkpoint_meta
                (generation, replay_generation, replay_generation_floor,
                 applied_watermark, saved_at_ms, manifest_version,
                 base_generation, base_manifest_hash, manifest_hash,
                 manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.generation,
                    version.replay_generation,
                    int(
                        version.manifest.get(
                            "replay_generation_floor", version.replay_generation
                        )
                    ),
                    version.applied_watermark,
                    version.saved_at_ms,
                    version.manifest_version,
                    version.base_generation,
                    version.base_manifest_hash,
                    version.manifest_hash,
                    canonical_json(version.manifest),
                ),
            )
            for row in version.snapshot_rows:
                connection.execute(
                    """
                    INSERT INTO snapshot_rows
                    (generation, table_name, row_key, version, txn_id,
                     deleted, value_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.generation,
                        row.table,
                        row.key,
                        row.version,
                        row.txn_id,
                        1 if row.deleted else 0,
                        canonical_json(row.value) if row.value is not None else None,
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    manifest_doc = {
        "format": "walcheckpoint-v2-package",
        "versions": [
            {
                "generation": version.generation,
                "manifest_version": version.manifest_version,
                "manifest_hash": version.manifest_hash,
                "saved_at_ms": version.saved_at_ms,
                "replay_generation": version.replay_generation,
                "applied_watermark": version.applied_watermark,
            }
            for version in versions
        ],
        "package_hash": sha256_obj(
            [version.manifest_hash for version in versions]
        ),
    }
    atomic_write_json(tmp_dir / "manifest.json", manifest_doc)
    for version in versions:
        version.source_path = str(
            Path(out_dir).resolve() / "metadata.sqlite"
        )
        version.source_hash = sha256_obj(
            {
                "manifest_hashes": [
                    item.manifest_hash for item in versions
                ],
                "version": version.generation,
            }
        )
    if out_dir.exists():
        existing_manifest = None
        try:
            existing_manifest = json.loads(
                (out_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            existing_manifest = None
        if (
            overwrite_same
            and existing_manifest is not None
            and existing_manifest.get("package_hash") == manifest_doc["package_hash"]
        ):
            shutil.rmtree(tmp_dir)
            return manifest_doc
        if not overwrite_same:
            shutil.rmtree(tmp_dir)
            raise AppError(
                ErrorCode.CHECKPOINT_CONFLICT,
                "checkpoint package destination already exists",
                {"path": str(out_dir)},
            )
        shutil.rmtree(out_dir)
    os.replace(tmp_dir, out_dir)
    return manifest_doc
