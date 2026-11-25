from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .models import canonical_digest, sha256_file

DEFAULT_SEED = 436_202_611


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_samples(out_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    out = Path(out_dir)
    (out / "policy_v2" / "shards").mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    tenant_specs = [
        ("alpha", 42, 0.50, 1.00, 2.00),
        ("beta", 36, 0.62, 1.00, 1.25),
        ("gamma", 30, 0.68, 0.95, 0.75),
    ]
    tenants_payload = {
        "schema": "cache-ledger/tenants/v1",
        "seed": seed,
        "tenants": [
            {
                "name": name,
                "max_blocks": max_blocks,
                "soft_quota_ratio": soft,
                "hard_quota_ratio": hard,
                "eviction_priority_weight": weight,
            }
            for name, max_blocks, soft, hard, weight in tenant_specs
        ],
    }
    write_json(out / "tenants.json", tenants_payload)

    entries: list[dict[str, Any]] = []
    key_by_seq: dict[int, str] = {}
    seq = 0
    layer_cycle = ["L1", "L2", "L1", "L3", "L2", "L1", "L3", "L2", "L3", "L1", "L2", "L3"]
    for tenant_index, (tenant, _max_blocks, _soft, _hard, _weight) in enumerate(tenant_specs):
        for local_index in range(12):
            seq += 1
            key = f"cache-{seq:04d}"
            key_by_seq[seq] = key
            size_choice = rng.choices([1, 2, 2, 2, 3, 3, 4], weights=[2, 4, 4, 3, 3, 2, 1], k=1)[0]
            if tenant == "gamma" and local_index >= 9:
                size_choice = min(size_choice, 2)
            last_access_ms = rng.randint(18_000, 41_000)
            access_count = rng.choices([1, 3, 6, 9, 15, 20, 27], weights=[3, 4, 4, 3, 2, 2, 1], k=1)[0]
            declared_layer = layer_cycle[(local_index + tenant_index * 2) % len(layer_cycle)]
            entries.append({
                "seq": seq,
                "key": key,
                "tenant": tenant,
                "layer": declared_layer,
                "block_size": size_choice,
                "last_access_ms": last_access_ms,
                "access_count": access_count,
                "digest": canonical_digest(seq, key, size_choice),
            })

    entries_path = out / "entries.jsonl"
    entries_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in entries),
        encoding="utf-8",
    )

    capacity = {
        "schema": "cache-ledger/capacity/v1",
        "layers": {
            "L1": {"max_blocks": 20},
            "L2": {"max_blocks": 52},
            "L3": {"max_blocks": 100},
        },
        "block_bytes": 4096,
        "total_byte_budget": 105 * 4096,
        "rebalance_threshold": 75,
        "threshold_unit": "percent",
        "cooldown_window_ms": 5000,
        "merge_clock_ms": 18_000,
        "default_clock_ms": 42_000,
        "clock_tolerance_ms": 250,
        "migration_base_cost_ms": 2,
        "migration_block_cost_ms": 1,
        "fixed_tick_ms": 64,
        "byte_pressure_pct": 82,
        "byte_target_pct": 72,
        "hot_window_ms": 12_000,
        "hot_access_count": 18,
        "warm_access_count": 7,
        "rebalance_time_budget_ms": 10,
    }
    write_json(out / "capacity.json", capacity)

    v1_mappings: dict[str, str] = {}
    for seq_number in range(1, 25):
        layer = ("L1", "L2", "L3")[seq_number % 3]
        if seq_number in (4, 10, 16, 22):
            layer = "L1"
        v1_mappings[key_by_seq[seq_number]] = layer
    v1_payload = {
        "format": "v1",
        "schema": "cache-ledger/policy-v1/v1",
        "source_version": "policy-v1-fixed-seed",
        "generation": 1,
        "relative_effective_seconds": 3,
        "time_base": "injected_logical_clock",
        "flat_mapping": "key_to_layer",
        "mappings": v1_mappings,
    }
    write_json(out / "policy_v1.json", v1_payload)

    policy_patch = {
        "schema": "cache-ledger/policy-patch/v1",
        "description": "Localized layer change used to demonstrate incremental closure and reuse.",
        "generation": 4,
        "policies": {
            key_by_seq[1]: {
                "source": "v2-patch",
                "source_version": "policy-v2-patch-local",
                "generation": 4,
                "layer": "L3",
                "tombstone": False,
                "invalid": False,
                "effective_at_ms": 43_000,
            }
        },
    }
    write_json(out / "policy_update_example.json", policy_patch)

    v2_rows: list[dict[str, Any]] = []
    for seq_number in range(13, 37):
        key = key_by_seq[seq_number]
        shard_index = (seq_number - 13) // 8
        shard = f"shard_{shard_index:03d}"
        generation = 2 if seq_number <= 20 else 3
        layer = ("L1", "L2", "L3")[(seq_number + 1) % 3]
        effective_at_ms = 24_000 + (seq_number - 13) * 719
        tombstone = seq_number in (31, 32)
        invalid = seq_number in (33, 34)
        if tombstone:
            layer = None
            effective_at_ms = 39_000 + seq_number
        elif invalid:
            layer = "L1"
            effective_at_ms = 39_400 + seq_number
        elif seq_number == 14:
            effective_at_ms = 20_000
        elif seq_number == 15:
            effective_at_ms = 21_000
            generation = 1
        v2_rows.append({
            "key": key,
            "layer": layer,
            "effective_at_ms": effective_at_ms,
            "generation": generation,
            "tombstone": tombstone,
            "invalid": invalid,
            "shard": shard,
        })

    shard_payloads: dict[str, dict[str, Any]] = {}
    for row in v2_rows:
        shard = row["shard"]
        shard_payloads.setdefault(shard, {"shard": shard, "policies": {}})
        shard_payloads[shard]["policies"][row["key"]] = {
            "layer": row["layer"],
            "effective_at_ms": row["effective_at_ms"],
            "generation": row["generation"],
            "tombstone": row["tombstone"],
            "invalid": row["invalid"],
        }

    shard_meta: list[dict[str, Any]] = []
    for shard_index, shard_name in enumerate(sorted(shard_payloads)):
        path = out / "policy_v2" / "shards" / f"{shard_name}.json"
        write_json(path, shard_payloads[shard_name])
        shard_generation = 2 if shard_index == 0 else 3
        shard_meta.append({
            "shard": shard_name,
            "path": f"{shard_name}.json",
            "shard_index": shard_index,
            "generation": shard_generation,
            "sha256": sha256_file(path),
        })

    meta_db = out / "policy_v2" / "policy_v2_meta.sqlite"
    if meta_db.exists():
        meta_db.unlink()
    with sqlite3.connect(meta_db) as conn:
        conn.execute("""
            CREATE TABLE snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                format TEXT NOT NULL,
                source_version TEXT NOT NULL,
                generation INTEGER NOT NULL,
                created_logical_ms INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE shards (
                shard TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                shard_index INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE policies (
                key TEXT PRIMARY KEY,
                shard TEXT NOT NULL,
                layer TEXT,
                effective_at_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                invalid INTEGER NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO snapshot VALUES (1, 'v2-sharded-sqlite', 'policy-v2-fixed-seed', 3, 17000)"
        )
        conn.executemany(
            "INSERT INTO shards(shard,path,shard_index,generation,sha256) VALUES (:shard,:path,:shard_index,:generation,:sha256)",
            shard_meta,
        )
        conn.executemany(
            """INSERT INTO policies(key,shard,layer,effective_at_ms,generation,tombstone,invalid)
               VALUES (:key,:shard,:layer,:effective_at_ms,:generation,:tombstone,:invalid)""",
            [{**row, "tombstone": int(row["tombstone"]), "invalid": int(row["invalid"])} for row in v2_rows],
        )
        conn.commit()

    manifest = {
        "schema": "cache-ledger-sample-manifest/v1",
        "seed": seed,
        "files": [
            "tenants.json",
            "entries.jsonl",
            "capacity.json",
            "policy_v1.json",
            "policy_update_example.json",
            "policy_v2/policy_v2_meta.sqlite",
            *[f"policy_v2/shards/{item['shard']}.json" for item in shard_meta],
        ],
        "entry_count": len(entries),
        "tenant_count": len(tenant_specs),
    }
    write_json(out / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic cache ledger samples")
    parser.add_argument("--out", default="samples", help="output sample directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate_samples(args.out, args.seed)
    print(json.dumps({"ok": True, "manifest": manifest}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
