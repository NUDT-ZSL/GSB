from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .hashing import canonical_json, sha256_bytes, sha256_json


LOGICAL_EPOCH_MS = 1_735_689_600_000


def content_checksum(record: dict[str, Any]) -> str:
    keys = (
        "task_id", "image_seed", "image_shape", "text", "prompt",
        "sampler_steps", "noise_scale", "precision_profile", "effective_second",
    )
    return sha256_json({key: record.get(key) for key in keys})


def flatten_v1_record(task: dict[str, Any], effective_second: int) -> dict[str, Any]:
    shape = task["image_shape"]
    return {
        "format": "v1_flat_task_description",
        "task_id": task["task_id"],
        "image_seed": int(task["image_seed"]),
        "image_shape": list(shape),
        "image_height": int(shape[0]),
        "image_width": int(shape[1]),
        "image_channels": int(shape[2]),
        "text": task["text"],
        "prompt": task["prompt"],
        "sampler_steps": int(task["sampler_steps"]),
        "noise_scale": float(task["noise_scale"]),
        "precision_profile": task["precision_profile"],
        "effective_second": int(effective_second),
        "relative_effective_second": int(effective_second),
    }


def write_v1(records: list[dict[str, Any]], path: str | Path) -> dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "format": "task-description-v1",
        "time_semantics": "relative_effective_seconds",
        "record_count": len(records),
        "records": records,
    }
    data = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(data)
    return {"path": str(path), "sha256": sha256_bytes(data), "record_count": len(records)}


def write_v2(records: list[dict[str, Any]], shard_dir: str | Path,
             database_path: str | Path, shard_size: int = 2,
             logical_epoch_ms: int = LOGICAL_EPOCH_MS) -> dict[str, Any]:
    shard_dir = Path(shard_dir)
    database_path = Path(database_path)
    shard_dir.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()

    enriched: list[dict[str, Any]] = []
    shard_manifests: list[dict[str, Any]] = []
    for shard_index in range(0, len(records), shard_size):
        shard_records = []
        shard_name = f"shard-{shard_index // shard_size:04d}.json"
        for original in records[shard_index:shard_index + shard_size]:
            checksum = content_checksum(original)
            effective_second = int(original["effective_second"])
            item = {
                "format": "task-description-v2-record",
                **original,
                "absolute_logical_ms": logical_epoch_ms + effective_second * 1000,
                "content_sha256": checksum,
            }
            shard_records.append(item)
            enriched.append(item)
        shard_path = shard_dir / shard_name
        shard_document = {
            "format": "task-description-v2-shard",
            "shard": shard_name,
            "record_count": len(shard_records),
            "records": shard_records,
        }
        data = (json.dumps(shard_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        shard_path.write_bytes(data)
        shard_manifests.append({
            "shard": shard_name,
            "path": str(shard_path),
            "record_count": len(shard_records),
            "shard_sha256": sha256_bytes(data),
        })

    with sqlite3.connect(database_path) as connection:
        connection.execute("""
            CREATE TABLE task_metadata (
                task_id TEXT PRIMARY KEY,
                shard TEXT NOT NULL,
                effective_second INTEGER NOT NULL,
                absolute_logical_ms INTEGER NOT NULL,
                image_seed INTEGER NOT NULL,
                sampler_steps INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE shard_metadata (
                shard TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                shard_sha256 TEXT NOT NULL
            )
        """)
        for manifest in shard_manifests:
            connection.execute(
                "INSERT INTO shard_metadata VALUES (?, ?, ?, ?)",
                (manifest["shard"], manifest["path"], manifest["record_count"], manifest["shard_sha256"]),
            )
        for item in enriched:
            shard_name = next(
                manifest["shard"] for manifest in shard_manifests
                if any(record["task_id"] == item["task_id"] for record in json.loads(Path(manifest["path"]).read_text(encoding="utf-8"))["records"])
            )
            connection.execute(
                "INSERT INTO task_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["task_id"], shard_name, int(item["effective_second"]),
                    int(item["absolute_logical_ms"]), int(item["image_seed"]),
                    int(item["sampler_steps"]), item["content_sha256"],
                    canonical_json(item).decode("utf-8"),
                ),
            )
        connection.commit()

    return {
        "format": "task-description-v2",
        "shard_dir": str(shard_dir),
        "database_path": str(database_path),
        "shards": shard_manifests,
        "record_count": len(enriched),
        "logical_epoch_ms": logical_epoch_ms,
    }


def load_v2_rows(database_path: str | Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT task_id, shard, effective_second, absolute_logical_ms, "
            "image_seed, sampler_steps, content_sha256 FROM task_metadata ORDER BY task_id"
        ).fetchall()
        return [dict(row) for row in rows]


def load_v1(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != "task-description-v1":
        raise ValueError(f"unsupported v1 description format: {document.get('format')}")
    return document


def flatten_v1_to_v2_records(v1_document: dict[str, Any],
                             logical_epoch_ms: int = LOGICAL_EPOCH_MS) -> list[dict[str, Any]]:
    records = []
    for record in v1_document.get("records", []):
        enriched = dict(record)
        enriched["absolute_logical_ms"] = logical_epoch_ms + int(record["effective_second"]) * 1000
        enriched["content_sha256"] = content_checksum(record)
        records.append(enriched)
    return records


def verify_v2_checksums(database_path: str | Path) -> dict[str, Any]:
    database_path = Path(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT task_id, content_sha256, payload_json FROM task_metadata").fetchall()
        bad = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            expected = content_checksum(payload)
            if expected != row["content_sha256"] or payload["content_sha256"] != expected:
                bad.append({"task_id": row["task_id"], "expected": expected,
                            "actual": row["content_sha256"]})
    return {"ok": not bad, "checked": len(rows), "mismatches": bad}
