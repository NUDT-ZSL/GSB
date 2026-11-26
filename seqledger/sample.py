"""Fixed-seed deterministic fixture generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import sha256_bytes, write_json_atomic

SEED = 20261127


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _route(topic: str, partition_count: int, partition_key: str) -> int:
    digest = hashlib.sha256(
        f"{topic}\x1f{partition_key}".encode("utf-8")
    ).hexdigest()
    return int(digest, 16) % partition_count


def generate_fixtures(output_dir: str | Path) -> dict[str, Any]:
    """Generate the same files for every run.

    The seed is recorded for provenance. All generated identifiers, timestamps,
    digests and routes are derived deterministically from the fixed seed and the
    explicit dataset below; no random or wall-clock source is used.
    """

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    v2_dir = root / "semantics_v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    for stale in [
        root / "topology.json",
        root / "events.jsonl",
        root / "subscriptions.json",
        root / "semantics_v1.json",
        root / "patch-001.json",
        root / "fixture_manifest.json",
        v2_dir / "manifest.json",
        v2_dir / "shard-000.json",
        v2_dir / "shard-001.json",
        v2_dir / "meta.sqlite3",
    ]:
        if stale.exists():
            stale.unlink()

    topology = {
        "schema": "seqledger-topology/v1",
        "topics": [
            {
                "name": "orders",
                "partition_count": 4,
                "partition_key_rule": {
                    "type": "regex",
                    "pattern": "^ord-[0-9]{4}$",
                    "canonical_form": "^ord-[0-9]{4}$",
                    "routing": "sha256-mod",
                },
            },
            {
                "name": "payments",
                "partition_count": 3,
                "partition_key_rule": {
                    "type": "regex",
                    "pattern": "^pay-[A-Z]{2}-[0-9]{3}$",
                    "canonical_form": "^pay-[A-Z]{2}-[0-9]{3}$",
                    "routing": "sha256-mod",
                },
            },
        ],
    }
    write_json_atomic(root / "topology.json", topology)

    order_keys = [f"ord-{1000 + index}" for index in range(1, 11)]
    payment_keys = [f"pay-AB-{index:03d}" for index in range(1, 9)]
    all_keys = order_keys + payment_keys

    winner_generation = {
        "ord-1001": 2,
        "ord-1002": 2,
        "ord-1003": 2,
        "ord-1004": 3,
        "ord-1005": 2,
        "ord-1006": 3,
        "ord-1007": 2,
        "ord-1008": 3,
        "ord-1009": 2,
        "ord-1010": 3,
        "pay-AB-001": 2,
        "pay-AB-002": 2,
        "pay-AB-003": 2,
        "pay-AB-004": 3,
        "pay-AB-005": 2,
        "pay-AB-006": 3,
        "pay-AB-007": 2,
        "pay-AB-008": 3,
    }

    event_keys = [
        "ord-1001",
        "pay-AB-001",
        "ord-1002",
        "pay-AB-002",
        "ord-1003",
        "pay-AB-003",
        "ord-1004",
        "pay-AB-004",
        "ord-1005",
        "pay-AB-005",
        "ord-1006",
        "pay-AB-006",
        "ord-1007",
        "pay-AB-007",
        "ord-1008",
        "pay-AB-008",
        "ord-1009",
        "ord-1010",
    ]
    events: list[dict[str, Any]] = []
    for index, partition_key in enumerate(event_keys):
        topic = "orders" if partition_key.startswith("ord-") else "payments"
        logical_ts_ms = 120 + index * 170
        events.append(
            {
                "event_seq": 1001 + index,
                "topic": topic,
                "partition_key": partition_key,
                "payload_digest": _digest(f"{SEED}:{partition_key}:{index}"),
                "logical_ts_ms": logical_ts_ms,
                "expected_delivery_generation": winner_generation[partition_key],
            }
        )
    events_path = root / "events.jsonl"
    events_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

    subscriptions = {
        "schema": "seqledger-subscriptions/v1",
        "subscriptions": [
            {
                "subscriber_id": "orders-local-sink",
                "topic": "orders",
                "batch_size": 5,
                "callback_endpoint": "http://127.0.0.1:18081/consume",
                "max_rate_per_sec": 4,
            },
            {
                "subscriber_id": "payments-local-sink",
                "topic": "payments",
                "batch_size": 3,
                "callback_endpoint": "http://127.0.0.1:18082/consume",
                "max_rate_per_sec": 3,
            },
        ],
    }
    write_json_atomic(root / "subscriptions.json", subscriptions)

    event_by_key = {event["partition_key"]: event for event in events}
    v1_keys = [
        "ord-1001",
        "ord-1002",
        "ord-1003",
        "ord-1004",
        "pay-AB-001",
        "pay-AB-002",
        "pay-AB-003",
    ]
    v1_relative = {
        "ord-1001": 0,
        "ord-1002": 1,
        "ord-1003": 2,
        "ord-1004": 1,
        "pay-AB-001": 0,
        "pay-AB-002": 2,
        "pay-AB-003": 3,
    }
    v1_generation = {
        "ord-1001": 2,
        "ord-1002": 1,
        "ord-1003": 2,
        "ord-1004": 1,
        "pay-AB-001": 2,
        "pay-AB-002": 1,
        "pay-AB-003": 2,
    }
    semantics_v1 = {
        "schema": "seqledger-semantics-v1/v1",
        "logical_clock_ms": 1200,
        "ms_per_second": 1000,
        "rounding": "floor",
        "entries": [
            {
                "message_key": key,
                "topic": event_by_key[key]["topic"],
                "delivery_alias": "v1-at-least-once",
                "relative_effective_seconds": v1_relative[key],
                "semantic_generation": v1_generation[key],
                "payload_digest": event_by_key[key]["payload_digest"],
            }
            for key in v1_keys
        ],
    }
    write_json_atomic(root / "semantics_v1.json", semantics_v1)

    shard0_keys = [
        "ord-1002",
        "ord-1005",
        "ord-1007",
        "ord-1009",
        "pay-AB-002",
        "pay-AB-005",
        "pay-AB-007",
    ]
    shard1_keys = [
        "ord-1004",
        "ord-1006",
        "ord-1008",
        "ord-1010",
        "pay-AB-004",
        "pay-AB-006",
        "pay-AB-008",
    ]
    absolute_ms = {
        key: 1300 + index * 90
        for index, key in enumerate(shard0_keys + shard1_keys)
    }

    shard0_entries: list[dict[str, Any]] = []
    for key in shard0_keys:
        event = event_by_key[key]
        entry: dict[str, Any] = {
            "message_key": key,
            "topic": event["topic"],
            "delivery_alias": "v2-cardinality-safe",
            "absolute_effective_ms": absolute_ms[key],
            "semantic_generation": 2,
            "payload_digest": event["payload_digest"],
            "tombstone_ref": "tomb-orders-1005" if key == "ord-1005" else None,
        }
        shard0_entries.append(entry)
    shard1_entries: list[dict[str, Any]] = []
    for key in shard1_keys:
        event = event_by_key[key]
        shard1_entries.append(
            {
                "message_key": key,
                "topic": event["topic"],
                "delivery_alias": "v2-cardinality-safe",
                "absolute_effective_ms": absolute_ms[key],
                "semantic_generation": 3,
                "payload_digest": event["payload_digest"],
                "tombstone_ref": None,
            }
        )

    shard0 = {
        "schema": "seqledger-semantics-v2-shard/v1",
        "shard_id": "shard-000",
        "semantic_generation": 2,
        "logical_clock_ms": 1800,
        "entries": shard0_entries,
    }
    shard1 = {
        "schema": "seqledger-semantics-v2-shard/v1",
        "shard_id": "shard-001",
        "semantic_generation": 3,
        "logical_clock_ms": 2300,
        "entries": shard1_entries,
    }
    write_json_atomic(v2_dir / "shard-000.json", shard0)
    write_json_atomic(v2_dir / "shard-001.json", shard1)
    shard0_hash = sha256_bytes((v2_dir / "shard-000.json").read_bytes())
    shard1_hash = sha256_bytes((v2_dir / "shard-001.json").read_bytes())
    manifest = {
        "schema": "seqledger-semantics-v2-manifest/v1",
        "shards": [
            {
                "shard_id": "shard-000",
                "path": "shard-000.json",
                "sha256": shard0_hash,
                "semantic_generation": 2,
                "logical_clock_ms": 1800,
            },
            {
                "shard_id": "shard-001",
                "path": "shard-001.json",
                "sha256": shard1_hash,
                "semantic_generation": 3,
                "logical_clock_ms": 2300,
            },
        ],
    }
    write_json_atomic(v2_dir / "manifest.json", manifest)

    meta_path = v2_dir / "meta.sqlite3"
    connection = sqlite3.connect(str(meta_path))
    try:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE semantics (
                message_key TEXT PRIMARY KEY,
                topic TEXT,
                delivery_alias TEXT NOT NULL,
                absolute_effective_ms INTEGER NOT NULL,
                semantic_generation INTEGER NOT NULL,
                shard_id TEXT NOT NULL,
                payload_digest TEXT
            );
            CREATE TABLE watermarks (
                message_key TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL,
                upstream_offset INTEGER NOT NULL
            );
            CREATE TABLE tombstones (
                tombstone_id TEXT PRIMARY KEY,
                message_key TEXT NOT NULL,
                reason TEXT NOT NULL,
                semantic_generation INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema", "seqledger-semantics-v2-meta/v1"),
                ("current_generation", "3"),
                ("seed", str(SEED)),
            ],
        )
        all_v2_entries = shard0_entries + shard1_entries
        connection.executemany(
            "INSERT INTO semantics(message_key, topic, delivery_alias, "
            "absolute_effective_ms, semantic_generation, shard_id, payload_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    entry["message_key"],
                    entry["topic"],
                    entry["delivery_alias"],
                    entry["absolute_effective_ms"],
                    entry["semantic_generation"],
                    "shard-000" if entry in shard0_entries else "shard-001",
                    entry["payload_digest"],
                )
                for entry in all_v2_entries
            ],
        )
        event_partition = {
            event["partition_key"]: _route(
                event["topic"],
                4 if event["topic"] == "orders" else 3,
                event["partition_key"],
            )
            for event in events
        }
        event_offsets: dict[str, int] = {}
        next_offsets: dict[tuple[str, int], int] = {}
        for event in events:
            pid = (event["topic"], event_partition[event["partition_key"]])
            event_offsets[event["partition_key"]] = next_offsets.get(pid, 0)
            next_offsets[pid] = next_offsets.get(pid, 0) + 1
        connection.executemany(
            "INSERT INTO watermarks(message_key, topic, partition, upstream_offset) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    "pay-AB-002",
                    "orders",
                    event_partition["ord-1001"],
                    event_offsets["ord-1001"],
                ),
                (
                    "pay-AB-004",
                    "payments",
                    event_partition["pay-AB-002"],
                    event_offsets["pay-AB-002"],
                ),
                (
                    "pay-AB-006",
                    "payments",
                    event_partition["pay-AB-004"],
                    event_offsets["pay-AB-004"],
                ),
            ],
        )
        connection.execute(
            "INSERT INTO tombstones(tombstone_id, message_key, reason, semantic_generation) "
            "VALUES (?, ?, ?, ?)",
            (
                "tomb-orders-1005",
                "ord-1005",
                "superseded-by-v2-generation-three",
                2,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    patch = {
        "schema": "seqledger-semantic-patch/v1",
        "patch_id": "patch-001",
        "semantic_generation": 4,
        "logical_clock_ms": 7000,
        "updates": [
            {
                "message_key": "ord-1004",
                "delivery_alias": "v2-exactly-once-patched",
                "absolute_effective_ms": 3000,
                "semantic_generation": 4,
            }
        ],
        "upstream_watermarks": [
            {
                "message_key": "pay-AB-002",
                "topic": "orders",
                "partition": event_partition["ord-1002"],
                "upstream_offset": event_offsets["ord-1002"],
            }
        ],
        "tombstones": [
            {
                "tombstone_id": "tomb-patch-pay-007",
                "message_key": "pay-AB-007",
                "reason": "retired-by-patch-001",
                "semantic_generation": 4,
            }
        ],
    }
    write_json_atomic(root / "patch-001.json", patch)

    files = {
        "topology.json": sha256_bytes((root / "topology.json").read_bytes()),
        "events.jsonl": sha256_bytes((root / "events.jsonl").read_bytes()),
        "subscriptions.json": sha256_bytes((root / "subscriptions.json").read_bytes()),
        "semantics_v1.json": sha256_bytes((root / "semantics_v1.json").read_bytes()),
        "semantics_v2/manifest.json": sha256_bytes(
            (v2_dir / "manifest.json").read_bytes()
        ),
        "semantics_v2/shard-000.json": shard0_hash,
        "semantics_v2/shard-001.json": shard1_hash,
        "semantics_v2/meta.sqlite3": sha256_bytes(meta_path.read_bytes()),
        "patch-001.json": sha256_bytes((root / "patch-001.json").read_bytes()),
    }
    fixture_manifest = {
        "schema": "seqledger-fixture-manifest/v1",
        "seed": SEED,
        "event_count": len(events),
        "message_key_count": len(all_keys),
        "topic_partition_count": sum(
            topic["partition_count"] for topic in topology["topics"]
        ),
        "files": files,
    }
    write_json_atomic(root / "fixture_manifest.json", fixture_manifest)
    return fixture_manifest
