#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
KIB = 1024
EPOCH_CLOCK_MS = 1_700_000_000_000
REFERENCE_CLOCK_MS = EPOCH_CLOCK_MS + 600_000
SEED = 0x5EED_42


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value), encoding="utf-8")


def deterministic_blob(key: str, size: int) -> bytes:
    key_seed = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16)
    rng = np.random.default_rng((SEED + key_seed) % (2**63))
    return rng.bytes(size)


def object_specs() -> list[dict[str, Any]]:
    now = REFERENCE_CLOCK_MS
    specs = [
        ("obj-0001", 96 * KIB, 0, 40, 80, "cold"),
        ("obj-0002", 64 * KIB, 60_000, 32, 70, "cold"),
        ("obj-0003", 48 * KIB, 120_000, 30, 60, "cold"),
        ("obj-0004", 128 * KIB, 600_000, 2, 10, "hot"),
        ("obj-0005", 80 * KIB, 540_000, 4, 20, "hot"),
        ("obj-0006", 32 * KIB, 480_000, 6, 30, "hot"),
        ("obj-0007", 32 * KIB + 1, 480_000, 6, 30, "hot"),
        ("obj-0008", 192 * KIB, 600_000, 0, 0, "hot"),
        ("obj-0009", 72 * KIB, 300_000, 10, 40, "hot"),
        ("obj-0010", 112 * KIB, 300_000, 10, 40, "cold"),
        ("obj-0011", 64 * KIB, 0, 40, 80, "hot"),
        ("obj-0012", 120 * KIB, 600_000, 2, 10, "cold"),
    ]
    objects: list[dict[str, Any]] = []
    blob_dir = SAMPLES / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    for key, size, age_ms, count, weight, tier in specs:
        content = deterministic_blob(key, size)
        blob_path = blob_dir / f"{key}.bin"
        blob_path.write_bytes(content)
        objects.append(
            {
                "key": key,
                "size_bytes": size,
                "last_access_clock_ms": now - age_ms,
                "access_count": count,
                "heat_weight": weight,
                "declared_tier": tier,
                "content_digest": f"sha256:{sha256_hex(content)}",
                "blob_path": f"blobs/{key}.bin",
            }
        )
    return objects


def generate_manifest() -> dict[str, Any]:
    manifest = {
        "manifest_type": "tierflow-object-manifest",
        "manifest_version": 1,
        "seed": SEED,
        "reference_clock_ms": REFERENCE_CLOCK_MS,
        "object_count": 12,
        "objects": object_specs(),
    }
    write_json(SAMPLES / "manifest.json", manifest)
    return manifest


def generate_config() -> dict[str, Any]:
    config = {
        "config_type": "tierflow-tier-config",
        "config_version": 1,
        "reference_clock_ms": REFERENCE_CLOCK_MS,
        "hot_capacity_bytes": 768 * KIB,
        "cold_capacity_bytes": 1024 * KIB,
        "migration_byte_budget": 528 * KIB,
        "max_concurrency": 4,
        "score_scale_bp": 10_000,
        "hot_threshold_bp": 7_000,
        "cold_threshold_bp": 3_500,
        "clock_rollback_tolerance_ms": 5_000,
        "recency_window_ms": 600_000,
        "frequency_unit_bp": 200,
        "backoff": {
            "initial_delay_ms": 25,
            "max_delay_ms": 2_000,
            "multiplier": 2,
        },
        "cost_model": {
            "fixed_point_scale": 1_000_000,
            "bandwidth_bytes_per_ms": 4_096,
            "complexity_multiplier_num": 105,
            "complexity_multiplier_den": 100,
            "fixed_overhead_scaled": 250_000,
            "difference_tolerance_scaled": 1_000,
        },
    }
    write_json(SAMPLES / "config.json", config)
    return config


def generate_events(manifest: dict[str, Any]) -> dict[str, Any]:
    keys = [obj["key"] for obj in manifest["objects"]]
    events = {
        "events_type": "tierflow-migration-events",
        "events": [
            {"seq": 1, "object_key": keys[0], "event_type": "requested", "clock_ms": EPOCH_CLOCK_MS + 10_000, "round_id": "seed-round-0"},
            {"seq": 2, "object_key": keys[0], "event_type": "succeeded", "clock_ms": EPOCH_CLOCK_MS + 11_000, "round_id": "seed-round-0"},
            {"seq": 3, "object_key": keys[3], "event_type": "requested", "clock_ms": EPOCH_CLOCK_MS + 20_000, "round_id": "seed-round-0"},
            {"seq": 4, "object_key": keys[3], "event_type": "failed", "clock_ms": EPOCH_CLOCK_MS + 21_000, "round_id": "seed-round-0", "reason": "deterministic historical checksum busy state"},
            {"seq": 5, "object_key": keys[4], "event_type": "requested", "clock_ms": EPOCH_CLOCK_MS + 30_000},
            {"seq": 6, "object_key": keys[5], "event_type": "requested", "clock_ms": EPOCH_CLOCK_MS + 40_000},
            {"seq": 7, "object_key": keys[6], "event_type": "requested", "clock_ms": EPOCH_CLOCK_MS + 40_500},
            {"seq": 8, "object_key": keys[7], "event_type": "abandoned", "clock_ms": EPOCH_CLOCK_MS + 50_000, "reason": "seed historical budget boundary"},
        ],
    }
    write_json(SAMPLES / "migration_events.json", events)
    return events


def generate_v1_policy() -> dict[str, Any]:
    policy = {
        "format": "v1",
        "schema": "tierflow-policy-v1-flat",
        "package_version": "v1-flat-2026.11",
        "manifest_version": 10,
        "generation": 4,
        "declared_clock_ms": EPOCH_CLOCK_MS + 90_000,
        "effective_after_seconds": "1.000",
        "policies": {
            "heat.hot_threshold": {
                "value": 7_000,
                "declared_clock_ms": EPOCH_CLOCK_MS + 200_000,
                "generation": 4,
                "effective_after_seconds": "0.333333",
            },
            "heat.cold_threshold": {
                "value": 3_500,
                "declared_clock_ms": EPOCH_CLOCK_MS + 150_000,
                "generation": 4,
                "effective_after_seconds": "0.1239",
            },
            "scheduler.byte_budget": {
                "value": 528 * KIB,
                "declared_clock_ms": EPOCH_CLOCK_MS + 140_000,
                "generation": 4,
                "effective_after_seconds": 2,
            },
            "cost.bandwidth_bytes_per_ms": {
                "value": 4_096,
                "declared_clock_ms": EPOCH_CLOCK_MS + 160_000,
                "generation": 4,
                "effective_after_seconds": "0.5",
            },
            "archive.check_interval_seconds": {
                "value": 300,
                "declared_clock_ms": EPOCH_CLOCK_MS + 170_000,
                "generation": 4,
                "effective_after_seconds": "15.25",
            },
            "experimental.legacy_prefetch": {
                "value": False,
                "declared_clock_ms": EPOCH_CLOCK_MS + 100_000,
                "generation": 4,
                "effective_after_seconds": 0,
            },
        },
    }
    write_json(SAMPLES / "policies" / "policy-v1.json", policy)
    return policy


def shard_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shard_zero = [
        {
            "policy_key": "heat.hot_threshold",
            "value": 7_000,
            "declared_clock_ms": EPOCH_CLOCK_MS + 100_000,
            "generation": 4,
            "effective_at_ms": EPOCH_CLOCK_MS + 100_250,
        },
        {
            "policy_key": "heat.cold_threshold",
            "value": 3_500,
            "declared_clock_ms": EPOCH_CLOCK_MS + 150_000,
            "generation": 4,
            "effective_at_ms": EPOCH_CLOCK_MS + 150_250,
        },
        {
            "policy_key": "scheduler.byte_budget",
            "value": 528 * KIB,
            "declared_clock_ms": EPOCH_CLOCK_MS + 250_000,
            "generation": 4,
            "effective_at_ms": EPOCH_CLOCK_MS + 251_000,
        },
    ]
    shard_one = [
        {
            "policy_key": "cost.bandwidth_bytes_per_ms",
            "value": 4_096,
            "declared_clock_ms": EPOCH_CLOCK_MS + 120_000,
            "generation": 4,
            "effective_at_ms": EPOCH_CLOCK_MS + 120_000,
        },
        {
            "policy_key": "verify.parallel_hash_workers",
            "value": 2,
            "declared_clock_ms": EPOCH_CLOCK_MS + 180_000,
            "generation": 4,
            "effective_at_ms": EPOCH_CLOCK_MS + 180_500,
        },
    ]
    return shard_zero, shard_one


def generate_v2_policy() -> None:
    v2_dir = SAMPLES / "policies" / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    shard_zero, shard_one = shard_records()
    shards = [
        ("shard-00.json", {"format": "v2", "shard_id": "shard-00", "records": shard_zero}),
        ("shard-01.json", {"format": "v2", "shard_id": "shard-01", "records": shard_one}),
    ]
    shard_hashes: list[tuple[str, str, str]] = []
    for filename, payload in shards:
        path = v2_dir / filename
        write_json(path, payload)
        shard_hashes.append((filename, filename, sha256_hex(path.read_bytes())))

    meta_path = v2_dir / "policy-v2-meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(meta_path)
    try:
        conn.executescript(
            """
            CREATE TABLE policy_package (
                id INTEGER PRIMARY KEY CHECK (id=1),
                package_version TEXT NOT NULL,
                policy_manifest_version INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                generation_floor INTEGER NOT NULL,
                absolute_effective_ms INTEGER NOT NULL,
                object_set_note TEXT
            );
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL
            );
            CREATE TABLE policy_records (
                policy_key TEXT PRIMARY KEY,
                absolute_effective_ms INTEGER NOT NULL,
                declared_clock_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                value_json TEXT NOT NULL,
                shard_id TEXT NOT NULL
            );
            CREATE TABLE tombstones (
                policy_key TEXT PRIMARY KEY,
                absolute_effective_ms INTEGER NOT NULL,
                declared_clock_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO policy_package VALUES(1, ?, ?, ?, ?, ?, ?)",
            (
                "v2-sharded-2026.11",
                10,
                4,
                1,
                EPOCH_CLOCK_MS + 250_000,
                "object set digest is checked at merge time",
            ),
        )
        conn.executemany("INSERT INTO shards VALUES(?, ?, ?)", shard_hashes)
        for shard_id, records in [("shard-00", shard_zero), ("shard-01", shard_one)]:
            for record in records:
                conn.execute(
                    "INSERT INTO policy_records VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        record["policy_key"],
                        record["effective_at_ms"],
                        record["declared_clock_ms"],
                        record["generation"],
                        json.dumps(record["value"], sort_keys=True),
                        shard_id,
                    ),
                )
        conn.execute(
            "INSERT INTO tombstones VALUES(?, ?, ?, ?, ?)",
            (
                "experimental.legacy_prefetch",
                EPOCH_CLOCK_MS + 300_000,
                EPOCH_CLOCK_MS + 300_000,
                4,
                "prefetch path removed after deterministic benchmark",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def generate_update_packages() -> None:
    pkg_dir = SAMPLES / "policy_packages"
    write_json(
        pkg_dir / "append-v11.json",
        {
            "package_type": "tierflow-policy-update",
            "update_mode": "incremental",
            "manifest_version": 11,
            "generation": 5,
            "generation_floor": 1,
            "package_version": "append-v11",
            "entries": {
                "resume.dead_owner_grace_ms": {
                    "value": 1_000,
                    "source_format": "package",
                    "source_version": "append-v11",
                    "declared_clock_ms": REFERENCE_CLOCK_MS + 10_000,
                    "generation": 5,
                    "absolute_effective_ms": REFERENCE_CLOCK_MS + 10_000,
                }
            },
        },
    )
    write_json(
        pkg_dir / "rollback-v9.json",
        {
            "package_type": "tierflow-policy-update",
            "update_mode": "full",
            "manifest_version": 9,
            "generation": 3,
            "generation_floor": 1,
            "package_version": "rollback-v9",
            "entries": {},
        },
    )


def main() -> None:
    if SAMPLES.exists():
        for child in SAMPLES.iterdir():
            if child.is_dir() and child.name == "blobs":
                shutil.rmtree(child)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    manifest = generate_manifest()
    generate_config()
    generate_events(manifest)
    generate_v1_policy()
    generate_v2_policy()
    generate_update_packages()
    print(
        canonical_json(
            {
                "ok": True,
                "samples_dir": "samples",
                "object_count": len(manifest["objects"]),
                "reference_clock_ms": REFERENCE_CLOCK_MS,
                "seed": SEED,
            }
        )
    )


if __name__ == "__main__":
    main()
