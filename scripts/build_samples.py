#!/usr/bin/env python3
"""Build deterministic local v1/v2 sample manifests and SQLite metadata."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
ENVIRONMENTS = ("development", "staging", "production")
BASE_CLOCK_MS = 1_735_693_200_000
V1_LOGICAL_CLOCKS = {
    "development": 1_735_689_600.125,
    "staging": 1_735_689_600.250,
    "production": 1_735_689_600.375,
}
DEFAULT_TTL_MS = 172_800_000


PARAMETER_TYPES = {
    "image_tag": "string",
    "replicas": "integer",
    "timeout_ms": "integer",
    "retry_factor": "number",
    "resource_limit": {
        "type": "object",
        "fields": {
            "cpu_millicores": "integer",
            "memory_mb": "integer",
        },
    },
    "strategy": "string",
    "max_burst": "integer",
}


def params(
    *,
    service: str,
    environment: str,
    timeout_ms: int,
    retry_factor: float,
    cpu_millicores: int,
    memory_mb: int,
    replicas: int,
    include_v2_extensions: bool = True,
    strategy: str = "rolling",
    max_burst: int = 4,
) -> dict:
    value = {
        "image_tag": f"registry.local/{service}:1.4.{ENVIRONMENTS.index(environment)}",
        "replicas": replicas,
        "timeout_ms": timeout_ms,
        "retry_factor": retry_factor,
        "resource_limit": {
            "cpu_millicores": cpu_millicores,
            "memory_mb": memory_mb,
        },
    }
    if include_v2_extensions:
        value["strategy"] = strategy
        value["max_burst"] = max_burst
    return value


def service_table(environment: str) -> dict[str, dict]:
    env_index = ENVIRONMENTS.index(environment)
    table = {
        "auth-service": params(
            service="auth-service",
            environment=environment,
            timeout_ms=300,
            retry_factor=1.5,
            cpu_millicores=250,
            memory_mb=256,
            replicas=2 + env_index,
        ),
        "inventory-api": params(
            service="inventory-api",
            environment=environment,
            timeout_ms=450,
            retry_factor=1.25,
            cpu_millicores=400,
            memory_mb=512,
            replicas=2,
        ),
        "orders-api": params(
            service="orders-api",
            environment=environment,
            timeout_ms=850,
            retry_factor=1.8,
            cpu_millicores=600,
            memory_mb=768,
            replicas=3 + env_index,
        ),
        "payments-api": params(
            service="payments-api",
            environment=environment,
            timeout_ms=1200,
            retry_factor=(1.35 if environment == "production" else 1.10),
            cpu_millicores=500,
            memory_mb=768,
            replicas=2,
        ),
        "gateway": params(
            service="gateway",
            environment=environment,
            timeout_ms=250,
            retry_factor=2.0,
            cpu_millicores=300,
            memory_mb=512,
            replicas=2,
        ),
        "ledger-api": params(
            service="ledger-api",
            environment=environment,
            timeout_ms=700,
            retry_factor=1.2,
            cpu_millicores=500,
            memory_mb=768,
            replicas=2,
        ),
        "legacy-worker": params(
            service="legacy-worker",
            environment=environment,
            timeout_ms=900,
            retry_factor=1.1,
            cpu_millicores=200,
            memory_mb=256,
            replicas=1,
            include_v2_extensions=False,
        ),
        "reporting-api": params(
            service="reporting-api",
            environment=environment,
            timeout_ms=500,
            retry_factor=1.1,
            cpu_millicores=200,
            memory_mb=256,
            replicas=1,
            include_v2_extensions=False,
        ),
        "retired-cache": params(
            service="retired-cache",
            environment=environment,
            timeout_ms=100,
            retry_factor=1.0,
            cpu_millicores=100,
            memory_mb=128,
            replicas=1,
            include_v2_extensions=False,
        ),
    }
    return table


UPSTREAMS = {
    "auth-service": [],
    "inventory-api": ["auth-service"],
    "orders-api": ["auth-service", "inventory-api"],
    "payments-api": ["auth-service", "ledger-api"],
    "gateway": ["orders-api", "payments-api"],
    "ledger-api": ["auth-service"],
    "legacy-worker": [],
    "reporting-api": [],
    "retired-cache": [],
}


def build_v1() -> None:
    environments = {}
    for environment in ENVIRONMENTS:
        services = []
        table = service_table(environment)
        for name in sorted(table):
            value = table[name]
            if name == "orders-api":
                value["timeout_ms"] = 800
            item = {
                "name": name,
                "image_tag": value["image_tag"],
                "replicas": value["replicas"],
                "timeout_ms": value["timeout_ms"],
                "retry_factor": value["retry_factor"],
                "resource_limit": value["resource_limit"],
                "params": value,
            }
            if name == "retired-cache":
                item["ttl_ms"] = 600_000
            services.append(item)
        environments[environment] = {
            "logical_clock": V1_LOGICAL_CLOCKS[environment],
            "ttl_ms": DEFAULT_TTL_MS,
            "services": services,
        }
    payload = {
        "api_version": "deployment-manifest/v1",
        "manifest_version": "1.0.0",
        "clock_scale_ms": 1000,
        "rounding": "floor",
        "ttl_ms": DEFAULT_TTL_MS,
        "parameter_types": PARAMETER_TYPES,
        "environments": environments,
    }
    path = SAMPLES / "v1" / "manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def build_v2(
    *,
    target: Path,
    manifest_version: str,
    generation_min: int,
    created_at_ms: int,
    production_retry_factor: float,
) -> None:
    shards_dir = target / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    for old in shards_dir.glob("*.json"):
        old.unlink()
    meta_path = target / "meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()

    shard_specs = []
    generation = generation_min
    for environment in ENVIRONMENTS:
        shard_specs.append(
            (
                f"{environment}-core",
                environment,
                generation,
                ["auth-service", "orders-api", "payments-api"],
            )
        )
        generation += 1
        shard_specs.append(
            (
                f"{environment}-edge",
                environment,
                generation,
                ["gateway", "inventory-api", "ledger-api", "legacy-worker"],
            )
        )
        generation += 1

    shard_rows = []
    for shard_id, environment, shard_generation, names in shard_specs:
        table = service_table(environment)
        if environment == "production":
            table["payments-api"]["retry_factor"] = production_retry_factor
        services = []
        for name in names:
            if name == "legacy-worker":
                services.append(
                    {
                        "name": name,
                        "effective_at_ms": BASE_CLOCK_MS,
                        "generation": shard_generation,
                        "params": {},
                        "upstreams": [],
                        "tombstone": True,
                    }
                )
                continue
            effective_at_ms = BASE_CLOCK_MS
            if environment == "development" and name == "auth-service":
                effective_at_ms = 1_735_689_600_125
            services.append(
                {
                    "name": name,
                    "effective_at_ms": effective_at_ms,
                    "generation": shard_generation,
                    "params": table[name],
                    "upstreams": UPSTREAMS[name],
                    "ttl_ms": DEFAULT_TTL_MS,
                    "tombstone": False,
                }
            )
        payload = {
            "shard_id": shard_id,
            "manifest_version": manifest_version,
            "generation": shard_generation,
            "environment": environment,
            "services": services,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        relative_path = f"{shard_id}.json"
        (shards_dir / relative_path).write_bytes(encoded)
        shard_rows.append(
            (
                shard_id,
                relative_path,
                "sha256:" + hashlib.sha256(encoded).hexdigest(),
                shard_generation,
                environment,
                10,
            )
        )

    connection = sqlite3.connect(meta_path)
    try:
        connection.executescript(
            """
            CREATE TABLE package_meta (
                manifest_version TEXT NOT NULL,
                generation_min INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                parameter_schema_json TEXT NOT NULL,
                compatibility_defaults_json TEXT NOT NULL
            );
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                digest TEXT NOT NULL,
                generation INTEGER NOT NULL,
                environment TEXT NOT NULL,
                priority INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO package_meta(
                manifest_version, generation_min, created_at_ms,
                parameter_schema_json, compatibility_defaults_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                manifest_version,
                generation_min,
                created_at_ms,
                json.dumps(PARAMETER_TYPES, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    {"strategy": "rolling", "max_burst": 0},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO shards(
                shard_id, relative_path, digest, generation, environment, priority
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            shard_rows,
        )
        connection.commit()
    finally:
        connection.close()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_config_and_controls() -> None:
    config = {
        "environment_order": list(ENVIRONMENTS),
        "tolerances": {
            "timeout_ms": 0,
            "retry_factor": 0.05,
            "resource_limit": {
                "cpu_millicores": 25,
                "memory_mb": 32,
            },
        },
        "clock_tolerance_ms": 1000,
        "rollback_policy": "reject",
        "rollback_window_ms": 300000,
        "paths": {
            "v1": "v1/manifest.yaml",
            "v2_meta": "v2/meta.sqlite",
            "v2_shards": "v2/shards",
            "v2_next_meta": "v2_next/meta.sqlite",
            "v2_next_shards": "v2_next/shards",
            "clock_steps": "clock_steps.json",
            "injections": "injections.json",
        },
    }
    write_json(SAMPLES / "config.json", config)
    write_json(
        SAMPLES / "clock_steps.json",
        {
            "initial_clock_ms": BASE_CLOCK_MS,
            "steps": [
                {"name": "initial_commit", "now_ms": BASE_CLOCK_MS},
                {"name": "within_tolerance_rollback", "now_ms": BASE_CLOCK_MS + 500},
                {
                    "name": "beyond_tolerance_rollback",
                    "now_ms": BASE_CLOCK_MS - 5000,
                },
                {"name": "later_clock", "now_ms": BASE_CLOCK_MS + 3_600_000},
            ],
        },
    )
    common = {"enabled": False}
    write_json(
        SAMPLES / "injections.json",
        {
            "injections": [
                {
                    "id": "inj-malformed-yaml",
                    "stage": "parse",
                    "kind": "malformed_yaml_field",
                    "description": "Mutates an in-memory v1 timeout field.",
                    "parameters": {
                        "environment": "development",
                        "service": "orders-api",
                        "field": "timeout_ms",
                        "value": -5,
                    },
                    **common,
                },
                {
                    "id": "inj-digest-mismatch",
                    "stage": "parse",
                    "kind": "shard_digest_mismatch",
                    "description": "Forces an in-memory metadata digest mismatch.",
                    "parameters": {"shard_id": "development-core"},
                    **common,
                },
                {
                    "id": "inj-generation-conflict",
                    "stage": "merge",
                    "kind": "generation_conflict",
                    "description": "Raises a controlled merge generation conflict.",
                    "parameters": {
                        "environment": "development",
                        "service": "orders-api",
                        "expected_generation": 12,
                        "actual_generation": 11,
                    },
                    **common,
                },
                {
                    "id": "inj-migration-failure",
                    "stage": "migrate",
                    "kind": "migration_failure",
                    "description": "Raises a controlled v1-to-v2 migration failure.",
                    "parameters": {
                        "environment": "development",
                        "service": "reporting-api",
                    },
                    **common,
                },
                {
                    "id": "inj-dependency-cycle",
                    "stage": "evaluate",
                    "kind": "dependency_cycle",
                    "description": "Adds an in-memory dependency edge that creates a cycle.",
                    "parameters": {
                        "environment": "development",
                        "service": "auth-service",
                        "add_upstream": "gateway",
                    },
                    **common,
                },
                {
                    "id": "inj-clock-rollback",
                    "stage": "evaluate",
                    "kind": "clock_rollback",
                    "description": "Raises a controlled logical clock rollback.",
                    "parameters": {"rollback_ms": 5000},
                    **common,
                },
            ]
        },
    )


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    build_v1()
    build_v2(
        target=SAMPLES / "v2",
        manifest_version="2.1.0",
        generation_min=11,
        created_at_ms=BASE_CLOCK_MS,
        production_retry_factor=1.35,
    )
    build_v2(
        target=SAMPLES / "v2_next",
        manifest_version="2.2.0",
        generation_min=21,
        created_at_ms=BASE_CLOCK_MS + 7_200_000,
        production_retry_factor=1.10,
    )
    build_config_and_controls()
    print("built samples under", SAMPLES)


if __name__ == "__main__":
    main()
