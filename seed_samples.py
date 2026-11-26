#!/usr/bin/env python3
"""Deterministic sample generator for the delivery-semantics workbench."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


FIXED_SEED = 20261127


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def digest(seed: int) -> str:
    return "sha256:" + hashlib.sha256(f"fixed-seed-{FIXED_SEED}-event-{seed}".encode("utf-8")).hexdigest()


def dep(topic: str, partition: int, offset: int) -> Dict[str, Any]:
    return {
        "upstream_topic": topic,
        "upstream_partition": partition,
        "upstream_offset": offset,
        "required_watermark": offset + 1,
    }


def semantic_entry(
    key: str,
    clock: int,
    effective_ms: int,
    generation: int,
    *,
    revision: int = 1,
    dependencies: List[Dict[str, Any]] | None = None,
    tombstone: bool = False,
    reason: str | None = None,
) -> Dict[str, Any]:
    return {
        "message_key": key,
        "effective_at_ms": effective_ms,
        "logical_clock_ms": clock,
        "semantic_generation": generation,
        "revision": revision,
        "upstream_watermark_dependencies": dependencies or [],
        "tombstone": tombstone,
        "tombstone_reason": reason,
    }


def build_topics() -> List[Dict[str, Any]]:
    return [
        {"name": "orders", "partitions": 3, "key_rule": "suffix_mod:3", "backlog_limit": 1},
        {"name": "payments", "partitions": 2, "key_rule": "suffix_mod:2", "backlog_limit": 1},
        {"name": "inventory", "partitions": 2, "key_rule": "suffix_mod:2", "backlog_limit": 1},
    ]


def build_events() -> List[Dict[str, Any]]:
    raw = [
        ("orders", "order-3000", 3),
        ("orders", "order-3001", 3),
        ("orders", "order-3002", 3),
        ("orders", "order-3003", 3),
        ("orders", "order-3004", 3),
        ("orders", "order-3005", 3),
        ("payments", "pay-4000", 3),
        ("payments", "pay-4001", 3),
        ("payments", "pay-4002", 3),
        ("payments", "pay-4003", 4),
        ("inventory", "sku-5000", 3),
        ("inventory", "sku-5001", 2),
        ("inventory", "sku-5002", 3),
        ("inventory", "sku-5003", 3),
    ]
    events = []
    for seq, (topic, key, expected_generation) in enumerate(raw, 1):
        events.append(
            {
                "seq": seq,
                "topic": topic,
                "key": key,
                "payload_summary": f"deterministic payload summary for {key}",
                "payload_digest": digest(seq),
                "logical_clock_ms": 1000 + seq * 100,
                "expected_generation": expected_generation,
                "revision": 1,
            }
        )
    return events


def build_subscriptions() -> List[Dict[str, Any]]:
    return [
        {
            "subscriber_id": "sub-orders-a",
            "topic": "orders",
            "callback_url": "http://127.0.0.1:18081/hooks/orders-a",
            "pull_batch_size": 2,
            "rate_limit_per_sec": 20,
            "priority": 30,
        },
        {
            "subscriber_id": "sub-payments",
            "topic": "payments",
            "callback_url": "http://127.0.0.1:18083/hooks/payments",
            "pull_batch_size": 2,
            "rate_limit_per_sec": 20,
            "priority": 20,
        },
        {
            "subscriber_id": "sub-inventory",
            "topic": "inventory",
            "callback_url": "http://127.0.0.1:18084/hooks/inventory",
            "pull_batch_size": 2,
            "rate_limit_per_sec": 20,
            "priority": 15,
        },
        {
            "subscriber_id": "sub-orders-b",
            "topic": "orders",
            "callback_url": "http://127.0.0.1:18082/hooks/orders-b",
            "pull_batch_size": 1,
            "rate_limit_per_sec": 10,
            "priority": 10,
        },
    ]


def build_v1() -> Dict[str, Any]:
    entries = [
        {
            "message_key": "orders/1/0",
            "effective_rel_sec": 1,
            "logical_clock_ms": 1200,
            "semantic_generation": 3,
            "revision": 1,
        },
        {
            "message_key": "orders/0/0",
            "effective_rel_sec": 0,
            "logical_clock_ms": 1100,
            "semantic_generation": 3,
            "revision": 1,
        },
        {
            "message_key": "payments/0/0",
            "effective_rel_sec": 0,
            "logical_clock_ms": 1700,
            "semantic_generation": 3,
            "revision": 1,
        },
        {
            "message_key": "inventory/1/0",
            "effective_rel_sec": 2,
            "logical_clock_ms": 2200,
            "semantic_generation": 2,
            "revision": 1,
        },
    ]
    dependencies = {
        "orders/0/0": [
            "inventory/0/0",
            "payments/0/0",
        ],
        "orders/1/0": ["payments/1/0"],
    }
    return {
        "format": "v1",
        "snapshot_id": "semantics-v1-fixed",
        "snapshot_version": 1,
        "seed": FIXED_SEED,
        "declared_clock_ms": 1000,
        "time_semantics": "effective_rel_sec is relative to the injected merge clock",
        "entries": entries,
        "delivery_dependencies": dependencies,
    }


def build_v2_shards() -> List[Dict[str, Any]]:
    orders_entries = [
        semantic_entry("orders/0/0", 1100, 2100, 3, dependencies=[dep("inventory", 0, 0), dep("payments", 0, 0)]),
        semantic_entry("orders/1/0", 1200, 2200, 3, dependencies=[dep("payments", 1, 0)]),
        semantic_entry("orders/2/0", 1300, 2300, 3, dependencies=[dep("inventory", 1, 0)]),
        semantic_entry("orders/0/1", 1400, 2400, 3, dependencies=[dep("payments", 0, 1)]),
        semantic_entry(
            "orders/1/1",
            1500,
            2500,
            3,
            dependencies=[dep("payments", 1, 1)],
            tombstone=True,
            reason="duplicate_order_canceled",
        ),
        semantic_entry("orders/2/1", 1600, 2600, 3, dependencies=[dep("inventory", 1, 1)]),
    ]
    payment_inventory_entries = [
        semantic_entry("payments/0/0", 1600, 2700, 3),
        semantic_entry("payments/1/0", 1800, 2800, 3),
        semantic_entry("payments/0/1", 1900, 2900, 3),
        semantic_entry("payments/1/1", 2000, 3000, 4),
        semantic_entry("inventory/0/0", 2100, 3100, 3),
        semantic_entry("inventory/1/0", 2100, 3200, 3),
        semantic_entry("inventory/0/1", 2300, 3300, 3),
    ]
    return [
        {"shard_id": "shard-000-orders", "ordinal": 0, "semantic_generation": 3, "entries": orders_entries},
        {"shard_id": "shard-001-payment-inventory", "ordinal": 1, "semantic_generation": 4, "entries": payment_inventory_entries},
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def generate_v2(root: Path) -> None:
    semantics = root / "semantics"
    shard_dir = semantics / "v2"
    shards = build_v2_shards()
    shard_refs = []
    previous_hash = "GENESIS"
    for shard in shards:
        relative_path = f"v2/shard-{shard['ordinal']:03d}.json"
        path = semantics / relative_path
        write_json(path, {"shard_id": shard["shard_id"], "ordinal": shard["ordinal"], "semantic_generation": shard["semantic_generation"], "entries": shard["entries"]})
        file_hash = sha256_file(path)
        shard_refs.append(
            {
                "shard_id": shard["shard_id"],
                "ordinal": shard["ordinal"],
                "path": relative_path,
                "sha256": file_hash,
                "prev_sha256": previous_hash,
                "semantic_generation": shard["semantic_generation"],
            }
        )
        previous_hash = file_hash
    manifest = {
        "format": "v2",
        "snapshot_id": "semantics-v2-fixed",
        "snapshot_version": 1,
        "seed": FIXED_SEED,
        "generation_min": 3,
        "declared_clock_ms": 1600,
        "metadata_db": "v2_metadata.sqlite",
        "shards": shard_refs,
    }
    write_json(semantics / "v2_manifest.json", manifest)
    db_path = semantics / "v2_metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE snapshot_meta (
                snapshot_version INTEGER NOT NULL,
                generation_min INTEGER NOT NULL,
                declared_clock_ms INTEGER NOT NULL,
                seed INTEGER NOT NULL
            );
            CREATE TABLE shards (
                ordinal INTEGER PRIMARY KEY,
                shard_id TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                prev_sha256 TEXT NOT NULL,
                semantic_generation INTEGER NOT NULL
            );
            CREATE TABLE entry_metadata (
                message_key TEXT PRIMARY KEY,
                shard_ordinal INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                semantic_generation INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                tombstone INTEGER NOT NULL
            );
            CREATE TABLE upstream_dependencies (
                message_key TEXT NOT NULL,
                dep_ord INTEGER NOT NULL,
                upstream_topic TEXT NOT NULL,
                upstream_partition INTEGER NOT NULL,
                upstream_offset INTEGER NOT NULL,
                required_watermark INTEGER NOT NULL,
                PRIMARY KEY(message_key, dep_ord)
            );
            CREATE TABLE tombstones (
                message_key TEXT PRIMARY KEY,
                semantic_generation INTEGER NOT NULL,
                reason TEXT NOT NULL,
                effective_at_ms INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO snapshot_meta VALUES(?, ?, ?, ?)",
            (manifest["snapshot_version"], manifest["generation_min"], manifest["declared_clock_ms"], FIXED_SEED),
        )
        for ref in shard_refs:
            conn.execute(
                "INSERT INTO shards VALUES(?, ?, ?, ?, ?, ?)",
                (ref["ordinal"], ref["shard_id"], ref["path"], ref["sha256"], ref["prev_sha256"], ref["semantic_generation"]),
            )
        for shard in shards:
            for entry in shard["entries"]:
                conn.execute(
                    "INSERT INTO entry_metadata VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["message_key"],
                        shard["ordinal"],
                        entry["effective_at_ms"],
                        entry["logical_clock_ms"],
                        entry["semantic_generation"],
                        entry["revision"],
                        1 if entry["tombstone"] else 0,
                    ),
                )
                for dep_ord, dependency in enumerate(entry["upstream_watermark_dependencies"]):
                    conn.execute(
                        "INSERT INTO upstream_dependencies VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            entry["message_key"],
                            dep_ord,
                            dependency["upstream_topic"],
                            dependency["upstream_partition"],
                            dependency["upstream_offset"],
                            dependency["required_watermark"],
                        ),
                    )
                if entry["tombstone"]:
                    conn.execute(
                        "INSERT INTO tombstones VALUES(?, ?, ?, ?)",
                        (entry["message_key"], entry["semantic_generation"], entry["tombstone_reason"], entry["effective_at_ms"]),
                    )
        conn.commit()
    finally:
        conn.close()


def write_package(package_dir: Path, package: Dict[str, Any], entries: List[Dict[str, Any]]) -> None:
    shard_path = package_dir / "shards" / "patch-000.json"
    write_json(shard_path, {"shard_id": package["shard_id"], "ordinal": 0, "semantic_generation": package["shard_generation"], "entries": entries})
    file_hash = sha256_file(shard_path)
    manifest = {
        "format": "v2-semantic-package",
        "package_id": package["package_id"],
        "snapshot_version": package["snapshot_version"],
        "generation_min": package["generation_min"],
        "base_snapshot_version": package.get("base_snapshot_version"),
        "mode": package.get("mode", "incremental"),
        "declared_clock_ms": package["declared_clock_ms"],
        "shards": [
            {
                "shard_id": package["shard_id"],
                "ordinal": 0,
                "path": "shards/patch-000.json",
                "sha256": file_hash,
                "prev_sha256": "GENESIS",
                "semantic_generation": package["shard_generation"],
            }
        ],
    }
    write_json(package_dir / "package.json", manifest)


def generate_packages(root: Path) -> None:
    packages = root / "packages"
    write_package(
        packages / "pkg-002-invalidation",
        {
            "package_id": "pkg-002-invalidation",
            "snapshot_version": 2,
            "generation_min": 3,
            "base_snapshot_version": 1,
            "mode": "incremental",
            "declared_clock_ms": 3000,
            "shard_id": "shard-pkg-002",
            "shard_generation": 4,
        },
        [
            semantic_entry(
                "payments/0/0",
                3100,
                3100,
                4,
                revision=2,
                tombstone=True,
                reason="refund_rollback_detected",
            ),
            semantic_entry("payments/1/1", 3200, 3200, 3, revision=1),
            semantic_entry("inventory/1/1", 3300, 3300, 3, revision=1),
        ],
    )
    write_package(
        packages / "pkg-009-rollback",
        {
            "package_id": "pkg-009-rollback",
            "snapshot_version": 1,
            "generation_min": 3,
            "base_snapshot_version": 2,
            "mode": "incremental",
            "declared_clock_ms": 3400,
            "shard_id": "shard-pkg-009",
            "shard_generation": 3,
        },
        [semantic_entry("inventory/0/0", 3400, 3400, 3, revision=1)],
    )

    full_entries: Dict[str, Dict[str, Any]] = {}
    for shard in build_v2_shards():
        for entry in shard["entries"]:
            full_entries[entry["message_key"]] = dict(entry)
    full_entries["inventory/1/1"] = semantic_entry("inventory/1/1", 3300, 3300, 3, revision=1)
    full_entries["payments/0/0"] = semantic_entry(
        "payments/0/0",
        3100,
        3100,
        4,
        revision=2,
        dependencies=[],
        tombstone=True,
        reason="refund_rollback_detected",
    )
    full_entries["payments/1/1"] = semantic_entry("payments/1/1", 3200, 3200, 3, revision=1)
    write_package(
        packages / "pkg-003-full-replacement",
        {
            "package_id": "pkg-003-full-replacement",
            "snapshot_version": 3,
            "generation_min": 3,
            "base_snapshot_version": None,
            "mode": "full",
            "declared_clock_ms": 3500,
            "shard_id": "shard-pkg-003-full",
            "shard_generation": 4,
        },
        [full_entries[key] for key in sorted(full_entries)],
    )


def generate(target: str | Path = "samples") -> Path:
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "topics.json", {"seed": FIXED_SEED, "topics": build_topics()})
    events = build_events()
    with (root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    write_json(root / "subscriptions.json", {"seed": FIXED_SEED, "subscriptions": build_subscriptions()})
    write_json(root / "semantics" / "v1_snapshot.json", build_v1())
    generate_v2(root)
    generate_packages(root)
    return root


def main() -> int:
    root = generate("samples")
    print(json.dumps({"ok": True, "code": "WB_OK", "generated": str(root), "seed": FIXED_SEED}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
