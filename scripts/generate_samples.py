#!/usr/bin/env python3
"""Generate the deterministic local manifests, cluster state, and project config."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPOSITORY_ROOT / "samples"
SEED = 20261121
ENVIRONMENTS = ("dev", "staging", "prod")
V1_CLOCK_MS = 1_763_510_400_000


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(value), encoding="utf-8", newline="\n")


def _resources(index: int) -> dict[str, int]:
    return {
        "cpu_millicores": 150 + index * 50,
        "memory_mib": 128 + index * 64,
    }


def _v1_manifest() -> dict[str, Any]:
    rng = random.Random(SEED)
    applications = {
        "postgres": {
            "effective_seconds": 0,
            "image_version": "14.2.0",
            "params": {
                "depends_on": [],
                "engine": "postgresql",
                "namespace_hint": f"data-{rng.randrange(100, 999)}",
            },
            "replicas": 1,
        },
        "identity-api": {
            "effective_seconds": 30,
            "image_version": "1.8.0",
            "params": {
                "depends_on": ["postgres"],
                "tier": "edge",
                "timeout_seconds": 5,
            },
            "replicas": 2,
        },
        "catalog-api": {
            "effective_seconds": 60,
            "image_version": "2.2.0",
            "params": {
                "depends_on": ["postgres"],
                "tier": "service",
                "timeout_seconds": 8,
            },
            "replicas": 2,
        },
        "checkout-api": {
            "effective_seconds": 90,
            "image_version": "3.1.0",
            "params": {
                "depends_on": ["catalog-api", "identity-api"],
                "tier": "service",
                "timeout_seconds": 10,
            },
            "replicas": 2,
        },
        "web-frontend": {
            "effective_seconds": 120,
            "image_version": "5.0.0",
            "params": {
                "depends_on": ["checkout-api"],
                "tier": "edge",
                "timeout_seconds": 3,
            },
            "replicas": 2,
        },
        "worker": {
            "effective_seconds": 180,
            "image_version": "1.4.0",
            "params": {
                "depends_on": ["postgres"],
                "queue": "jobs",
                "timeout_seconds": 30,
            },
            "replicas": 1,
        },
        "mailer": {
            "effective_seconds": 300,
            "image_version": "0.7.0",
            "params": {
                "depends_on": ["identity-api"],
                "tier": "sidecar",
                "timeout_seconds": 15,
            },
            "replicas": 1,
        },
    }
    return {
        "applications": applications,
        "format": "v1",
        "logical_clock_ms": V1_CLOCK_MS,
        "manifest_generation": 10,
        "seed": SEED,
    }


def _v2_specs() -> dict[str, dict[str, Any]]:
    common_dev = [
        ("postgres", "14.3.0", 2, []),
        ("identity-api", "1.9.0", 3, ["postgres"]),
        ("catalog-api", "2.3.0", 3, ["postgres"]),
        ("checkout-api", "3.2.0", 3, ["catalog-api", "identity-api"]),
        ("web-frontend", "5.1.0", 2, ["checkout-api"]),
        ("worker", "1.5.0", 2, ["postgres"]),
        ("analytics-api", "0.9.0", 1, ["postgres"]),
        ("legacy-reports", "1.0.0", 1, []),
    ]
    common_staging = [
        ("postgres", "14.2.1", 1, []),
        ("identity-api", "1.8.1", 2, ["postgres"]),
        ("catalog-api", "2.2.1", 2, ["postgres"]),
        ("checkout-api", "3.1.1", 2, ["catalog-api", "identity-api"]),
        ("web-frontend", "5.0.1", 2, ["checkout-api"]),
        ("worker", "1.4.1", 1, ["postgres"]),
        ("analytics-api", "0.8.1", 1, ["postgres"]),
        ("legacy-reports", "1.0.0", 1, []),
    ]
    common_prod = [
        ("postgres", "14.4.0", 2, []),
        ("identity-api", "2.0.0", 3, ["postgres"]),
        ("catalog-api", "2.4.0", 3, ["postgres"]),
        ("checkout-api", "3.3.0", 3, ["catalog-api", "identity-api"]),
        ("web-frontend", "5.2.0", 2, ["checkout-api"]),
        ("worker", "1.6.0", 2, ["postgres"]),
        ("analytics-api", "1.0.0", 1, ["postgres"]),
        ("legacy-reports", "1.0.0", 1, []),
    ]
    return {
        "dev": {
            "clock_ms": 1_763_510_700_000,
            "generation": 12,
            "apps": common_dev,
        },
        "staging": {
            "clock_ms": V1_CLOCK_MS,
            "generation": 10,
            "apps": common_staging,
        },
        "prod": {
            "clock_ms": 1_763_510_800_000,
            "generation": 13,
            "apps": common_prod,
        },
    }


def _v2_shard(
    environment: str,
    generation: int,
    clock_ms: int,
    apps: list[tuple[str, str, int, list[str]]],
) -> dict[str, Any]:
    records = []
    for index, (name, image, replicas, dependencies) in enumerate(apps):
        records.append(
            {
                "depends_on": dependencies,
                "effective_at_ms": clock_ms + index * 1000,
                "generation": generation,
                "image_version": image,
                "name": name,
                "replicas": replicas,
                "resources": _resources(index),
                "tombstone": name == "legacy-reports",
            }
        )
    return {
        "applications": records,
        "environment": environment,
        "format": "v2",
        "generation": generation,
        "logical_clock_ms": clock_ms,
        "seed": SEED,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_v2(specs: dict[str, dict[str, Any]]) -> None:
    v2_dir = SAMPLES_DIR / "v2"
    shards_dir = v2_dir / "shards"
    if shards_dir.exists():
        shutil.rmtree(shards_dir)
    (shards_dir / "history").mkdir(parents=True, exist_ok=True)
    metadata_path = v2_dir / "metadata.db"
    if metadata_path.exists():
        metadata_path.unlink()

    rows: list[tuple[Any, ...]] = []
    for environment in ENVIRONMENTS:
        spec = specs[environment]
        current_generation = int(spec["generation"])
        current_clock = int(spec["clock_ms"])
        current_shard = _v2_shard(
            environment,
            current_generation,
            current_clock,
            spec["apps"],
        )
        current_path = shards_dir / f"{environment}.json"
        _write_json(current_path, current_shard)
        for sequence, generation in enumerate(
            range(current_generation - 2, current_generation + 1),
            start=1,
        ):
            clock_ms = current_clock - (current_generation - generation) * 1000
            shard = _v2_shard(
                environment,
                generation,
                clock_ms,
                spec["apps"],
            )
            if generation == current_generation:
                path = current_path
                shard_file = f"{environment}.json"
            else:
                path = shards_dir / "history" / f"{environment}.g{generation}.json"
                _write_json(path, shard)
                shard_file = f"history/{environment}.g{generation}.json"
            rows.append(
                (
                    environment,
                    sequence,
                    generation,
                    clock_ms,
                    shard_file,
                    _sha256_file(path),
                )
            )

    connection = sqlite3.connect(str(metadata_path))
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE metadata_info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE manifest_revisions (
                environment TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                shard_file TEXT NOT NULL,
                shard_sha256 TEXT NOT NULL,
                PRIMARY KEY (environment, sequence),
                UNIQUE (environment, generation)
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata_info(key, value) VALUES(?, ?)",
            [
                ("format", "v2-metadata-v1"),
                ("seed", str(SEED)),
            ],
        )
        connection.executemany(
            """
            INSERT INTO manifest_revisions(
                environment, sequence, generation,
                logical_clock_ms, shard_file, shard_sha256
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()


def _cluster_state() -> dict[str, Any]:
    dev = {
        "postgres": {"committed_manifest_revision": 10, "image_version": "14.2.0", "replicas": 1},
        "identity-api": {"committed_manifest_revision": 10, "image_version": "1.8.0", "replicas": 2},
        "catalog-api": {"committed_manifest_revision": 10, "image_version": "2.4.0", "replicas": 2},
        "checkout-api": {"committed_manifest_revision": 10, "image_version": "3.1.0", "replicas": 2},
        "web-frontend": {"committed_manifest_revision": 12, "image_version": "5.1.0", "replicas": 2},
        "worker": {"committed_manifest_revision": 10, "image_version": "1.4.0", "replicas": 1},
        "mailer": {"committed_manifest_revision": 9, "image_version": "0.6.0", "replicas": 1},
        "legacy-reports": {"committed_manifest_revision": 9, "image_version": "1.0.0", "replicas": 1},
    }
    staging = {
        "postgres": {"committed_manifest_revision": 9, "image_version": "14.2.0", "replicas": 1},
        "identity-api": {"committed_manifest_revision": 9, "image_version": "1.8.0", "replicas": 2},
        "catalog-api": {"committed_manifest_revision": 9, "image_version": "2.2.0", "replicas": 2},
        "checkout-api": {"committed_manifest_revision": 14, "image_version": "3.1.0", "replicas": 2},
        "web-frontend": {"committed_manifest_revision": 9, "image_version": "5.0.0", "replicas": 2},
        "worker": {"committed_manifest_revision": 9, "image_version": "1.4.0", "replicas": 1},
        "mailer": {"committed_manifest_revision": 9, "image_version": "0.7.0", "replicas": 1},
        "legacy-reports": {"committed_manifest_revision": 9, "image_version": "1.0.0", "replicas": 1},
    }
    prod = {
        "postgres": {"committed_manifest_revision": 12, "image_version": "14.5.0", "replicas": 1},
        "identity-api": {"committed_manifest_revision": 12, "image_version": "1.9.0", "replicas": 2},
        "catalog-api": {"committed_manifest_revision": 12, "image_version": "2.3.0", "replicas": 2},
        "checkout-api": {"committed_manifest_revision": 12, "image_version": "3.2.0", "replicas": 2},
        "web-frontend": {"committed_manifest_revision": 12, "image_version": "5.1.0", "replicas": 2},
        "worker": {"committed_manifest_revision": 12, "image_version": "1.5.0", "replicas": 1},
        "mailer": {"committed_manifest_revision": 9, "image_version": "0.6.0", "replicas": 1},
        "legacy-reports": {"committed_manifest_revision": 9, "image_version": "1.0.0", "replicas": 1},
    }
    return {
        "environments": {
            "dev": {"apps": dev},
            "prod": {"apps": prod},
            "staging": {"apps": staging},
        },
        "format": "cluster-state-v1",
        "seed": SEED,
    }


def _project_config() -> dict[str, Any]:
    return {
        "clock": {
            "clock_tolerance_ms": 5000,
            "default_budget_ms": 5000,
            "initial_clock_ms": V1_CLOCK_MS,
            "operation_cost_ms": 250,
        },
        "cluster_state": "cluster-state.json",
        "environments": list(ENVIRONMENTS),
        "format": "gitops-project-v1",
        "name": "local-feedback-demo",
        "runtime": {
            "cluster_lock": "runtime/cluster-state.lock",
            "database": "runtime/controller.db",
        },
        "seed": SEED,
        "sources": {
            "v1": "v1/manifest.json",
            "v2": {
                "metadata_db": "v2/metadata.db",
                "shards_dir": "v2/shards",
            },
        },
    }


def reset_runtime() -> None:
    runtime = SAMPLES_DIR / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    for name in (
        "controller.db",
        "controller.db-shm",
        "controller.db-wal",
        "cluster-state.lock",
    ):
        path = runtime / name
        if path.exists():
            path.unlink()
    for path in runtime.glob("rollback-*.lock"):
        path.unlink()


def generate(*, clean_runtime: bool = False) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    (SAMPLES_DIR / "v1").mkdir(parents=True, exist_ok=True)
    _write_json(SAMPLES_DIR / "v1" / "manifest.json", _v1_manifest())
    _write_v2(_v2_specs())
    _write_json(SAMPLES_DIR / "cluster-state.json", _cluster_state())
    _write_json(SAMPLES_DIR / "project.json", _project_config())
    if clean_runtime:
        reset_runtime()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset-runtime",
        action="store_true",
        help="delete the local controller database after generating samples",
    )
    args = parser.parse_args(argv)
    generate(clean_runtime=args.reset_runtime)
    print(
        json.dumps(
            {
                "ok": True,
                "project": str(SAMPLES_DIR / "project.json"),
                "seed": SEED,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
