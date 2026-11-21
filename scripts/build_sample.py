#!/usr/bin/env python3
"""Build the deterministic local sample package (including its SQLite metadata)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_engine.canonical import pretty_json, sha256_bytes, sha256_value
from config_engine.models import content_digest


SAMPLE = ROOT / "sample"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(value) + "\n", encoding="utf-8")


def v1_document() -> dict[str, Any]:
    return {
        "logical_clock_ms": 1000,
        "records": [
            {
                "effective_after_s": 0,
                "environment": "dev",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {
                    "feature": "basic",
                    "region": "cn-north",
                },
                "replicas": 2,
                "retry_factor": 1.5,
                "service": "orders",
                "timeout_ms": 500,
                "version": "1.0.0",
            },
            {
                "effective_after_s": 5,
                "environment": "prod",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {"currency": "CNY"},
                "replicas": 2,
                "retry_factor": 2.0,
                "service": "billing",
                "timeout_ms": 800,
                "version": "1.0.0",
            },
            {
                "effective_after_s": 0,
                "environment": "prod",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {
                    "feature": "basic",
                    "region": "cn-east",
                },
                "replicas": 2,
                "retry_factor": 1.5,
                "service": "orders",
                "timeout_ms": 600,
                "version": "1.0.0",
            },
            {
                "effective_after_s": 0,
                "environment": "staging",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {"index": "primary"},
                "replicas": 1,
                "retry_factor": 1.2,
                "service": "search",
                "timeout_ms": 300,
                "version": "1.0.0",
            },
            {
                "effective_after_s": 0,
                "environment": "prod",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {"team": "legacy"},
                "replicas": 1,
                "retry_factor": 1.0,
                "service": "legacy",
                "timeout_ms": 1000,
                "version": "1.0.0",
            },
        ],
        "schema_version": 1,
    }


def v2_initial() -> dict[str, Any]:
    return {
        "logical_clock_ms": 1500,
        "records": [
            {
                "effective_at_ms": 1200,
                "environment": "prod",
                "generation": 1,
                "params": {"issuer": "local"},
                "service": "auth",
                "tombstone": False,
                "upstreams": [],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 1000,
                "environment": "prod",
                "generation": 1,
                "params": {"currency": "CNY", "settlement": "T+1"},
                "service": "billing",
                "tombstone": False,
                "upstreams": ["auth"],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 1400,
                "environment": "prod",
                "generation": 1,
                "params": {"feature": "basic", "region": "cn-east"},
                "service": "orders",
                "tombstone": False,
                "upstreams": ["auth"],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 1400,
                "environment": "staging",
                "generation": 1,
                "params": {"index": "primary"},
                "service": "search",
                "tombstone": False,
                "upstreams": [],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 1200,
                "environment": "staging",
                "expires_at_ms": 2500,
                "generation": 1,
                "params": {"format": "daily"},
                "service": "reports",
                "tombstone": False,
                "upstreams": [],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 900,
                "environment": "dev",
                "generation": 1,
                "params": {},
                "service": "cache",
                "tombstone": True,
                "upstreams": [],
                "version": "1.5.0",
            },
            {
                "effective_at_ms": 1000,
                "environment": "prod",
                "generation": 1,
                "params": {},
                "service": "legacy",
                "tombstone": True,
                "upstreams": [],
                "version": "1.5.0",
            },
        ],
        "schema_version": 2,
        "shard_id": "initial",
    }


def v2_update() -> dict[str, Any]:
    return {
        "logical_clock_ms": 2000,
        "records": [
            {
                "effective_at_ms": 1200,
                "environment": "prod",
                "generation": 2,
                "params": {"currency": "CNY", "settlement": "T+0"},
                "service": "billing",
                "tombstone": False,
                "upstreams": ["auth"],
                "version": "2.0.0",
            },
            {
                "effective_at_ms": 2500,
                "environment": "prod",
                "generation": 2,
                "params": {"feature": "tiered", "region": "cn-east"},
                "service": "orders",
                "tombstone": False,
                "upstreams": ["auth"],
                "version": "2.0.0",
            },
            {
                "effective_at_ms": 1800,
                "environment": "staging",
                "generation": 2,
                "params": {"index": "primary-v2"},
                "service": "search",
                "tombstone": False,
                "upstreams": [],
                "version": "2.0.0",
            },
            {
                "effective_at_ms": 2100,
                "environment": "dev",
                "generation": 2,
                "params": {"ttl_s": 60},
                "service": "cache",
                "tombstone": False,
                "upstreams": [],
                "version": "2.0.0",
            },
        ],
        "schema_version": 2,
        "shard_id": "update",
    }


def event(
    *,
    event_id: str,
    seq: int,
    logical_clock_ms: int,
    service: str,
    environment: str,
    version: str,
    generation: int,
    effective_at_ms: int,
    params: dict[str, Any],
    upstreams: list[str] | None = None,
    tombstone: bool = False,
) -> dict[str, Any]:
    upstreams = sorted(upstreams or [])
    digest = content_digest(
        service=service,
        environment=environment,
        generation=generation,
        version=version,
        params=params,
        upstreams=upstreams,
        tombstone=tombstone,
        expires_at_ms=None,
    )
    return {
        "content_digest": digest,
        "effective_at_ms": effective_at_ms,
        "environment": environment,
        "event_id": event_id,
        "generation": generation,
        "logical_clock_ms": logical_clock_ms,
        "params": params,
        "seq": seq,
        "service": service,
        "target_version": version,
        "tombstone": tombstone,
        "upstreams": upstreams,
    }


def build_events() -> tuple[dict[str, Any], dict[str, Any]]:
    manual = {
        "events": [
            event(
                event_id="manual-orders-dev-2.1.0",
                seq=1,
                logical_clock_ms=1200,
                service="orders",
                environment="dev",
                version="2.1.0",
                generation=3,
                effective_at_ms=2200,
                params={"feature": "tiered", "region": "cn-north"},
            ),
            event(
                event_id="manual-orders-prod-2.2.0",
                seq=2,
                logical_clock_ms=2600,
                service="orders",
                environment="prod",
                version="2.2.0",
                generation=3,
                effective_at_ms=3000,
                params={"feature": "canary", "region": "cn-east"},
                upstreams=["auth"],
            ),
            event(
                event_id="manual-billing-prod-tombstone-3.0.0",
                seq=3,
                logical_clock_ms=2800,
                service="billing",
                environment="prod",
                version="3.0.0",
                generation=3,
                effective_at_ms=2800,
                params={},
                tombstone=True,
            ),
        ],
        "schema_version": 1,
        "source": "manual",
    }
    auto = {
        "events": [
            event(
                event_id="auto-orders-dev-2.0.1",
                seq=1,
                logical_clock_ms=1300,
                service="orders",
                environment="dev",
                version="2.0.1",
                generation=3,
                effective_at_ms=2100,
                params={"feature": "auto", "region": "cn-north"},
            ),
            event(
                event_id="auto-orders-prod-2.3.0",
                seq=2,
                logical_clock_ms=2700,
                service="orders",
                environment="prod",
                version="2.3.0",
                generation=3,
                effective_at_ms=2700,
                params={"feature": "auto", "region": "cn-east"},
                upstreams=["auth"],
            ),
            event(
                event_id="auto-auth-prod-2.0.0",
                seq=3,
                logical_clock_ms=2800,
                service="auth",
                environment="prod",
                version="2.0.0",
                generation=2,
                effective_at_ms=2800,
                params={"issuer": "local-v2"},
            ),
            event(
                event_id="auto-search-staging-tombstone-3.0.0",
                seq=4,
                logical_clock_ms=3000,
                service="search",
                environment="staging",
                version="3.0.0",
                generation=3,
                effective_at_ms=3100,
                params={},
                tombstone=True,
            ),
        ],
        "schema_version": 1,
        "source": "auto",
    }
    return manual, auto


def build() -> None:
    write_json(SAMPLE / "v1_manifest.json", v1_document())
    shards = {
        "initial.json": v2_initial(),
        "update.json": v2_update(),
    }
    for name, document in shards.items():
        write_json(SAMPLE / "shards" / name, document)
    manual, auto = build_events()
    write_json(SAMPLE / "events" / "manual.json", manual)
    write_json(SAMPLE / "events" / "auto.json", auto)

    package = {
        "clock": {
            "default_step": "t0",
            "initial_clock_ms": 1000,
            "rollback_tolerance_ms": 250,
            "source_priorities": {"auto": 10, "manual": 20},
            "steps": {
                "t0": 1000,
                "t1": 1400,
                "t2": 2600,
                "t3": 3200,
                "t4": 5000,
            },
        },
        "events": {
            "auto": "events/auto.json",
            "manual": "events/manual.json",
        },
        "schema_version": 1,
        "v1": {"path": "v1_manifest.json"},
        "v2": {
            "metadata_db": "v2_meta.sqlite",
            "shards": ["shards/initial.json", "shards/update.json"],
        },
    }
    write_json(SAMPLE / "package.json", package)

    metadata_path = SAMPLE / "v2_meta.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()
    conn = sqlite3.connect(metadata_path)
    try:
        conn.executescript(
            """
            CREATE TABLE shard_registry (
                shard_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE config_metadata (
                service TEXT NOT NULL,
                environment TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                shard_path TEXT NOT NULL,
                record_digest TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                PRIMARY KEY (service, environment, generation, record_digest)
            );
            """
        )
        for shard_name, document in sorted(shards.items()):
            raw = (SAMPLE / "shards" / shard_name).read_bytes()
            conn.execute(
                "INSERT INTO shard_registry(shard_path, sha256, logical_clock_ms, "
                "schema_version) VALUES(?, ?, ?, 2)",
                (shard_name, sha256_bytes(raw), document["logical_clock_ms"]),
            )
            for record in document["records"]:
                record_digest = content_digest(
                    service=record["service"],
                    environment=record["environment"],
                    generation=record["generation"],
                    version=record["version"],
                    params=record["params"],
                    upstreams=record["upstreams"],
                    tombstone=record["tombstone"],
                    expires_at_ms=record.get("expires_at_ms"),
                )
                conn.execute(
                    "INSERT INTO config_metadata(service, environment, generation, "
                    "effective_at_ms, logical_clock_ms, shard_path, record_digest, tombstone) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["service"],
                        record["environment"],
                        record["generation"],
                        record["effective_at_ms"],
                        document["logical_clock_ms"],
                        shard_name,
                        record_digest,
                        int(record["tombstone"]),
                    ),
                )
        conn.commit()
    finally:
        conn.close()

    manifest = {
        "expected": {
            "t0_states": {
                "auth/prod": "pending",
                "billing/prod": "active",
                "cache/dev": "tombstoned",
                "legacy/prod": "tombstoned",
                "orders/dev": "active",
                "orders/prod": "active",
                "reports/staging": "pending",
                "search/staging": "active",
            },
            "t3_states": {
                "auth/prod": "active",
                "billing/prod": "tombstoned",
                "cache/dev": "active",
                "legacy/prod": "tombstoned",
                "orders/dev": "active",
                "orders/prod": "active",
                "reports/staging": "expired",
                "search/staging": "tombstoned",
            },
            "t3_versions": {
                "auth/prod": "2.0.0",
                "cache/dev": "2.0.0",
                "orders/dev": "2.1.0",
                "orders/prod": "2.3.0",
            },
        },
        "package_sha256": sha256_value(package),
        "purpose": "Deterministic local acceptance sample; rebuild with scripts/build_sample.py",
    }
    write_json(SAMPLE / "expected.json", manifest)


if __name__ == "__main__":
    build()
    print(f"built sample at {SAMPLE}")
