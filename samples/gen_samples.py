"""Deterministic local sample generator (fixed data, repeatable runs).

Produces, under the given output directory:
  v1/recovery_v1.json          single-file v1 recovery config
  v2/manifest.json             v2 package manifest with shard hashes
  v2/shards/shard-0.json       v2 shard files
  v2/shards/shard-1.json
  v2/meta.sqlite3              v2 SQLite metadata (monotonic generations)
All files live inside the workspace; stale temporaries are cleaned first.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

V1_CONFIG = {
    "format": "v1",
    "services": [
        {
            "name": "svc-alpha",
            "fault_domain": "fd-east",
            "rollback_seconds": 30,
            "actions": [
                {"id": "alpha-stop", "at_tick": 0, "op": "stop"},
                {"id": "alpha-switch", "at_tick": 5, "op": "switch"},
                {"id": "alpha-start", "at_tick": 9, "op": "start"},
            ],
        },
        {
            "name": "svc-beta",
            "fault_domain": "fd-east",
            "rollback_seconds": 45,
            "actions": [
                {"id": "beta-stop", "at_tick": 2, "op": "stop"},
                {"id": "beta-switch", "at_tick": 7, "op": "switch"},
            ],
        },
    ],
    "action_map": {
        "svc-alpha": ["alpha-stop", "alpha-switch", "alpha-start"],
        "svc-beta": ["beta-stop", "beta-switch"],
    },
}

V2_SHARDS = {
    "shards/shard-0.json": {
        "services": [
            {
                "name": "svc-beta",
                "effective_ms": 900000,
                "generation": 2,
                "priority": "P1",
                "upstream": [],
                "tombstone": False,
            },
            {
                "name": "svc-gamma",
                "effective_ms": 900500,
                "generation": 2,
                "priority": "P2",
                "upstream": ["svc-beta"],
                "tombstone": False,
            },
        ]
    },
    "shards/shard-1.json": {
        "services": [
            {
                "name": "svc-delta",
                "effective_ms": 901000,
                "generation": 3,
                "priority": "P0",
                "upstream": ["svc-beta", "svc-gamma"],
                "tombstone": False,
            },
            {
                "name": "svc-legacy",
                "effective_ms": 1000,
                "generation": 1,
                "priority": "P3",
                "upstream": [],
                "tombstone": True,
            },
        ]
    },
}

META_ROWS = [
    ("svc-beta", 1, 800000),
    ("svc-beta", 2, 900000),
    ("svc-gamma", 2, 900500),
    ("svc-delta", 2, 899000),
    ("svc-delta", 3, 901000),
    ("svc-legacy", 1, 1000),
]


def generate(out_dir) -> dict:
    base = Path(out_dir)
    if base.exists():
        shutil.rmtree(base)
    v1_dir = base / "v1"
    v2_dir = base / "v2"
    (v2_dir / "shards").mkdir(parents=True)
    v1_dir.mkdir(parents=True)

    v1_path = v1_dir / "recovery_v1.json"
    v1_path.write_text(json.dumps(V1_CONFIG, indent=2, sort_keys=True),
                       encoding="utf-8")

    shard_entries = []
    for rel, payload in V2_SHARDS.items():
        blob = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        (v2_dir / rel).write_bytes(blob)
        shard_entries.append({
            "path": rel,
            "sha256": hashlib.sha256(blob).hexdigest(),
        })

    meta_path = v2_dir / "meta.sqlite3"
    conn = sqlite3.connect(str(meta_path))
    try:
        conn.execute(
            "CREATE TABLE service_meta ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            " service TEXT NOT NULL,"
            " generation INTEGER NOT NULL,"
            " effective_ms INTEGER NOT NULL)")
        conn.executemany(
            "INSERT INTO service_meta (service, generation, effective_ms)"
            " VALUES (?, ?, ?)", META_ROWS)
        conn.commit()
    finally:
        conn.close()

    manifest = {
        "format": "v2",
        "manifest_version": 2,
        "generation_floor": 2,
        "meta_db": "meta.sqlite3",
        "shards": shard_entries,
    }
    (v2_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {"v1": str(v1_path), "v2": str(v2_dir)}


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "samples/out"
    print(json.dumps(generate(out), indent=2))
