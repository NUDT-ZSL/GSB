#!/usr/bin/env python3
"""Generate the fixed local demo workload and its truth digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shardpipe.determinism import shard_manifest  # noqa: E402
from shardpipe.util import pretty_json  # noqa: E402
from shardpipe.workload import parse_workload  # noqa: E402


def build_document(seed: int) -> dict:
    return {
        "global_seed": seed,
        "datasets": [
            {
                "name": "alpha",
                "sample_count": 96,
                "feature_dim": 8,
                "sample_bytes": 64,
                "cacheable": True,
                "shard_size": 16,
                "version": "v1",
                "generation_params": {
                    "distribution": "deterministic-sha256",
                    "salt": "alpha-local-v1",
                },
            },
            {
                "name": "beta",
                "sample_count": 64,
                "feature_dim": 16,
                "sample_bytes": 96,
                "cacheable": True,
                "shard_size": 16,
                "version": "v1",
                "generation_params": {
                    "distribution": "deterministic-sha256",
                    "salt": "beta-local-v1",
                },
            },
        ],
        "cache": {
            "generation": 3,
            "version": "demo-cache-v3",
            "entries": [
                {
                    "dataset": "alpha",
                    "shard_index": 0,
                    "generation": 3,
                    "bytes": 1024,
                    "last_used_clock": 10,
                },
                {
                    "dataset": "alpha",
                    "shard_index": 1,
                    "generation": 3,
                    "bytes": 1024,
                    "last_used_clock": 11,
                },
                {
                    "dataset": "beta",
                    "shard_index": 0,
                    "generation": 3,
                    "bytes": 1536,
                    "last_used_clock": 12,
                },
            ],
        },
        "jobs": [
            {
                "name": "train-alpha",
                "dataset": "alpha",
                "batch_size": 8,
                "epochs": 1,
                "data_parallelism": 2,
                "prefetch_depth": 2,
                "allow_shard_reuse": True,
            },
            {
                "name": "train-beta",
                "dataset": "beta",
                "batch_size": 8,
                "epochs": 1,
                "data_parallelism": 2,
                "prefetch_depth": 1,
                "allow_shard_reuse": False,
            },
        ],
        "scheduling": {
            "logical_time_budget_ms": 64,
            "per_dataset_prefetch_limit": 2,
            "global_memory_budget_mib": 8,
            "cache_capacity_entries": 20,
            "tolerance_ms": 5,
            "logical_clock_step_ms": 2,
            "logical_clock_start_ms": 0,
            "generation_cost": {
                "base_ms": 1,
                "bytes_per_ms": 2048,
                "hit_ms": 0,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/sample.json")
    parser.add_argument("--seed", type=int, default=20261121)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    document = build_document(args.seed)
    workload = parse_workload(document)

    dataset_map = workload.dataset_map()
    for entry in workload.cache.entries:
        manifest = shard_manifest(
            global_seed=workload.global_seed,
            dataset=dataset_map[entry.dataset],
            dataset_fingerprint_value=entry.generation_fingerprint or "",
            shard_index=entry.shard_index,
        )
        for raw_entry in document["cache"]["entries"]:
            if (
                raw_entry["dataset"] == entry.dataset
                and raw_entry["shard_index"] == entry.shard_index
            ):
                raw_entry["digest"] = manifest["digest"]
                raw_entry["generation_fingerprint"] = (
                    entry.generation_fingerprint
                )

    final_workload = parse_workload(document)
    text = pretty_json(dict(final_workload.canonical)) + "\n"
    output.write_text(text, encoding="utf-8")
    file_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    summary = {
        "workload_digest": final_workload.digest,
        "input_sha256": file_digest,
        "global_seed": final_workload.global_seed,
        "cache_generation": final_workload.cache.generation,
        "datasets": [
            {
                "name": dataset.name,
                "shards": dataset.shard_count,
                "shard_bytes": dataset.shard_bytes,
                "generation_fingerprint": final_workload.canonical[
                    "_dataset_fingerprints"
                ][dataset.name],
            }
            for dataset in final_workload.datasets
        ],
        "jobs": [job.name for job in final_workload.jobs],
    }
    output.with_suffix(".summary.json").write_text(
        pretty_json(summary) + "\n", encoding="utf-8"
    )
    output.with_suffix(".sha256").write_text(
        f"{file_digest}  {output.name}\n", encoding="utf-8"
    )
    print(pretty_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
