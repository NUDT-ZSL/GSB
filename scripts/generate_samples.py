#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_workbench.util import content_digest


def build_topology() -> dict[str, Any]:
    return {
        "isolation_domains": ["zone-core", "zone-east", "zone-west"],
        "services": [
            {
                "data_seed": 1101,
                "domain": "zone-core",
                "health_weight": 100,
                "initial_primary": "auth-core-a",
                "instances": [
                    {
                        "health_weight": 100,
                        "instance_id": "auth-core-a",
                        "replica_seed": 9001,
                    },
                    {
                        "health_weight": 80,
                        "instance_id": "auth-core-b",
                        "replica_seed": 9002,
                    },
                ],
                "name": "auth",
                "row_count": 24,
                "upstream": [],
            },
            {
                "data_seed": 2201,
                "domain": "zone-east",
                "health_weight": 90,
                "initial_primary": "orders-east-a",
                "instances": [
                    {
                        "health_weight": 100,
                        "instance_id": "orders-east-a",
                        "replica_seed": 9101,
                    },
                    {
                        "health_weight": 70,
                        "instance_id": "orders-east-b",
                        "replica_seed": 9102,
                    },
                ],
                "name": "orders",
                "row_count": 20,
                "upstream": ["auth"],
            },
            {
                "data_seed": 3301,
                "domain": "zone-east",
                "health_weight": 85,
                "initial_primary": "payments-east-a",
                "instances": [
                    {
                        "health_weight": 100,
                        "instance_id": "payments-east-a",
                        "replica_seed": 9201,
                    },
                    {
                        "health_weight": 75,
                        "instance_id": "payments-east-b",
                        "replica_seed": 9202,
                    },
                ],
                "name": "payments",
                "row_count": 28,
                "upstream": ["auth", "orders"],
            },
            {
                "data_seed": 4401,
                "domain": "zone-west",
                "health_weight": 70,
                "initial_primary": "search-west-a",
                "instances": [
                    {
                        "health_weight": 100,
                        "instance_id": "search-west-a",
                        "replica_seed": 9301,
                    },
                    {
                        "health_weight": 65,
                        "instance_id": "search-west-b",
                        "replica_seed": 9302,
                    },
                ],
                "name": "search",
                "row_count": 16,
                "upstream": ["auth"],
            },
            {
                "data_seed": 5501,
                "domain": "zone-west",
                "health_weight": 60,
                "initial_primary": "catalog-west-a",
                "instances": [
                    {
                        "health_weight": 100,
                        "instance_id": "catalog-west-a",
                        "replica_seed": 9401,
                    },
                    {
                        "health_weight": 60,
                        "instance_id": "catalog-west-b",
                        "replica_seed": 9402,
                    },
                ],
                "name": "catalog",
                "row_count": 12,
                "upstream": ["search"],
            },
        ],
    }


def build_policy() -> dict[str, Any]:
    return {
        "clock_regression_tolerance_ms": 2,
        "freeze_batch_size": 2,
        "freeze_order": ["zone-core", "zone-east", "zone-west"],
        "max_concurrency": 6,
        "probe_cost_ms": {"freeze": 12, "switch": 12},
        "services": {
            "auth": {
                "cooldown_ms": 600,
                "failback_ladder": [1.0, 0.5, 0.0],
                "mode": "active_passive",
                "trigger_metric": "error_rate",
                "trigger_threshold": 50,
            },
            "catalog": {
                "cooldown_ms": 500,
                "failback_ladder": [1.0, 0.5, 0.0],
                "mode": "active_passive",
                "trigger_metric": "error_rate",
                "trigger_threshold": 50,
            },
            "orders": {
                "cooldown_ms": 400,
                "failback_ladder": [1.0, 0.5, 0.0],
                "mode": "active_passive",
                "trigger_metric": "error_rate",
                "trigger_threshold": 50,
            },
            "payments": {
                "cooldown_ms": 700,
                "failback_ladder": [1.0, 0.25, 0.0],
                "mode": "dual_active",
                "trigger_metric": "error_rate",
                "trigger_threshold": 50,
            },
            "search": {
                "cooldown_ms": 500,
                "failback_ladder": [1.0, 0.5, 0.0],
                "mode": "active_passive",
                "trigger_metric": "error_rate",
                "trigger_threshold": 50,
            },
        },
        "time_budget_ms": 120,
        "version": 1,
    }


def build_events() -> list[dict[str, Any]]:
    return [
        {
            "instance_id": "auth-core-a",
            "logical_time_ms": 100,
            "metric": "error_rate",
            "seq": 1,
            "value": 82,
            "window": "window-001",
        },
        {
            "instance_id": "orders-east-a",
            "logical_time_ms": 110,
            "metric": "error_rate",
            "seq": 2,
            "value": 75,
            "window": "window-001",
        },
        {
            "instance_id": "payments-east-a",
            "logical_time_ms": 120,
            "metric": "error_rate",
            "seq": 3,
            "value": 91,
            "window": "window-001",
        },
        {
            "instance_id": "catalog-west-a",
            "logical_time_ms": 130,
            "metric": "error_rate",
            "seq": 4,
            "value": 60,
            "window": "window-001",
        },
        {
            "instance_id": "search-west-a",
            "logical_time_ms": 140,
            "metric": "error_rate",
            "seq": 5,
            "value": 20,
            "window": "window-001",
        },
        {
            "instance_id": "auth-core-b",
            "logical_time_ms": 450,
            "metric": "error_rate",
            "seq": 6,
            "value": 10,
            "window": "window-002",
        },
        {
            "instance_id": "orders-east-b",
            "logical_time_ms": 600,
            "metric": "error_rate",
            "seq": 7,
            "value": 15,
            "window": "window-002",
        },
        {
            "instance_id": "payments-east-b",
            "logical_time_ms": 800,
            "metric": "error_rate",
            "seq": 8,
            "value": 12,
            "window": "window-002",
        },
        {
            "instance_id": "search-west-a",
            "logical_time_ms": 2100,
            "metric": "error_rate",
            "seq": 9,
            "value": 99,
            "window": "window-003",
        },
        {
            "instance_id": "search-west-b",
            "logical_time_ms": 2500,
            "metric": "error_rate",
            "seq": 10,
            "value": 5,
            "window": "window-003",
        },
    ]


def build_sample() -> dict[str, Any]:
    return {
        "events": build_events(),
        "policy": build_policy(),
        "sample_id": "local-deterministic-v1",
        "schema_version": 1,
        "topology": build_topology(),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def generate(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    topology = build_topology()
    policy = build_policy()
    events = build_events()
    sample = build_sample()
    sample_digest = content_digest(sample)
    policy_digest = content_digest(policy)
    manifest = {
        "failure_injection": {
            "crash_env": "WORKBENCH_CRASH_AFTER_DOMAIN_COMMITS",
            "data_variant_field": "instances[].data_variant",
        },
        "generator": "scripts/generate_samples.py",
        "policy_digest": policy_digest,
        "query_definitions": [
            "record_count",
            "sum_amount_cents",
            "sum_quantity",
            "amount_cents_by_shard",
            "quantity_by_category",
            "row_fingerprint",
        ],
        "sample_digest": sample_digest,
        "sample_id": sample["sample_id"],
        "schema_version": 1,
    }
    write_json(out_dir / "topology.json", topology)
    write_json(out_dir / "policy.json", policy)
    write_json(out_dir / "events.json", {"events": events, "schema_version": 1})
    write_json(out_dir / "sample.json", sample)
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "samples",
        help="Output directory for deterministic sample files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if existing sample.json does not match regenerated content.",
    )
    args = parser.parse_args()
    out_dir = args.out.resolve()
    if args.check:
        sample_path = out_dir / "sample.json"
        if not sample_path.exists():
            print(f"missing sample: {sample_path}", file=sys.stderr)
            return 1
        existing = json.loads(sample_path.read_text(encoding="utf-8"))
        if content_digest(existing) != content_digest(build_sample()):
            print("sample digest differs from generator output", file=sys.stderr)
            return 1
        print("sample is reproducible")
        return 0
    manifest = generate(out_dir)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

