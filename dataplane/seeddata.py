from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .hashing import canonical_digest, canonical_json, sha256_bytes
from .tensors import block_bounds, generate_distribution_array, array_digest


FIXED_SEED = 20261127


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_json(row))
            handle.write(b"\n")


def _tensor_descriptor(
    case_id: str,
    block_index: int,
    role: str,
    rows: int,
    dim: int,
    dtype: str,
    seed: int,
) -> Dict[str, Any]:
    if role == "feature":
        distribution = {
            "kind": "normal",
            "seed": seed,
            "mean": round(((seed % 7) - 3) * 0.03, 4),
            "std": round(0.75 + (seed % 5) * 0.08, 4),
        }
        name = f"feature_b{block_index:03d}"
    else:
        distribution = {
            "kind": "uniform",
            "seed": seed,
            "low": -0.8,
            "high": 0.8,
        }
        name = f"label_b{block_index:03d}"
    shape = [rows, dim]
    array = generate_distribution_array(shape, dtype, distribution)
    return {
        "case_id": case_id,
        "name": name,
        "role": role,
        "block_index": block_index,
        "shape": shape,
        "dtype": dtype,
        "distribution": distribution,
        "digest": array_digest(array),
    }


def build_sample_tree(data_dir: Path | str, seed: int = FIXED_SEED) -> Dict[str, Any]:
    data_dir = Path(data_dir)
    (data_dir / "samples").mkdir(parents=True, exist_ok=True)
    (data_dir / "objects").mkdir(parents=True, exist_ok=True)
    (data_dir / "locks").mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    data_sizes = [80, 128, 192, 256, 320, 96, 160, 224]
    feature_dims = [8, 12, 16, 20, 24, 10, 16, 28]
    label_dims = [1, 2, 1, 3, 2, 1, 2, 4]
    block_sizes = [32, 64, 64, 64, 80, 48, 40, 64]
    dtypes = ["float32", "float16", "float32", "float32", "float16", "float32", "float32", "float16"]

    tasks: List[Dict[str, Any]] = []
    samples: List[Dict[str, Any]] = []
    for index in range(8):
        case_id = f"case-{index + 1:03d}"
        data_size = data_sizes[index]
        feature_dim = feature_dims[index]
        label_dim = label_dims[index]
        dtype = dtypes[index]
        block_plan = {
            "strategy": "fixed_rows",
            "block_size": block_sizes[index],
            "carry_dependency": True,
        }
        task = {
            "case_id": case_id,
            "data_size": data_size,
            "feature_dim": feature_dim,
            "label_dim": label_dim,
            "dtype": dtype,
            "quant_scale": round(rng.uniform(0.04, 0.22), 6),
            "quant_zero_point": rng.randint(1, 16),
            "block_plan": block_plan,
            "revision": index + 1,
            "logical_clock_ms": 1_000 + index * 250,
        }
        task["case_digest"] = canonical_digest(
            {k: v for k, v in task.items() if k != "case_digest"}
        )
        tasks.append(task)

        descriptors: List[Dict[str, Any]] = []
        from .tensors import block_count

        count = block_count(data_size, block_plan)
        for block_index in range(count):
            start, end = block_bounds(data_size, block_plan, block_index)
            rows = end - start
            feature_seed = seed + index * 100_000 + block_index * 100
            label_seed = feature_seed + 7
            feature = _tensor_descriptor(
                case_id, block_index, "feature", rows, feature_dim, dtype, feature_seed
            )
            label = _tensor_descriptor(
                case_id, block_index, "label", rows, label_dim, "float32", label_seed
            )
            feature["row_range"] = [start, end]
            label["row_range"] = [start, end]
            if block_index > 0:
                feature["depends_on"] = [f"block:{block_index - 1}"]
                label["depends_on"] = [f"block:{block_index - 1}"]
            descriptors.extend([feature, label])
        sample = {
            "schema": "dp.sample_tensor_set/v1",
            "case_id": case_id,
            "case_digest": task["case_digest"],
            "tensors": descriptors,
        }
        samples.append(sample)
        _write_json(data_dir / "samples" / f"{case_id}.json", sample)

    _write_jsonl(data_dir / "tasks.jsonl", tasks)

    patches = _build_patches(tasks)
    _write_jsonl(data_dir / "patches.jsonl", patches)
    _write_json(data_dir / "patches.json", {"schema": "dp.patch_stream/v1", "patches": patches})

    v1 = _build_v1_config()
    _write_json(data_dir / "config_v1.json", v1)
    _write_v2_package(
        data_dir / "config_v2",
        package_id="cfg-2026-11-initial",
        version=2026112702,
        floor=3,
        effective_at_ms=1_795_000_000_000,
        shards=_initial_v2_shards(),
    )
    _write_v2_package(
        data_dir / "config_v2_incremental",
        package_id="cfg-2026-11-incremental",
        version=2026112703,
        floor=3,
        effective_at_ms=1_795_000_060_000,
        shards=_incremental_v2_shards(),
    )
    _write_v2_package(
        data_dir / "config_v2_full",
        package_id="cfg-2026-12-full",
        version=2026120101,
        floor=3,
        effective_at_ms=1_795_300_000_000,
        shards=_full_v2_shards(),
    )

    summary = {
        "seed": seed,
        "case_count": len(tasks),
        "patch_count": len(patches),
        "data_dir": str(data_dir),
    }
    _write_json(data_dir / "seed-summary.json", summary)
    return summary


def _build_patches(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {task["case_id"]: task for task in tasks}

    def range_for(case_id: str, start: int, end: Optional[int] = None) -> List[int]:
        from .tensors import block_count

        task = by_id[case_id]
        count = block_count(task["data_size"], task["block_plan"])
        if end is None:
            end = min(count, start + 1)
        return [start, min(count, end)]

    specs = [
        ("case-001", 0, 2, "weight_update", 1, 10_000, "metric_projection", 101),
        ("case-001", 0, 2, "weight_update", 2, 12_000, "metric_projection", 102),
        ("case-002", 1, 2, "activation_recompute", 1, 10_500, "activation_gelu", 201),
        ("case-003", 1, 3, "weight_update", 1, 11_000, "metric_projection", 301),
        ("case-003", 1, 3, "weight_update", 2, 13_000, "metric_projection", 302),
        ("case-004", 2, 4, "weight_update", 2, 14_000, "metric_projection", 401),
        ("case-005", 0, 1, "activation_recompute", 1, 15_000, "activation_silu", 501),
        ("case-006", 1, None, "activation_recompute", 1, 15_500, "activation_gelu", 601),
        ("case-008", 2, 4, "weight_update", 1, 16_000, "metric_projection", 801),
    ]
    patches: List[Dict[str, Any]] = []
    for index, (case_id, start, end, kind, generation, clock, operator, seed) in enumerate(specs, 1):
        block_range = range_for(case_id, start, end)
        content = {
            "operator": operator,
            "weight_scale_delta": round(((seed % 5) - 1) * 0.035, 4),
            "weight_seed": seed,
            "bias": round((seed % 7 - 3) * 0.015, 4),
            "activation_gain": round(1.0 + (seed % 4) * 0.08, 4),
            "reason": kind,
        }
        patches.append(
            {
                "patch_id": f"patch-{index:04d}",
                "case_id": case_id,
                "block_range": block_range,
                "patch_kind": kind,
                "operator_name": "metric_op",
                "patch_generation": generation,
                "logical_clock_ms": clock,
                "new_digest": canonical_digest(content),
                "patch_content": content,
                "refs": [],
            }
        )
    return patches


def _build_v1_config() -> Dict[str, Any]:
    return {
        "schema": "dp.config.v1",
        "manifest_version": 1,
        "effective_after_seconds": 0,
        "cache_policy": {
            "cache.reuse_digest": True,
            "cache.atomic_rename": True,
            "cache.default_concurrency": 2,
            "scheduler.default_budget_ms": 180,
            "quant.fixed_point_scale": 256,
            "inference.tolerance": 0.18,
            "cache.legacy_warmup": True,
        },
    }


def _initial_v2_shards() -> List[Dict[str, Any]]:
    return [
        {
            "shard_id": "policy-000-cache",
            "generation": 3,
            "policies": {
                "cache.affinity": "case",
                "cache.hashing": "sha256-canonical-json",
                "scheduler.stable_sort": True,
            },
            "tombstones": [],
        },
        {
            "shard_id": "policy-001-quant",
            "generation": 4,
            "policies": {
                "quant.fixed_point_scale": 512,
                "inference.tolerance": 0.12,
                "inference.error_bound": "conservative-l1",
            },
            "tombstones": ["cache.legacy_warmup"],
        },
        {
            "shard_id": "policy-002-diagnostics",
            "generation": 5,
            "policies": {"diagnostics.error_source_chain": True},
            "tombstones": [],
        },
    ]


def _incremental_v2_shards() -> List[Dict[str, Any]]:
    shards = _initial_v2_shards()
    shards.append(
        {
            "shard_id": "policy-003-trace",
            "generation": 6,
            "policies": {"diagnostics.round_remaining_work": True},
            "tombstones": [],
        }
    )
    return shards


def _full_v2_shards() -> List[Dict[str, Any]]:
    shards = _initial_v2_shards()
    shards[1]["policies"]["inference.tolerance"] = 0.08
    shards[1]["generation"] = 6
    shards[2]["generation"] = 7
    shards.append(
        {
            "shard_id": "policy-003-replacement",
            "generation": 8,
            "policies": {"cache.replacement_mode": "temporary-table-pointer"},
            "tombstones": [],
        }
    )
    return shards


def _write_v2_package(
    package_dir: Path,
    package_id: str,
    version: int,
    floor: int,
    effective_at_ms: int,
    shards: List[Dict[str, Any]],
) -> None:
    if package_dir.exists():
        for child in sorted(package_dir.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
    (package_dir / "shards").mkdir(parents=True, exist_ok=True)
    shard_refs = []
    metadata_rows = []
    generation_rows = set()
    for index, shard in enumerate(shards):
        rel_path = f"shards/policy_{index:03d}.json"
        shard_with_schema = {"schema": "dp.config.shard.v2", **shard}
        data = canonical_json(shard_with_schema) + b"\n"
        path = package_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = sha256_bytes(data)
        shard_refs.append(
            {"path": rel_path, "sha256": digest, "generation": shard["generation"]}
        )
        metadata_rows.append((rel_path, digest, shard["generation"]))
        generation_rows.add(shard["generation"])

    policy_keys = sorted({key for shard in shards for key in shard.get("policies", {})})
    tombstones = sorted({key for shard in shards for key in shard.get("tombstones", [])})
    key_digest = canonical_digest({"keys": policy_keys, "tombstones": tombstones})
    manifest = {
        "schema": "dp.config.package.v2",
        "package_id": package_id,
        "manifest_version": version,
        "effective_at_ms": effective_at_ms,
        "policy_generation_floor": floor,
        "metadata_db": "metadata.sqlite",
        "cache_key_set_digest": key_digest,
        "shards": shard_refs,
    }
    _write_json(package_dir / "manifest.json", manifest)

    metadata_path = package_dir / "metadata.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()
    conn = sqlite3.connect(str(metadata_path))
    try:
        conn.execute(
            "CREATE TABLE config_shard ("
            "path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, generation INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE policy_generation ("
            "generation INTEGER PRIMARY KEY, package_id TEXT NOT NULL, effective_at_ms INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE config_tombstone (policy_key TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO config_shard(path, sha256, generation) VALUES (?, ?, ?)",
            metadata_rows,
        )
        conn.executemany(
            "INSERT INTO policy_generation(generation, package_id, effective_at_ms) VALUES (?, ?, ?)",
            [(gen, package_id, effective_at_ms) for gen in sorted(generation_rows)],
        )
        tomb_rows = []
        for shard in shards:
            for key in shard.get("tombstones", []):
                tomb_rows.append((key, shard["generation"]))
        conn.executemany(
            "INSERT OR REPLACE INTO config_tombstone(policy_key, generation) VALUES (?, ?)",
            tomb_rows,
        )
        conn.commit()
    finally:
        conn.close()


def generate_fixed_samples(
    data_dir: Path | str = Path("data"), seed: int = FIXED_SEED
) -> Dict[str, Any]:
    return build_sample_tree(data_dir, seed)
