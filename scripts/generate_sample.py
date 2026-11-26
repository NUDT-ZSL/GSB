#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

SEED = 20261127


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_v2_metadata(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            DROP TABLE IF EXISTS policy_meta;
            DROP TABLE IF EXISTS thresholds;
            DROP TABLE IF EXISTS tombstones;
            DROP TABLE IF EXISTS resource_dependencies;

            CREATE TABLE policy_meta (
                snapshot_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_clock_ms INTEGER NOT NULL
            );
            CREATE TABLE thresholds (
                tenant TEXT PRIMARY KEY,
                resource_key TEXT NOT NULL,
                threshold_quota TEXT NOT NULL
            );
            CREATE TABLE tombstones (
                target_snapshot_id TEXT NOT NULL,
                target_version TEXT NOT NULL,
                tenant TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                invalidated_generation INTEGER NOT NULL,
                effective_clock_ms INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE resource_dependencies (
                resource_key TEXT NOT NULL,
                depends_on TEXT NOT NULL,
                PRIMARY KEY (resource_key, depends_on)
            );
            """
        )
        connection.execute(
            "INSERT INTO policy_meta(snapshot_id, version, generation, effective_clock_ms) "
            "VALUES('v2-main', 'v2', 2, 4000)"
        )
        connection.executemany(
            "INSERT INTO thresholds(tenant, resource_key, threshold_quota) VALUES(?,?,?)",
            [
                ("t-alpha", "orders", "680"),
                ("t-beta", "orders", "560"),
                ("t-gamma", "payments", "400"),
            ],
        )
        connection.executemany(
            "INSERT INTO tombstones("
            "target_snapshot_id, target_version, tenant, resource_key, "
            "invalidated_generation, effective_clock_ms, reason"
            ") VALUES(?,?,?,?,?,?,?)",
            [
                ("v1-main", "v1", "t-alpha", "orders", 1, 4000, "superseded-v1"),
                ("v1-main", "v1", "t-beta", "orders", 1, 4000, "superseded-v1"),
            ],
        )
        connection.execute(
            "INSERT INTO resource_dependencies(resource_key, depends_on) "
            "VALUES('catalog', 'orders')"
        )
        connection.commit()
    finally:
        connection.close()


def generate(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    rng = np.random.default_rng(SEED)
    probe = [int(value) for value in rng.integers(0, 1000, size=8)]

    configs = [
        {
            "tenant": "t-alpha",
            "resource_key": "orders",
            "baseline_quota": 500,
            "elasticity_ratio": "1.30",
            "lease_duration_ms": 3000,
            "quota_generation": 1,
        },
        {
            "tenant": "t-beta",
            "resource_key": "orders",
            "baseline_quota": 400,
            "elasticity_ratio": "1.25",
            "lease_duration_ms": 3000,
            "quota_generation": 1,
        },
        {
            "tenant": "t-gamma",
            "resource_key": "payments",
            "baseline_quota": 300,
            "elasticity_ratio": "1.20",
            "lease_duration_ms": 2500,
            "quota_generation": 1,
        },
        {
            "tenant": "t-delta",
            "resource_key": "catalog",
            "baseline_quota": 200,
            "elasticity_ratio": "1.10",
            "lease_duration_ms": 2000,
            "quota_generation": 1,
            "depends_on_resource_keys": ["orders"],
        },
    ]
    _write_json(out_dir / "quota_configs.json", configs)

    applications = [
        {
            "application_seq": 1,
            "tenant": "t-alpha",
            "resource_key": "orders",
            "requested_quota": 250,
            "logical_clock_ms": 0,
            "priority": 5,
        },
        {
            "application_seq": 2,
            "tenant": "t-beta",
            "resource_key": "orders",
            "requested_quota": 200,
            "logical_clock_ms": 1000,
            "priority": 7,
        },
        {
            "application_seq": 3,
            "tenant": "t-gamma",
            "resource_key": "payments",
            "requested_quota": 120,
            "logical_clock_ms": 1000,
            "priority": 6,
        },
        {
            "application_seq": 4,
            "tenant": "t-delta",
            "resource_key": "catalog",
            "requested_quota": 120,
            "logical_clock_ms": 2000,
            "priority": 4,
        },
        {
            "application_seq": 5,
            "tenant": "t-alpha",
            "resource_key": "orders",
            "requested_quota": 280,
            "logical_clock_ms": 4000,
            "priority": 8,
        },
        {
            "application_seq": 6,
            "tenant": "t-beta",
            "resource_key": "orders",
            "requested_quota": 180,
            "logical_clock_ms": 4000,
            "priority": 9,
        },
        {
            "application_seq": 7,
            "tenant": "t-gamma",
            "resource_key": "payments",
            "requested_quota": 120,
            "logical_clock_ms": 5000,
            "priority": 5,
        },
        {
            "application_seq": 8,
            "tenant": "t-delta",
            "resource_key": "catalog",
            "requested_quota": 100,
            "logical_clock_ms": 6000,
            "priority": 4,
        },
        {
            "application_seq": 9,
            "tenant": "t-alpha",
            "resource_key": "orders",
            "requested_quota": 350,
            "logical_clock_ms": 7000,
            "priority": 10,
        },
        {
            "application_seq": 10,
            "tenant": "t-beta",
            "resource_key": "orders",
            "requested_quota": 600,
            "logical_clock_ms": 8000,
            "priority": 9,
        },
        {
            "application_seq": 11,
            "tenant": "t-gamma",
            "resource_key": "payments",
            "requested_quota": 200,
            "logical_clock_ms": 8000,
            "priority": 6,
        },
    ]
    _write_json(out_dir / "applications.json", applications)

    releases = [
        {
            "release_seq": 101,
            "application_seq": 1,
            "tenant": "t-alpha",
            "resource_key": "orders",
            "actual_quota": 300,
            "logical_clock_ms": 2500,
        },
        {
            "release_seq": 102,
            "application_seq": 2,
            "tenant": "t-beta",
            "resource_key": "orders",
            "actual_quota": 260,
            "logical_clock_ms": 3000,
        },
        {
            "release_seq": 103,
            "application_seq": 3,
            "tenant": "t-gamma",
            "resource_key": "payments",
            "actual_quota": 130,
            "logical_clock_ms": 3500,
        },
        {
            "release_seq": 104,
            "application_seq": 4,
            "tenant": "t-delta",
            "resource_key": "catalog",
            "actual_quota": 110,
            "logical_clock_ms": 4500,
        },
        {
            "release_seq": 105,
            "application_seq": 7,
            "tenant": "t-gamma",
            "resource_key": "payments",
            "actual_quota": 130,
            "logical_clock_ms": 6500,
        },
        {
            "release_seq": 106,
            "application_seq": 5,
            "tenant": "t-alpha",
            "resource_key": "orders",
            "actual_quota": 200,
            "logical_clock_ms": 7500,
        },
        {
            "release_seq": 107,
            "application_seq": 6,
            "tenant": "t-beta",
            "resource_key": "orders",
            "actual_quota": 180,
            "logical_clock_ms": 7500,
        },
        {
            "release_seq": 108,
            "application_seq": 8,
            "tenant": "t-delta",
            "resource_key": "catalog",
            "actual_quota": 90,
            "logical_clock_ms": 7500,
        },
    ]
    _write_json(out_dir / "releases.json", releases)

    observation_specs = [
        (2001, "t-alpha", 620, 1000),
        (2002, "t-beta", 470, 1500),
        (2003, "t-delta", 290, 2500),
        (2004, "t-alpha", 600, 3500),
        (2005, "t-gamma", 280, 3500),
        (2006, "t-alpha", 850, 4000),
        (2007, "t-beta", 700, 4000),
        (2008, "t-gamma", 300, 4000),
        (2009, "t-gamma", 320, 5000),
        (2010, "t-delta", 330, 6000),
        (2011, "t-alpha", 1150, 7000),
        (2012, "t-beta", 900, 7000),
        (2013, "t-gamma", 600, 7000),
        (2014, "t-gamma", 700, 8000),
        (2015, "t-delta", 450, 8000),
        (2016, "t-beta", 600, 9000),
        (2017, "t-gamma", 420, 10000),
    ]
    observations = [
        {
            "observation_seq": seq,
            "tenant": tenant,
            "actual_occupancy": actual,
            "logical_clock_ms": clock,
        }
        for seq, tenant, actual, clock in observation_specs
    ]
    _write_json(out_dir / "observations.json", observations)

    _write_json(
        out_dir / "dataset.json",
        {
            "round_span_ms": 1000,
            "seed": SEED,
            "numpy_probe": probe,
            "description": "deterministic local quota-ledger fixture",
        },
    )

    v1 = {
        "snapshot_id": "v1-main",
        "version": "v1",
        "generation": 1,
        "created_clock_ms": 0,
        "effective_after_seconds": "0",
        "global_quota_budget": 2200,
        "round_threshold_budget": 120,
        "per_tenant_adjustment_cap": 60,
        "safety_watermark": 40,
        "drift_threshold_ratio": "0.20",
        "danger_threshold_ratio": "0.40",
        "thresholds": {
            "t-alpha": 550,
            "t-beta": 450,
            "t-gamma": 350,
            "t-delta": 250,
        },
    }
    _write_json(out_dir / "snapshots" / "v1" / "policy.json", v1)

    v2_dir = out_dir / "snapshots" / "v2" / "main"
    v2 = {
        "snapshot_id": "v2-main",
        "version": "v2",
        "generation": 2,
        "created_clock_ms": 4000,
        "effective_clock_ms": 4000,
        "metadata_db": "meta.sqlite",
        "shards": [
            "shards/orders.json",
            "shards/payments.json",
            "shards/catalog.json",
        ],
        "global_quota_budget": 2400,
        "round_threshold_budget": 90,
        "per_tenant_adjustment_cap": 50,
        "safety_watermark": 45,
        "drift_threshold_ratio": "0.15",
        "danger_threshold_ratio": "0.40",
    }
    _write_json(v2_dir / "policy.json", v2)
    _write_v2_metadata(v2_dir / "meta.sqlite")
    for resource_key, note in (
        ("orders", "shared order lock shard"),
        ("payments", "payment lock shard"),
        ("catalog", "catalog shard; depends on orders invalidation"),
    ):
        _write_json(
            v2_dir / "shards" / f"{resource_key}.json",
            {
                "resource_key": resource_key,
                "parameters": {"note": note},
                "tenant_resource_bindings": {
                    config["tenant"]: config["resource_key"]
                    for config in configs
                    if config["resource_key"] == resource_key
                },
            },
        )
    print(
        json.dumps(
            {
                "ok": True,
                "out_dir": str(out_dir),
                "seed": SEED,
                "configs": len(configs),
                "applications": len(applications),
                "releases": len(releases),
                "observations": len(observations),
                "numpy_probe": probe,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic quota-ledger sample")
    parser.add_argument("--out-dir", default="sample")
    args = parser.parse_args()
    generate(Path(args.out_dir).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

