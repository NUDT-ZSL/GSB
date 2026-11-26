from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .util import canonical_hash, sha256_file, write_json


SEED = 4620261127
LOGICAL_CLOCK_MS = 1795737600000


LAYER_BASE = {
    "L0": (112, 176),
    "L1": (104, 168),
    "L2": (96, 160),
    "L3": (88, 152),
}


def _requests(rng: random.Random) -> Dict[str, Any]:
    requests: List[Dict[str, Any]] = []
    token_counts = [18, 22, 25, 20, 27, 16]
    priorities = [20, 10, 30, 10, 40, 20]
    for index, tokens in enumerate(token_counts, start=1):
        seq_id = f"seq-{index:04d}"
        layers = []
        for layer_index, layer_id in enumerate(["L0", "L1", "L2", "L3"]):
            low, high = LAYER_BASE[layer_id]
            entry_bytes = [rng.randint(low, high) for _ in range(tokens)]
            layers.append(
                {
                    "layer_id": layer_id,
                    "cold_order": 3 - layer_index,
                    "entry_bytes": entry_bytes,
                }
            )
        requests.append(
            {
                "seq_id": seq_id,
                "priority": priorities[index - 1],
                "max_len": tokens + rng.randint(12, 28),
                "generated_tokens": tokens,
                "layers": layers,
            }
        )
    return {
        "schema": "kvledger.requests/v1",
        "seed": SEED,
        "logical_clock_ms": LOGICAL_CLOCK_MS,
        "requests": requests,
    }


def _memory_config() -> Dict[str, Any]:
    return {
        "schema": "kvledger.memory_config/v1",
        "total_memory_bytes": 32768,
        "layer_capacity_ratios": {
            "L0": "0.16",
            "L1": "0.20",
            "L2": "0.24",
            "L3": "0.32",
        },
        "eviction_budget_bytes_per_round": 4096,
        "max_concurrent_evictions": 3,
        "fixed_point": {"scale": 48, "rounding": "floor"},
        "nvme_file": "nvme/kv_swap.bin",
        "lock_file": "nvme/kv_swap.lock",
    }


def _v1_policy(clock_ms: int) -> Dict[str, Any]:
    return {
        "format": "v1",
        "schema": "kvledger.policy.v1",
        "policy_set_id": "cold-tier-baseline-v1",
        "generation": 1,
        "effective_after_seconds": 0,
        "effective_seconds": {
            "layer:L0": 120,
        },
        "thresholds": {
            "layer:L0": "0.94",
            "layer:L1": "0.91",
            "layer:L2": "0.88",
            "layer:L3": "0.84",
        },
        "cold_order": {
            "layer:L0": 3,
            "layer:L1": 2,
            "layer:L2": 1,
            "layer:L3": 0,
        },
        "declared_base_clock_ms": clock_ms,
    }


def _v2_policies(clock_ms: int, *, next_snapshot: bool = False) -> List[Dict[str, Any]]:
    if not next_snapshot:
        return [
            {
                "policy_key": "layer:L0",
                "threshold": "0.89",
                "cold_order": 3,
                "absolute_clock_ms": clock_ms + 90_000,
                "generation": 3,
            },
            {
                "policy_key": "layer:L1",
                "threshold": "0.86",
                "cold_order": 2,
                "absolute_clock_ms": clock_ms,
                "generation": 2,
            },
            {
                "policy_key": "layer:L2",
                "threshold": "0.83",
                "cold_order": 1,
                "absolute_clock_ms": clock_ms + 45_000,
                "generation": 2,
            },
            {
                "policy_key": "layer:L3",
                "threshold": "0.79",
                "cold_order": 0,
                "absolute_clock_ms": clock_ms + 30_000,
                "generation": 3,
            },
        ]
    return [
        {
            "policy_key": "layer:L0",
            "threshold": "0.875",
            "cold_order": 3,
            "absolute_clock_ms": clock_ms + 180_000,
            "generation": 4,
        },
        {
            "policy_key": "layer:L1",
            "threshold": "0.845",
            "cold_order": 2,
            "absolute_clock_ms": clock_ms + 180_000,
            "generation": 3,
        },
        {
            "policy_key": "layer:L2",
            "threshold": "0.815",
            "cold_order": 1,
            "absolute_clock_ms": clock_ms + 180_000,
            "generation": 3,
        },
        {
            "policy_key": "layer:L3",
            "threshold": "0.765",
            "cold_order": 0,
            "absolute_clock_ms": clock_ms + 180_000,
            "generation": 4,
        },
    ]


def _incremental_policies(clock_ms: int) -> List[Dict[str, Any]]:
    return [
        {
            "policy_key": "layer:L3",
            "threshold": "0.748",
            "cold_order": 0,
            "absolute_clock_ms": clock_ms + 240_000,
            "generation": 5,
        }
    ]


def _tombstones(clock_ms: int, *, incremental: bool = False) -> List[Dict[str, Any]]:
    key = "layer:DEPRECATED_INCREMENTAL" if incremental else "layer:LEGACY_V0"
    generation = 2 if incremental else 1
    return [
        {
            "policy_key": key,
            "generation": generation,
            "absolute_clock_ms": clock_ms - 120_000,
        }
    ]


def _write_v2_package(
    root: Path,
    *,
    clock_ms: int,
    manifest_version: int,
    snapshot_type: str,
    next_snapshot: bool,
    incremental: bool = False,
) -> None:
    v2_root = root / "v2"
    shard_dir = v2_root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    policies = _incremental_policies(clock_ms) if incremental else _v2_policies(clock_ms, next_snapshot=next_snapshot)
    tombstones = _tombstones(clock_ms, incremental=incremental)
    shard = {
        "schema": "kvledger.policy.shard/v2",
        "shard_id": "shard-000",
        "policies": policies,
        "tombstones": tombstones,
    }
    shard_path = shard_dir / "shard-000.json"
    write_json(shard_path, shard)
    shard_hash = sha256_file(shard_path)

    key_digests: Dict[str, str] = {
        item["policy_key"]: canonical_hash(
            {
                "policy_key": item["policy_key"],
                "threshold": item["threshold"],
                "cold_order": item["cold_order"],
                "generation": item["generation"],
            }
        )
        for item in policies
    }
    for tombstone in tombstones:
        key_digests[tombstone["policy_key"]] = canonical_hash(
            {
                "policy_key": tombstone["policy_key"],
                "generation": tombstone["generation"],
                "tombstone": True,
            }
        )
    manifest = {
        "format": "v2-package",
        "schema": "kvledger.policy.manifest/v2",
        "manifest_version": manifest_version,
        "snapshot_type": snapshot_type,
        "base_manifest_version": None if snapshot_type == "full" else manifest_version - 1,
        "generation_floor": min(int(item["generation"]) for item in policies),
        "declared_clock_ms": clock_ms,
        "shards": [{"path": "shards/shard-000.json", "sha256": shard_hash}],
        "key_digests": key_digests,
    }
    write_json(v2_root / "manifest.json", manifest)

    db_path = v2_root / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE shard_metadata (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                generation INTEGER NOT NULL,
                row_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE policy_metadata (
                policy_key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                absolute_clock_ms INTEGER NOT NULL,
                digest TEXT NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(policy_key, generation)
            )
            """
        )
        conn.execute(
            "INSERT INTO shard_metadata VALUES (?, ?, ?, ?)",
            ("shards/shard-000.json", shard_hash, manifest["generation_floor"], len(policies)),
        )
        for item in policies:
            key = item["policy_key"]
            current_gen = int(item["generation"])
            digest = key_digests[key]
            conn.execute(
                "INSERT INTO policy_metadata VALUES (?, ?, ?, ?, 0)",
                (key, current_gen, int(item["absolute_clock_ms"]), digest),
            )
            prior_gen = current_gen - 1
            prior_policy = dict(item)
            prior_policy["generation"] = prior_gen
            prior_policy["threshold"] = "0.800"
            prior_digest = canonical_hash(
                {
                    "policy_key": key,
                    "threshold": prior_policy["threshold"],
                    "cold_order": item["cold_order"],
                    "generation": prior_gen,
                }
            )
            conn.execute(
                "INSERT INTO policy_metadata VALUES (?, ?, ?, ?, 0)",
                (key, prior_gen, int(clock_ms - 60_000), prior_digest),
            )
        for tombstone in tombstones:
            conn.execute(
                "INSERT INTO policy_metadata VALUES (?, ?, ?, ?, 1)",
                (
                    tombstone["policy_key"],
                    int(tombstone["generation"]),
                    int(tombstone["absolute_clock_ms"]),
                    key_digests[tombstone["policy_key"]],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def generate_samples(out_dir: str | Path = "samples") -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    requests_payload = _requests(rng)
    config_payload = _memory_config()
    write_json(out / "requests.json", requests_payload)
    write_json(out / "memory_config.json", config_payload)

    initial = out / "policy_package"
    initial.mkdir(exist_ok=True)
    write_json(initial / "v1" / "policy_v1.json", _v1_policy(LOGICAL_CLOCK_MS))
    _write_v2_package(
        initial,
        clock_ms=LOGICAL_CLOCK_MS,
        manifest_version=1,
        snapshot_type="full",
        next_snapshot=False,
    )
    write_json(
        initial / "package.json",
        {
            "bundle": "kvledger.policy_bundle/v1",
            "manifest_version": 1,
            "includes": ["v1/policy_v1.json", "v2/manifest.json"],
        },
    )

    nxt = out / "policy_package_next"
    nxt.mkdir(exist_ok=True)
    _write_v2_package(
        nxt,
        clock_ms=LOGICAL_CLOCK_MS + 600_000,
        manifest_version=2,
        snapshot_type="full",
        next_snapshot=True,
    )
    inc = out / "policy_package_incremental"
    inc.mkdir(exist_ok=True)
    _write_v2_package(
        inc,
        clock_ms=LOGICAL_CLOCK_MS + 1_200_000,
        manifest_version=3,
        snapshot_type="incremental",
        next_snapshot=False,
        incremental=True,
    )
    return {
        "requests": str(out / "requests.json"),
        "config": str(out / "memory_config.json"),
        "policy_package": str(initial),
        "next_policy_package": str(nxt / "v2"),
        "incremental_policy_package": str(inc / "v2"),
    }


def main() -> None:
    paths = generate_samples()
    for key, value in paths.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
