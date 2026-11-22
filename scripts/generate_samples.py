#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def etag(body: bytes) -> str:
    return f'"sha256:{digest(body)}"'


def resource(
    path: str,
    body: str,
    version: int,
    max_age: int,
    s_maxage: int,
    last_modified_clock: int,
) -> dict[str, Any]:
    encoded = body.encode("utf-8")
    return {
        "path": path,
        "body": body,
        "content_version": version,
        "digest_algorithm": "sha256",
        "content_digest": digest(encoded),
        "etag": etag(encoded),
        "cache_control": {
            "max_age": max_age,
            "s_maxage": s_maxage,
        },
        "cacheable": True,
        "last_modified_clock": last_modified_clock,
    }


def v1_entry(
    node: str,
    path: str,
    version: int,
    body: str,
    age_seconds: int,
    ttl_remaining_seconds: int,
    generation: int,
) -> dict[str, Any]:
    encoded = body.encode("utf-8")
    return {
        "node": node,
        "path": path,
        "content_version": version,
        "digest_algorithm": "sha256",
        "content_digest": digest(encoded),
        "etag": etag(encoded),
        "last_verified_age_seconds": age_seconds,
        "ttl_remaining_seconds": ttl_remaining_seconds,
        "cache_generation": generation,
        "cacheable": True,
    }


def v2_entry(
    node: str,
    path: str,
    version: int,
    body: str,
    last_verified_clock: int,
    expires_at_clock: int,
    generation: int,
) -> dict[str, Any]:
    encoded = body.encode("utf-8")
    return {
        "node": node,
        "path": path,
        "content_version": version,
        "digest_algorithm": "sha256",
        "content_digest": digest(encoded),
        "etag": etag(encoded),
        "last_verified_clock": last_verified_clock,
        "expires_at_clock": expires_at_clock,
        "cache_generation": generation,
        "cacheable": True,
    }


def build() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for stale in [
        SAMPLES / "v2" / "shards" / "edge-a.json",
        SAMPLES / "v2" / "shards" / "edge-b.json",
        SAMPLES / "v2" / "shards" / "edge-c.json",
    ]:
        if stale.exists():
            stale.unlink()
    metadata_path = SAMPLES / "v2_meta.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()

    bodies = {
        "app_v1": 'console.log("app-v1");\n',
        "app_v2": 'console.log("app-v2");\n',
        "app_v3": 'console.log("app-v3");\n',
        "style_v1": "body{color:#111}\n",
        "style_v2": "body{color:#123}\n",
        "config_v4": '{"theme":"light","rev":4}\n',
        "config_v5": '{"theme":"dark","rev":5}\n',
        "logo_v1": "PNGDATA-v1",
        "hero_v1": "HERO-IMAGE-v1",
        "catalog_v1": '{"items":[1,2]}\n',
        "catalog_v2": '{"items":[1,2,3]}\n',
        "report_v2": '{"report":2}\n',
        "news_v1": '{"news":[1]}\n',
        "news_v2": '{"news":[1,2]}\n',
        "news_v3": '{"news":[3]}\n',
        "weather_v1": '{"weather":"cloudy","rev":1}\n',
        "weather_v2": '{"weather":"sunny","rev":2}\n',
    }
    resources = [
        resource("/assets/app.js", bodies["app_v3"], 3, 30, 30, 1180),
        resource("/assets/style.css", bodies["style_v2"], 2, 60, 60, 1170),
        resource("/api/config.json", bodies["config_v5"], 5, 20, 20, 1190),
        resource("/images/logo.png", bodies["logo_v1"], 1, 300, 300, 1100),
        resource("/images/hero.png", bodies["hero_v1"], 1, 300, 300, 1110),
        resource("/data/catalog.json", bodies["catalog_v2"], 2, 40, 40, 1150),
        resource("/data/report.json", bodies["report_v2"], 2, 25, 25, 1160),
        resource("/feeds/news.json", bodies["news_v3"], 3, 30, 30, 1185),
        resource("/feeds/weather.json", bodies["weather_v2"], 2, 40, 40, 1175),
    ]
    dump_json(
        SAMPLES / "origin_manifest.json",
        {"schema_version": 1, "etag_algorithm": "sha256", "resources": resources},
    )
    dump_json(
        SAMPLES / "nodes.json",
        {
            "schema_version": 1,
            "nodes": [
                {
                    "name": "edge-a",
                    "region": "cn-east",
                    "capacity": 3,
                    "validation_budget": 5,
                },
                {
                    "name": "edge-b",
                    "region": "cn-north",
                    "capacity": 3,
                    "validation_budget": 5,
                },
                {
                    "name": "edge-c",
                    "region": "cn-south",
                    "capacity": 2,
                    "validation_budget": 2,
                },
            ],
        },
    )
    dump_json(
        SAMPLES / "policy.json",
        {
            "schema_version": 1,
            "policy": {
                "logical_clock": 1200,
                "validation_cycle_ticks": 15,
                "freshness_tolerance_ticks": 2,
                "clock_rollback_tolerance_ticks": 5,
                "origin_retry_budget": 1,
                "origin_request_budget_ms": 150,
                "logical_clock_step": 10,
                "origin_seconds_per_tick": 10,
                "comparison_threshold": 2,
                "sample_fraction": 0.5,
                "sample_seed": "local-cdn-stable-seed",
                "origin_epoch_utc": "2026-01-01T00:00:00Z",
            },
        },
    )
    v1_entries = [
        v1_entry("edge-a", "/assets/app.js", 2, bodies["app_v2"], 200, -20, 1),
        v1_entry("edge-a", "/assets/style.css", 1, bodies["style_v1"], 100, 50, 1),
        v1_entry("edge-a", "/images/logo.png", 1, bodies["logo_v1"], 20, 60, 1),
        v1_entry("edge-a", "/data/catalog.json", 1, bodies["catalog_v1"], 10, 100, 1),
        v1_entry("edge-a", "/feeds/weather.json", 1, bodies["weather_v1"], 30, 50, 1),
        v1_entry("edge-b", "/assets/app.js", 1, bodies["app_v1"], 50, 100, 1),
        v1_entry("edge-b", "/api/config.json", 4, bodies["config_v4"], 80, 70, 1),
        v1_entry("edge-b", "/data/report.json", 2, bodies["report_v2"], 30, 90, 1),
        v1_entry("edge-b", "/feeds/news.json", 2, bodies["news_v2"], 60, 80, 1),
        v1_entry("edge-c", "/data/catalog.json", 1, bodies["catalog_v1"], 20, 100, 1),
        v1_entry("edge-c", "/assets/style.css", 1, bodies["style_v1"], 50, 90, 1),
        v1_entry("edge-c", "/feeds/news.json", 1, bodies["news_v1"], 10, 100, 1),
    ]
    dump_json(
        SAMPLES / "v1_snapshot.json",
        {
            "schema_version": 1,
            "format": "cache-v1",
            "snapshot_generation": 1,
            "logical_clock": 1000,
            "time_semantics": {
                "kind": "relative_seconds",
                "scale_seconds_per_tick": 10,
                "rounding": "ceil",
            },
            "entries": v1_entries,
        },
    )
    shard_entries = {
        "edge-a": [
            v2_entry("edge-a", "/assets/app.js", 2, bodies["app_v2"], 1195, 1198, 2),
            v2_entry("edge-a", "/assets/style.css", 2, bodies["style_v2"], 1190, 1230, 2),
            v2_entry("edge-a", "/images/logo.png", 1, bodies["logo_v1"], 1195, 1199, 2),
            v2_entry("edge-a", "/images/hero.png", 1, bodies["hero_v1"], 1195, 1250, 2),
            v2_entry("edge-a", "/data/catalog.json", 2, bodies["catalog_v2"], 1190, 1220, 2),
            v2_entry("edge-a", "/feeds/weather.json", 1, bodies["weather_v1"], 1185, 1190, 2),
        ],
        "edge-b": [
            v2_entry("edge-b", "/assets/app.js", 1, bodies["app_v1"], 1180, 1190, 2),
            v2_entry("edge-b", "/api/config.json", 4, bodies["config_v4"], 1180, 1195, 2),
            v2_entry("edge-b", "/data/report.json", 2, bodies["report_v2"], 1190, 1199, 2),
            v2_entry("edge-b", "/feeds/news.json", 2, bodies["news_v2"], 1180, 1190, 2),
        ],
        "edge-c": [
            v2_entry("edge-c", "/data/catalog.json", 2, bodies["catalog_v2"], 1190, 1220, 2),
            v2_entry("edge-c", "/assets/style.css", 2, bodies["style_v2"], 1195, 1225, 2),
            v2_entry("edge-c", "/feeds/news.json", 1, bodies["news_v1"], 1180, 1190, 2),
        ],
    }
    shard_records: list[dict[str, Any]] = []
    for index, node in enumerate(["edge-a", "edge-b", "edge-c"]):
        shard_file = f"v2/shards/{node}.json"
        shard_path = SAMPLES / shard_file
        payload = {
            "schema_version": 2,
            "format": "v2-shard",
            "snapshot_generation": 2,
            "logical_clock": 1200,
            "shard_index": index,
            "node": node,
            "entries": shard_entries[node],
        }
        dump_json(shard_path, payload)
        shard_bytes = shard_path.read_bytes()
        shard_records.append(
            {
                "shard_file": shard_file,
                "sha256": hashlib.sha256(shard_bytes).hexdigest(),
                "entry_count": len(shard_entries[node]),
                "snapshot_generation": 2,
                "shard_index": index,
            }
        )
    connection = sqlite3.connect(metadata_path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE shards (
                shard_file TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                entry_count INTEGER NOT NULL,
                snapshot_generation INTEGER NOT NULL,
                shard_index INTEGER NOT NULL UNIQUE
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("format", "v2-sqlite-metadata"),
                ("snapshot_generation", "2"),
                ("logical_clock", "1200"),
                ("shard_count", "3"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO shards(
                shard_file, sha256, entry_count, snapshot_generation, shard_index
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    record["shard_file"],
                    record["sha256"],
                    record["entry_count"],
                    record["snapshot_generation"],
                    record["shard_index"],
                )
                for record in shard_records
            ],
        )
        connection.commit()
    finally:
        connection.close()
    dump_json(
        SAMPLES / "injections.json",
        {
            "schema_version": 1,
            "injections": [
                {
                    "seq": 1,
                    "stage": "parse",
                    "type": "content_tamper_same_version",
                    "path": "/images/logo.png",
                    "node": "edge-a",
                },
                {
                    "seq": 2,
                    "stage": "merge",
                    "type": "etag_conflict",
                    "path": "/data/catalog.json",
                    "node": "edge-c",
                },
                {
                    "seq": 3,
                    "stage": "freshness",
                    "type": "clock_rollback",
                    "path": "/assets/style.css",
                    "node": "edge-c",
                    "rollback_ticks": 20,
                },
                {
                    "seq": 4,
                    "stage": "origin",
                    "type": "stale_304",
                    "path": "/assets/app.js",
                    "node": "edge-a",
                },
                {
                    "seq": 5,
                    "stage": "origin",
                    "type": "request_timeout",
                    "path": "/assets/app.js",
                    "node": "edge-b",
                    "delay_ms": 400,
                },
                {
                    "seq": 6,
                    "stage": "origin",
                    "type": "malformed_headers",
                    "path": "/api/config.json",
                    "node": "edge-b",
                },
                {
                    "seq": 7,
                    "stage": "origin",
                    "type": "unchanged_200",
                    "path": "/feeds/news.json",
                    "node": "edge-c",
                },
            ],
        },
    )
    (SAMPLES / "README.md").write_text(
        "\n".join(
            [
                "# Deterministic local samples",
                "",
                "- `origin_manifest.json`: loopback origin resources and cache policy.",
                "- `nodes.json`: edge nodes, capacity, per-round validation budget.",
                "- `v1_snapshot.json`: generation 1 relative-seconds cache snapshot.",
                "- `v2/shards/*.json`: generation 2 shards with absolute logical clocks.",
                "- `v2_meta.sqlite`: shard metadata and raw-byte SHA-256 hashes.",
                "- `injections.json`: parse, merge, freshness, and origin anomalies.",
                "- `policy.json`: logical clock, tolerances, retry budget, scale threshold.",
                "",
                "All file references are relative to this directory. Regenerate with",
                "`python scripts/generate_samples.py`.",
                "",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()
    print(f"generated deterministic samples in {SAMPLES}", file=sys.stderr)

