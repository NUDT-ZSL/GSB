"""Generate deterministic local sample inputs for the pipeline.

Running this script (re)creates several self-contained scenario directories.
Every numeric/digest value is derived from :mod:`datapipe.sharding`, so the
generated cache view always agrees with the deterministic shard manifests.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from typing import Any, Dict, List

from datapipe.loader import load_scenario
from datapipe.model import GenParams
from datapipe.sharding import build_shard_manifest, cache_algebra


def _write(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def base_datasets() -> List[Dict[str, Any]]:
    return [
        {
            "name": "images",
            "total_samples": 12,
            "feature_dim": 8,
            "bytes_per_sample": 128,
            "shard_size": 4,
            "global_seed": 101,
            "gen_params": {"noise": 3, "content_version": 1},
            "max_concurrent_prefetch": 2,
        },
        {
            "name": "text",
            "total_samples": 8,
            "feature_dim": 5,
            "bytes_per_sample": 96,
            "shard_size": 4,
            "global_seed": 202,
            "gen_params": {"noise": 1, "content_version": 1},
            "max_concurrent_prefetch": 1,
        },
    ]


def _cache_view(datasets: List[Dict[str, Any]], only: Dict[str, List[int]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for raw in datasets:
        if raw["name"] not in only:
            continue
        spec = _to_spec(raw)
        for shard_index in only[raw["name"]]:
            manifest = build_shard_manifest(spec, shard_index)
            entries.append(
                {
                    "dataset": raw["name"],
                    "shard_index": shard_index,
                    "byte_size": manifest.byte_size,
                    "algebra": cache_algebra(spec),
                    "last_used_clock": 100 + shard_index * 10,
                    "digest": manifest.digest,
                }
            )
    return entries


def _to_spec(raw: Dict[str, Any]):
    from datapipe.model import DatasetSpec

    return DatasetSpec(
        name=raw["name"],
        total_samples=raw["total_samples"],
        feature_dim=raw["feature_dim"],
        bytes_per_sample=raw["bytes_per_sample"],
        shard_size=raw["shard_size"],
        global_seed=raw["global_seed"],
        gen_params=GenParams.from_obj(raw["gen_params"]),
        max_concurrent_prefetch=raw["max_concurrent_prefetch"],
    )


def clock_steps(count: int = 40, advance_ms: int = 20, sleep_ms: int = 0) -> List[Dict[str, Any]]:
    return [
        {"advance_ms": advance_ms, "sleep_ms": sleep_ms, "rollback_tolerance_ms": 5}
        if index == 0
        else {"advance_ms": advance_ms, "sleep_ms": sleep_ms}
        for index in range(count)
    ]


def write_scenario(root: str, name: str, payload: Dict[str, Any]) -> str:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    _write(os.path.join(path, "datasets.json"), payload["datasets"])
    _write(os.path.join(path, "cache_view.json"), payload["cache_view"])
    _write(os.path.join(path, "job.json"), payload["job"])
    _write(os.path.join(path, "clock_steps.json"), payload["clock_steps"])
    _write(os.path.join(path, "injections.json"), payload["injections"])
    # Validate immediately so generated samples are guaranteed well-formed.
    load_scenario(path)
    return path


def build_all(root: str) -> List[str]:
    datasets = base_datasets()
    job = {
        "name": "nightly-job",
        "batch_size": 5,
        "data_parallel": 2,
        "prefetch_depth": 2,
        "epochs": 2,
        "memory_budget_mib": 1,
        # Byte-level override keeps tests deterministic without real MiB use;
        # 2604 covers every baseline shard so assembly can run to completion.
        "memory_budget_bytes": 2700,
        "round_time_budget_ms": 1000,
        "datasets": ["images", "text"],
    }
    base = {
        "datasets": datasets,
        "cache_view": _cache_view(datasets, {"images": [0], "text": [0]}),
        "job": job,
        "clock_steps": clock_steps(),
        "injections": [],
    }
    paths = [write_scenario(root, "baseline", base)]

    # Invalidation scenario: bump noise on images only; its cached shard must be
    # invalidated while the text shard keeps hitting.
    invalidated_datasets = copy.deepcopy(datasets)
    invalidated_datasets[0]["gen_params"]["noise"] = 9
    invalidation = {
        "datasets": invalidated_datasets,
        "cache_view": _cache_view(datasets, {"images": [0], "text": [0]}),
        "job": job,
        "clock_steps": clock_steps(),
        "injections": [],
    }
    paths.append(write_scenario(root, "invalidation", invalidation))

    # Fault scenarios each inject one controlled, locatable failure.
    fault_template = {
        "datasets": copy.deepcopy(datasets),
        "cache_view": [],
        "job": copy.deepcopy(job),
        "clock_steps": clock_steps(),
    }
    fault_scenarios = {
        "fault_gen_digest": [
            {"stage": "generate", "kind": "gen_digest_mismatch", "target": "images#s000000", "fire_after": 0}
        ],
        "fault_cache_rollback": [
            {"stage": "cache", "kind": "cache_algebra_rollback", "target": "images#s000000"}
        ],
        "fault_cache_write": [
            {"stage": "cache", "kind": "cache_write_failure", "target": "images#s000000"}
        ],
        "fault_prefetch": [
            {"stage": "prefetch", "kind": "prefetch_failure", "target": "images#s000000"}
        ],
        "fault_timeout": [
            {
                "stage": "prefetch",
                "kind": "prefetch_timeout",
                "target": "images#s000000",
                "options": {"round_time_budget_ms": 1},
            }
        ],
        "fault_clock_rollback": [
            {"stage": "prefetch", "kind": "clock_rollback", "target": "*"}
        ],
        "fault_batch_conflict": [
            {"stage": "assemble", "kind": "batch_digest_conflict", "target": "epoch0#b0"}
        ],
        "fault_crash_generate": [
            {"stage": "generate", "kind": "crash", "target": "images#s000000", "fire_after": 0}
        ],
        "budget_exhausted": [],
    }
    for name, injections in fault_scenarios.items():
        payload = copy.deepcopy(fault_template)
        payload["injections"] = injections
        if name == "fault_timeout":
            payload["job"] = copy.deepcopy(job)
            payload["job"]["round_time_budget_ms"] = 1
            # Pure clock-driven timeout: no injected fault, just clock steps
            # whose cumulative advance exceeds the 1ms round budget, with a
            # real sleep so timeout needs both clock advance and real waiting.
            payload["injections"] = []
            payload["clock_steps"] = [
                {"advance_ms": 20, "sleep_ms": 20, "rollback_tolerance_ms": 5}
                for _ in range(40)
            ]
        if name == "budget_exhausted":
            payload["job"] = copy.deepcopy(job)
            # Only the first two shards fit; the rest must be abandoned with
            # an explicit skipped_budget reason and never started.
            payload["job"]["memory_budget_bytes"] = 1000
        if name == "fault_clock_rollback":
            # First step advances, second step rolls the clock back beyond the
            # 5ms tolerance; the rollback is detected from the step table.
            payload["clock_steps"] = [
                {"advance_ms": 100, "sleep_ms": 0, "rollback_tolerance_ms": 5},
                {"advance_ms": -90, "sleep_ms": 0},
            ]
            payload["injections"] = []
        paths.append(write_scenario(root, name, payload))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="generate deterministic sample scenarios")
    parser.add_argument("--root", default=os.path.join(os.path.dirname(__file__), "samples"))
    args = parser.parse_args()
    paths = build_all(args.root)
    print("generated scenarios:")
    for path in paths:
        print(" -", path)


if __name__ == "__main__":
    main()
