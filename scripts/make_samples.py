#!/usr/bin/env python3
"""Regenerate deterministic loopback samples with fixed ports and seed."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mesh_drift.canon import json_digest, write_json

SEED = 20261123


def route(rule_id: str, path: str, label: str, weight: int):
    return {
        "rule_id": rule_id,
        "path": path,
        "version_label": label,
        "weight": weight,
    }


def main() -> None:
    checkout_routes = [
        route("checkout-pay-canary", "/pay", "v2", 10),
        route("checkout-pay-stable", "/pay", "v1", 90),
    ]
    orders_routes = [
        route("orders-submit-canary", "/submit", "v4", 20),
        route("orders-submit-stable", "/submit", "v3", 80),
    ]
    checkout_digest = json_digest(checkout_routes)
    orders_digest = json_digest(orders_routes)
    expected = {
        "schema_version": 1,
        "snapshot_id": "expected-20261123-01",
        "logical_clock_ms": 10000,
        "services": [
            {
                "service": "checkout",
                "routes": checkout_routes,
                "instances": [
                    {
                        "instance_id": "checkout-a",
                        "endpoint": "http://127.0.0.1:18101/report",
                        "version": 14,
                        "digest": checkout_digest,
                        "version_label": "v1",
                        "pull_cost_logic_ms": 100,
                    },
                    {
                        "instance_id": "checkout-b",
                        "endpoint": "http://127.0.0.1:18102/report",
                        "version": 14,
                        "digest": checkout_digest,
                        "version_label": "v2",
                        "pull_cost_logic_ms": 100,
                    },
                ],
            },
            {
                "service": "orders",
                "routes": orders_routes,
                "instances": [
                    {
                        "instance_id": "orders-a",
                        "endpoint": "http://127.0.0.1:18103/report",
                        "version": 9,
                        "digest": orders_digest,
                        "version_label": "v3",
                        "pull_cost_logic_ms": 100,
                    },
                    {
                        "instance_id": "orders-b",
                        "endpoint": "http://127.0.0.1:18104/report",
                        "version": 9,
                        "digest": orders_digest,
                        "version_label": "v4",
                        "pull_cost_logic_ms": 100,
                    },
                ],
            },
        ],
    }
    policy = {
        "schema_version": 1,
        "policy_id": "reconcile-policy-20261123",
        "tolerance_window_ms": 5000,
        "strict_digest": True,
        "max_concurrency": 2,
        "round_budget_ms": 1000,
        "dedup_cooldown_ms": 3000,
        "instance_timeout_ms": 600,
        "max_report_bytes": 65536,
    }

    checkout_match = {
        "service": "checkout",
        "instance_id": "checkout-a",
        "version": 14,
        "digest": checkout_digest,
        "logical_clock_ms": 9900,
        "healthy": True,
        "rules": copy.deepcopy(checkout_routes),
    }
    checkout_stale = {
        **copy.deepcopy(checkout_match),
        "instance_id": "checkout-b",
        "version": 12,
        "logical_clock_ms": 9700,
        "rules": [
            rule
            for rule in copy.deepcopy(checkout_routes)
            if rule["rule_id"] != "checkout-pay-canary"
        ],
    }
    orders_weighted = {
        "service": "orders",
        "instance_id": "orders-a",
        "version": 9,
        "digest": "sha256:tampered-orders-a",
        "logical_clock_ms": 9900,
        "healthy": True,
        "rules": [
            route("orders-submit-canary", "/submit", "v4", 30),
            route("orders-submit-stable", "/submit", "v3", 70),
        ],
    }
    orders_partial = {
        "service": "orders",
        "instance_id": "orders-b",
        "version": 9,
        "logical_clock_ms": 9900,
        "healthy": True,
        "rules": [
            *copy.deepcopy(orders_routes),
            route("orders-submit-undeclared", "/submit", "v9", 0),
        ],
    }
    reports = {
        "schema_version": 1,
        "seed": SEED,
        "reports": {
            "checkout-a": checkout_match,
            "checkout-b": checkout_stale,
            "orders-a": orders_weighted,
            "orders-b": orders_partial,
        },
    }
    anomalies = {
        "seed": SEED,
        "default": "match",
        "rounds": {
            "1000": {
                "checkout-b": {"mode": "stale", "version_offset": -2},
                "orders-a": {"mode": "tampered_digest"},
                "orders-b": {
                    "mode": "missing_metadata",
                    "fields": ["digest", "healthy"],
                },
            },
            "2000": {
                "checkout-b": {"mode": "stale", "version_offset": -2},
                "orders-a": {"mode": "tampered_digest"},
                "orders-b": {
                    "mode": "missing_metadata",
                    "fields": ["digest", "healthy"],
                },
            },
            "6000": {
                "checkout-b": {
                    "mode": "stale",
                    "version_offset": -2,
                    "remove_rules": ["checkout-pay-canary"],
                },
                "orders-a": {
                    "mode": "tampered_digest",
                    "rule_overrides": {
                        "orders-submit-stable": {"weight": 75},
                        "orders-submit-canary": {"weight": 25},
                    },
                },
                "orders-b": {
                    "mode": "missing_metadata",
                    "fields": ["digest", "healthy"],
                    "add_rules": [
                        {
                            "rule_id": "orders-submit-undeclared",
                            "path": "/submit",
                            "version_label": "v9",
                            "weight": 0,
                        }
                    ],
                },
            },
            "9000": {},
        },
    }
    write_json(ROOT / "samples" / "expected.json", expected)
    write_json(ROOT / "samples" / "policy.json", policy)
    write_json(ROOT / "samples" / "reports.json", reports)
    write_json(ROOT / "samples" / "anomalies.json", anomalies)
    print(
        json.dumps(
            {
                "ok": True,
                "seed": SEED,
                "files": [
                    "samples/expected.json",
                    "samples/policy.json",
                    "samples/reports.json",
                    "samples/anomalies.json",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

