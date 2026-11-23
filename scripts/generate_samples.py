"""Generate all deterministic local JSON samples and the v2 metadata database.

Run with ``python scripts/generate_samples.py [out_dir]``.  No network, clock
or randomness dependency: repeated runs produce byte identical artifacts.
"""

import hashlib
import json
import os
import sqlite3
import sys


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")


SERVICES = [
    {
        "name": "gateway",
        "replicas": 4,
        "cpu_per_replica": 2,
        "mem_per_replica": 4,
        "upstream": [],
        "failure_domain": "fd-a",
        "priority": 1,
    },
    {
        "name": "auth",
        "replicas": 3,
        "cpu_per_replica": 1,
        "mem_per_replica": 2,
        "upstream": ["gateway"],
        "failure_domain": "fd-a",
        "priority": 2,
    },
    {
        "name": "profile",
        "replicas": 2,
        "cpu_per_replica": 2,
        "mem_per_replica": 3,
        "upstream": ["auth"],
        "failure_domain": "fd-b",
        "priority": 3,
    },
    {
        "name": "billing",
        "replicas": 2,
        "cpu_per_replica": 3,
        "mem_per_replica": 4,
        "upstream": ["auth"],
        "failure_domain": "fd-b",
        "priority": 2,
    },
    {
        "name": "report",
        "replicas": 1,
        "cpu_per_replica": 2,
        "mem_per_replica": 2,
        "upstream": ["billing"],
        "failure_domain": "fd-c",
        "priority": 4,
    },
]

NODES = [
    {"name": "node-a1", "cpu_total": 16, "mem_total": 24,
     "cpu_free": 16, "mem_free": 24, "topology_domain": "fd-a"},
    {"name": "node-a2", "cpu_total": 12, "mem_total": 16,
     "cpu_free": 12, "mem_free": 16, "topology_domain": "fd-a"},
    {"name": "node-b1", "cpu_total": 16, "mem_total": 20,
     "cpu_free": 16, "mem_free": 20, "topology_domain": "fd-b"},
    {"name": "node-b2", "cpu_total": 8, "mem_total": 10,
     "cpu_free": 8, "mem_free": 10, "topology_domain": "fd-b"},
    {"name": "node-c1", "cpu_total": 10, "mem_total": 12,
     "cpu_free": 10, "mem_free": 12, "topology_domain": "fd-c"},
]

# v1 flat policies: relative seconds, strictly increasing ratios ending at 1
V1_POLICIES = [
    {
        "service": "gateway",
        "gears": [
            {"relative_seconds": 0, "ratio": "0.5"},
            {"relative_seconds": 30, "ratio": "1"},
            {"relative_seconds": 60, "ratio": "1.5"},
            {"relative_seconds": 90, "ratio": "2"},
        ],
    },
    {
        "service": "auth",
        "gears": [
            {"relative_seconds": 0, "ratio": "0.6"},
            {"relative_seconds": 20, "ratio": "1"},
            {"relative_seconds": 40, "ratio": "1.5"},
        ],
    },
    {
        "service": "report",
        "gears": [
            {"relative_seconds": 0, "ratio": "0.5"},
            {"relative_seconds": 45, "ratio": "1"},
        ],
    },
]

# v2 shard A: profile + billing, newer generation and absolute milliseconds
V2_SHARD_A = {
    "shard_id": "shard-001",
    "generation": 2,
    "declared_clock_ms": 5000,
    "services": ["profile", "billing"],
    "policies": [
        {
            "service": "profile",
            "generation": 2,
            "gears": [
                {"absolute_ms": 1000, "ratio": "0.5"},
                {"absolute_ms": 4000, "ratio": "1"},
                {"absolute_ms": 4500, "ratio": "2"},
            ],
        },
        {
            "service": "billing",
            "generation": 2,
            "gears": [
                {"absolute_ms": 2000, "ratio": "0.5"},
                {"absolute_ms": 5000, "ratio": "1"},
                {"absolute_ms": 5500, "ratio": "1.5"},
            ],
        },
    ],
    "tombstones": [],
}

# v2 shard B: report tombstoned at generation 3
V2_SHARD_B = {
    "shard_id": "shard-002",
    "generation": 3,
    "declared_clock_ms": 9000,
    "services": ["report"],
    "policies": [
        {
            "service": "report",
            "generation": 3,
            "gears": [
                {"absolute_ms": 3000, "ratio": "0.5"},
                {"absolute_ms": 8000, "ratio": "1"},
            ],
        }
    ],
    "tombstones": [
        {"service": "report", "generation": 3, "reason": "decommissioned"}
    ],
}

LOAD_TRACE = [
    {"service": "gateway", "at_ms": 0, "load": "0.4"},
    {"service": "gateway", "at_ms": 2000, "load": "0.9"},
    {"service": "auth", "at_ms": 0, "load": "0.7"},
    {"service": "auth", "at_ms": 3000, "load": "0.95"},
    {"service": "profile", "at_ms": 0, "load": "0.55"},
    {"service": "profile", "at_ms": 4500, "load": "0.9"},
    {"service": "billing", "at_ms": 0, "load": "0.45"},
    {"service": "billing", "at_ms": 5000, "load": "0.99"},
    {"service": "report", "at_ms": 0, "load": "0.5"},
    {"service": "report", "at_ms": 6000, "load": "0.8"},
]

CONFIG = {
    "round_budget_ms": 750,
    "action_cost_ms": 150,
    "fd_concurrency": 2,
    "tolerance_ticks": 5,
    "fixed_scale": 1000,
    "clock_step_ms": 1000,
    "cost_per_replica": "1.5",
    "round_budget_cost": "1000",
}


def _hash(payload):
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def generate(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "services.json"), SERVICES)
    write_json(os.path.join(out_dir, "nodes.json"), NODES)
    write_json(os.path.join(out_dir, "v1_policies.json"), V1_POLICIES)

    hash_a = _hash(V2_SHARD_A)
    hash_b = _hash(V2_SHARD_B)
    shard_a = dict(V2_SHARD_A, shard_hash=hash_a)
    shard_b = dict(V2_SHARD_B, shard_hash=hash_b)
    write_json(os.path.join(out_dir, "v2_shard_001.json"), shard_a)
    write_json(os.path.join(out_dir, "v2_shard_002.json"), shard_b)

    meta_db = os.path.join(out_dir, "v2_meta.db")
    if os.path.exists(meta_db):
        os.remove(meta_db)
    connection = sqlite3.connect(meta_db)
    connection.execute(
        "CREATE TABLE shard_meta ("
        "shard_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, "
        "shard_hash TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO shard_meta VALUES(?, ?, ?)",
        [
            ("shard-001", 2, hash_a),
            ("shard-002", 3, hash_b),
        ],
    )
    connection.commit()
    connection.close()

    write_json(os.path.join(out_dir, "load_trace.json"), LOAD_TRACE)
    write_json(os.path.join(out_dir, "config.json"), CONFIG)

    manifest = {
        "version": "2.3.0",
        "generation_floor": 2,
        "shards": [
            {"shard_id": "shard-001", "generation": 2, "shard_hash": hash_a},
            {"shard_id": "shard-002", "generation": 3, "shard_hash": hash_b},
        ],
        "injected_clock_ms": 5000,
    }
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    return manifest


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "samples/data"
    result = generate(target)
    print(json.dumps({"generated": target, "version": result["version"]}, indent=2))
