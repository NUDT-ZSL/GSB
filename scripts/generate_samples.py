#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


SEED = 20261117


def digest(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "seed": SEED,
        "model": {
            "layers": 3,
            "hidden_size": 8,
            "vocab_size": 96,
            "parameter_tensors": [
                {"name": "token_embedding.weight", "shape": ["vocab_size", "hidden_size"]},
                {"name": "layer.{layer}.q_proj.weight", "shape": ["hidden_size", "hidden_size"]},
                {"name": "layer.{layer}.k_proj.weight", "shape": ["hidden_size", "hidden_size"]},
                {"name": "layer.{layer}.v_proj.weight", "shape": ["hidden_size", "hidden_size"]},
                {"name": "layer.{layer}.o_proj.weight", "shape": ["hidden_size", "hidden_size"]},
                {"name": "layer.{layer}.mlp_up.weight", "shape": ["hidden_size", "ffn_size"]},
                {"name": "layer.{layer}.mlp_down.weight", "shape": ["ffn_size", "hidden_size"]},
            ],
        },
        "data_parallel": {
            "workers": 4,
            "shard_size_limit_bytes": 2048,
            "write_concurrency": 4,
            "snapshot_commit_interval_rounds": 1,
            "retention_generations": 3,
            "total_rounds": 12,
        },
        "storage": {"global_budget_bytes": 36000},
        "training": {"fixed_point_scale": 1000000},
    }


def build_policy() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "rules": [
            {
                "id": "low-occupancy-late",
                "when": {
                    "occupancy": {"min": 0.0, "max": 0.35},
                    "remaining_rounds": {"min": 0, "max": 4},
                },
                "actions": {
                    "retention_generations": 2,
                    "merge_mode": "merge_small",
                    "merge_small_threshold_bytes": 2500,
                    "max_merged_bytes": 4096,
                    "write_priority": "shard_size_desc",
                    "budget_action": "evict_then_write",
                },
            },
            {
                "id": "low-occupancy-early",
                "when": {
                    "occupancy": {"min": 0.0, "max": 0.35},
                    "remaining_rounds": {"min": 5, "max": 1000},
                },
                "actions": {
                    "retention_generations": 3,
                    "merge_mode": "none",
                    "merge_small_threshold_bytes": 0,
                    "max_merged_bytes": 0,
                    "write_priority": "tensor_size_desc",
                    "budget_action": "evict_then_write",
                },
            },
            {
                "id": "medium-occupancy",
                "when": {
                    "occupancy": {"min": 0.35, "max": 0.75},
                    "remaining_rounds": {"min": 0, "max": 1000},
                },
                "actions": {
                    "retention_generations": 3,
                    "merge_mode": "merge_small",
                    "merge_small_threshold_bytes": 2500,
                    "max_merged_bytes": 4096,
                    "write_priority": "shard_size_desc",
                    "budget_action": "evict_then_write",
                },
            },
            {
                "id": "high-occupancy-late",
                "when": {
                    "occupancy": {"min": 0.75, "max": 1.0},
                    "remaining_rounds": {"min": 0, "max": 4},
                },
                "actions": {
                    "retention_generations": 2,
                    "merge_mode": "merge_small",
                    "merge_small_threshold_bytes": 2500,
                    "max_merged_bytes": 4096,
                    "write_priority": "shard_size_desc",
                    "budget_action": "evict_then_write",
                },
            },
            {
                "id": "high-occupancy",
                "when": {
                    "occupancy": {"min": 0.75, "max": 1.0},
                    "remaining_rounds": {"min": 5, "max": 1000},
                },
                "actions": {
                    "retention_generations": 2,
                    "merge_mode": "merge_small",
                    "merge_small_threshold_bytes": 2500,
                    "max_merged_bytes": 4096,
                    "write_priority": "worker_then_chunk",
                    "budget_action": "evict_then_write",
                },
            },
        ],
    }


def build_data_shards() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(16):
        shard_id = f"data-{index:03d}"
        sample_count = 32 + ((SEED // (index + 7)) % 65)
        rows.append(
            {
                "shard_id": shard_id,
                "sample_count": sample_count,
                "feature_digest": digest(f"feature:{SEED}:{shard_id}:{sample_count}"),
                "label_digest": digest(f"label:{SEED}:{shard_id}:{sample_count}"),
            }
        )
    return rows


def build_injections() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "injections": [
            {
                "id": "INJ-BUDGET-001",
                "type": "budget_occupancy_out_of_range",
                "round": 4,
                "value": 1.25,
            },
            {
                "id": "INJ-RETENTION-001",
                "type": "illegal_retention",
                "round": 5,
                "value": 9,
            },
            {
                "id": "INJ-OVERSIZE-001",
                "type": "oversized_shard",
                "round": 6,
                "target_shard_index": 2,
            },
            {
                "id": "INJ-MERGE-001",
                "type": "merge_exception",
                "round": 7,
                "target_physical_index": 1,
            },
            {
                "id": "INJ-CONFLICT-001",
                "type": "fingerprint_conflict",
                "round": 8,
                "target_physical_index": 0,
            },
            {
                "id": "INJ-GENERATE-001",
                "type": "generate_exception",
                "round": 9,
                "target_shard_index": 0,
            },
            {
                "id": "INJ-WRITE-001",
                "type": "write_exception",
                "round": 10,
                "target_physical_index": 1,
            },
        ],
    }


def write_yaml(path: Path, value: Any) -> None:
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local sample inputs")
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    config = build_config()
    policy = build_policy()
    rows = build_data_shards()
    injections = build_injections()

    write_yaml(out / "train_config.yaml", config)
    write_yaml(out / "scheduler_policy.yaml", policy)
    write_json(out / "injections.json", injections)
    with (out / "data_shards.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    files = [
        "train_config.yaml",
        "scheduler_policy.yaml",
        "data_shards.jsonl",
        "injections.json",
    ]
    manifest = {
        "seed": SEED,
        "files": {
            name: {
                "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest(),
                "bytes": (out / name).stat().st_size,
            }
            for name in files
        },
    }
    write_json(out / "sample_manifest.json", manifest)
    print(f"generated {len(rows)} data shards in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
