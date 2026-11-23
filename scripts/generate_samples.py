#!/usr/bin/env python3
"""Generate deterministic v1/v2 sample files and the v2 SQLite metadata."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eventbus.util import canonical_json, digest_value, payload_digest  # noqa: E402

EPOCH_MS = 1_700_000_000_000
OUT = ROOT / "samples"


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def v1_document() -> dict[str, Any]:
    orders_events = [
        {
            "sequence": 1,
            "relative_seconds": 0,
            "partition_key": "order-1001",
            "logical_clock": 100,
            "fields": ["O-1001", "east", 10.5, "seed-v1"],
        },
        {
            "sequence": 2,
            "relative_seconds": 1,
            "partition_key": "order-1002",
            "logical_clock": 110,
            "fields": ["O-1002", "west", 7.0, "below-filter"],
        },
        {
            "sequence": 3,
            "relative_seconds": 2,
            "partition_key": "order-1003",
            "logical_clock": 120,
            "fields": ["O-1003", "east", 23.25, "v1-only"],
        },
        {
            "sequence": 4,
            "relative_seconds": 3,
            "partition_key": "order-1004",
            "logical_clock": 125,
            "fields": ["O-1004", "north", 15.0, "v2-clock-wins"],
        },
        {
            "sequence": 5,
            "relative_seconds": 4,
            "partition_key": "order-1005",
            "logical_clock": 150,
            "fields": ["O-1005", "east", 30.0, "v1-clock-wins"],
        },
        {
            "sequence": 6,
            "relative_seconds": 5,
            "partition_key": "order-1006",
            "logical_clock": 160,
            "fields": ["O-1006", "south", 99.0, "v1-only-filtered"],
        },
    ]
    return {
        "format": "eventbus-v1",
        "epoch_ms": EPOCH_MS,
        "retention_bytes": 1000,
        "clock_tolerance_ms": 500,
        "channels": [
            {
                "name": "orders-legacy",
                "canonical_name": "orders",
                "generation": 1,
                "partitions": 3,
                "event_fields": ["order_id", "region", "amount", "note"],
                "events": orders_events,
                "subscriptions": [
                    {"name": "orders-reader", "start_offset": 0}
                ],
            },
            {
                "name": "audit-legacy",
                "canonical_name": "audit",
                "generation": 1,
                "partitions": 2,
                "event_fields": ["order_id", "amount"],
                "events": [
                    {
                        "sequence": 1,
                        "relative_seconds": 0,
                        "partition_key": "legacy-audit",
                        "logical_clock": 50,
                        "fields": ["O-LEGACY", 5.0],
                    }
                ],
                "subscriptions": [
                    {"name": "audit-reader", "start_offset": 0}
                ],
            },
        ],
        "bridges": [
            {
                "id": "orders-to-audit",
                "source": "orders-legacy",
                "target": "audit-legacy",
                "source_generation": 1,
                "target_generation": 1,
                "min_upstream_watermark": 1,
                "filter": {},
                "transforms": [],
                "max_batch": 2,
            }
        ],
    }


def v2_channel_schema() -> dict[str, list[dict[str, Any]]]:
    return {
        "orders": [
            {"name": "order_id", "type": "string", "default": None},
            {"name": "region", "type": "string", "default": "unknown"},
            {"name": "amount", "type": "number", "default": 0},
            {"name": "note", "type": "string", "default": ""},
            {"name": "currency", "type": "string", "default": "CNY"},
            {"name": "version_tag", "type": "string", "default": "v2-only"},
        ],
        "audit": [
            {"name": "order_id", "type": "string", "default": None},
            {"name": "region", "type": "string", "default": "unknown"},
            {"name": "amount", "type": "number", "default": 0},
            {"name": "memo", "type": "string", "default": ""},
            {"name": "source", "type": "string", "default": "seed"},
            {"name": "currency", "type": "string", "default": "CNY"},
            {"name": "audit_tag", "type": "string", "default": "legacy"},
        ],
        "metrics": [
            {"name": "metric", "type": "string", "default": None},
            {"name": "value", "type": "number", "default": 0},
        ],
    }


def events() -> dict[str, list[dict[str, Any]]]:
    return {
        "orders": [
            {
                "sequence": 1,
                "partition_key": "order-1001",
                "logical_clock": 100,
                "produced_at_ms": EPOCH_MS,
                "fields": {
                    "order_id": "O-1001",
                    "region": "east",
                    "amount": 10.5,
                    "note": "v2-tie-wins",
                    "currency": "CNY",
                    "version_tag": "v2",
                },
                "tombstone": False,
            },
            {
                "sequence": 2,
                "partition_key": "order-1002",
                "logical_clock": 115,
                "produced_at_ms": EPOCH_MS + 1000,
                "fields": {
                    "order_id": "O-1002",
                    "region": "west",
                    "amount": 7.0,
                    "note": "below-filter",
                    "currency": "CNY",
                    "version_tag": "v2",
                },
                "tombstone": False,
            },
            {
                "sequence": 4,
                "partition_key": "order-1004",
                "logical_clock": 130,
                "produced_at_ms": EPOCH_MS + 3000,
                "fields": {
                    "order_id": "O-1004",
                    "region": "north",
                    "amount": 15.0,
                    "note": "v2-newer",
                    "currency": "CNY",
                    "version_tag": "v2",
                },
                "tombstone": False,
            },
            {
                "sequence": 5,
                "partition_key": "order-1005",
                "logical_clock": 145,
                "produced_at_ms": EPOCH_MS + 4000,
                "fields": {
                    "order_id": "O-1005",
                    "region": "east",
                    "amount": 30.0,
                    "note": "v1-clock-wins",
                    "currency": "CNY",
                    "version_tag": "v2-loses",
                },
                "tombstone": False,
            },
            {
                "sequence": 7,
                "partition_key": "order-1007",
                "logical_clock": 170,
                "produced_at_ms": EPOCH_MS + 6000,
                "fields": {
                    "order_id": "O-1007",
                    "region": "south",
                    "amount": 0,
                    "note": "",
                    "currency": "CNY",
                    "version_tag": "tombstone",
                },
                "tombstone": True,
            },
        ],
        "audit": [
            {
                "sequence": 1,
                "partition_key": "legacy-audit",
                "logical_clock": 60,
                "produced_at_ms": EPOCH_MS,
                "fields": {
                    "order_id": "O-LEGACY",
                    "region": "west",
                    "amount": 5.0,
                    "memo": "upgraded",
                    "source": "seed",
                    "currency": "CNY",
                    "audit_tag": "legacy",
                },
                "tombstone": False,
            }
        ],
        "metrics": [],
    }


def make_shards() -> list[tuple[str, str, dict[str, Any]]]:
    all_events = events()
    shard_specs = [
        ("orders-000", "orders", 2, [0, 1, 2]),
        ("orders-001", "orders", 2, [3, 4]),
        ("audit-000", "audit", 2, [0]),
        ("metrics-000", "metrics", 1, []),
    ]
    shards = []
    for shard_name, channel, generation, indexes in shard_specs:
        shard_events = []
        for index in indexes:
            item = dict(all_events[channel][index])
            item["field_checksum"] = payload_digest(item["fields"])
            shard_events.append(item)
        shard = {
            "format": "eventbus-v2-shard",
            "shard": shard_name,
            "channel": channel,
            "generation": generation,
            "events": shard_events,
        }
        shard["checksum"] = digest_value(shard)
        write_json(OUT / "v2" / "shards" / f"{shard_name}.json", shard)
        shards.append((shard_name, channel, shard))
    return shards


def bridge_definition() -> dict[str, Any]:
    return {
        "id": "orders-to-audit",
        "source_channel": "orders",
        "target_channel": "audit",
        "source_generation": 2,
        "target_generation": 2,
        "min_upstream_watermark": 2,
        "filter": {
            "all": [
                {"field": "amount", "op": "gte", "value": 10},
                {
                    "field": "region",
                    "op": "in",
                    "value": ["east", "north"],
                },
            ]
        },
        "transforms": [
            {"op": "rename", "from": "note", "to": "memo"},
            {"op": "multiply", "field": "amount", "value": 1.1, "scale": 2},
            {"op": "set", "field": "source", "value": "bridge"},
            {"op": "set", "field": "audit_tag", "value": "orders-v2"},
        ],
        "max_batch": 2,
    }


def make_metadata(shards: list[tuple[str, str, dict[str, Any]]]) -> None:
    path = OUT / "v2" / "metadata.sqlite"
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE channels(
                canonical_name TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                partitions INTEGER NOT NULL,
                field_schema_json TEXT NOT NULL,
                v1_name TEXT
            );
            CREATE TABLE event_index(
                channel TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                shard TEXT NOT NULL,
                shard_sha256 TEXT NOT NULL,
                field_checksum TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                produced_at_ms INTEGER NOT NULL,
                logical_clock INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                PRIMARY KEY(channel, sequence)
            );
            CREATE TABLE bridges(
                id TEXT PRIMARY KEY,
                source_channel TEXT NOT NULL,
                target_channel TEXT NOT NULL,
                source_generation INTEGER NOT NULL,
                target_generation INTEGER NOT NULL,
                min_upstream_watermark INTEGER NOT NULL,
                filter_json TEXT NOT NULL,
                transforms_json TEXT NOT NULL,
                max_batch INTEGER NOT NULL
            );
            CREATE TABLE subscriptions(
                name TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                start_offset INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            [
                ("format", "eventbus-v2-metadata"),
                ("retention_bytes", "1000"),
                ("clock_tolerance_ms", "500"),
            ],
        )
        schema = v2_channel_schema()
        channels = [
            ("orders", 2, 4, "orders-legacy"),
            ("audit", 2, 2, "audit-legacy"),
            ("metrics", 1, 2, None),
        ]
        connection.executemany(
            "INSERT INTO channels(canonical_name, generation, partitions, "
            "field_schema_json, v1_name) VALUES(?, ?, ?, ?, ?)",
            [
                (
                    name,
                    generation,
                    partitions,
                    canonical_json(schema[name]),
                    v1_name,
                )
                for name, generation, partitions, v1_name in channels
            ],
        )
        for shard_name, channel, shard in shards:
            for event in shard["events"]:
                connection.execute(
                    "INSERT INTO event_index(channel, sequence, generation, shard, "
                    "shard_sha256, field_checksum, fields_json, produced_at_ms, "
                    "logical_clock, tombstone) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel,
                        event["sequence"],
                        shard["generation"],
                        f"{shard_name}.json",
                        shard["checksum"],
                        event["field_checksum"],
                        canonical_json(event["fields"]),
                        event["produced_at_ms"],
                        event["logical_clock"],
                        1 if event["tombstone"] else 0,
                    ),
                )
        bridge = bridge_definition()
        connection.execute(
            "INSERT INTO bridges(id, source_channel, target_channel, source_generation, "
            "target_generation, min_upstream_watermark, filter_json, transforms_json, "
            "max_batch) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bridge["id"],
                bridge["source_channel"],
                bridge["target_channel"],
                bridge["source_generation"],
                bridge["target_generation"],
                bridge["min_upstream_watermark"],
                canonical_json(bridge["filter"]),
                canonical_json(bridge["transforms"]),
                bridge["max_batch"],
            ),
        )
        connection.executemany(
            "INSERT INTO subscriptions(name, channel, start_offset) VALUES(?, ?, ?)",
            [
                ("orders-reader", "orders", 0),
                ("audit-reader", "audit", 0),
                ("slow-audit-reader", "audit", 1),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def extra_files() -> None:
    write_json(
        OUT / "patches" / "toggle-audit-tag.json",
        {
            "patch_id": "toggle-audit-tag",
            "bridges": [
                {
                    "op": "update",
                    "bridge": {
                        **bridge_definition(),
                        "transforms": [
                            {"op": "rename", "from": "note", "to": "memo"},
                            {
                                "op": "multiply",
                                "field": "amount",
                                "value": 1.1,
                                "scale": 2,
                            },
                            {"op": "set", "field": "source", "value": "bridge"},
                            {
                                "op": "set",
                                "field": "audit_tag",
                                "value": "orders-v2-revised",
                            },
                        ],
                    },
                }
            ],
            "subscriptions": [
                {
                    "op": "add",
                    "subscription": {
                        "name": "orders-tail-reader",
                        "channel": "orders",
                        "start_offset": 2,
                    },
                }
            ],
        },
    )
    write_json(
        OUT / "append-events.json",
        {
            "events": [
                {
                    "sequence": 8,
                    "partition_key": "order-1008",
                    "logical_clock": 180,
                    "produced_at_ms": EPOCH_MS + 7000,
                    "fields": {
                        "order_id": "O-1008",
                        "region": "east",
                        "amount": 42.0,
                        "note": "runtime-append",
                        "currency": "CNY",
                        "version_tag": "append",
                    },
                    "tombstone": False,
                }
            ]
        },
    )
    write_json(
        OUT / "invalid" / "field_count.json",
        {
            "format": "eventbus-v1",
            "epoch_ms": EPOCH_MS,
            "channels": [
                {
                    "name": "bad",
                    "partitions": 1,
                    "event_fields": ["a", "b"],
                    "events": [
                        {
                            "sequence": 1,
                            "relative_seconds": 0,
                            "fields": ["only-one"],
                        }
                    ],
                }
            ],
            "bridges": [],
        },
    )
    write_json(
        OUT / "invalid" / "bridge_cycle.json",
        {
            "format": "eventbus-v1",
            "epoch_ms": EPOCH_MS,
            "channels": [
                {
                    "name": "a",
                    "partitions": 1,
                    "event_fields": ["x"],
                    "events": [],
                },
                {
                    "name": "b",
                    "partitions": 1,
                    "event_fields": ["x"],
                    "events": [],
                },
            ],
            "bridges": [
                {"id": "a-b", "source": "a", "target": "b"},
                {"id": "b-a", "source": "b", "target": "a"},
            ],
        },
    )


def main() -> None:
    write_json(OUT / "v1" / "events.json", v1_document())
    shards = make_shards()
    make_metadata(shards)
    extra_files()
    print("generated samples/v1/events.json")
    print("generated samples/v2/shards/*.json")
    print("generated samples/v2/metadata.sqlite")
    print("generated samples/patches and samples/append-events.json")


if __name__ == "__main__":
    main()
