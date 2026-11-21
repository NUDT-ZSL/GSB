#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def small_manifest() -> dict[str, Any]:
    objects = []
    for index in range(24):
        if index % 2 == 0:
            declared = "hot"
        else:
            declared = "cold"
        if index % 3 == 0:
            last_access = 980
            access_count = 0
            heat_weight = 1
        elif index % 3 == 1:
            last_access = 1000
            access_count = 20
            heat_weight = 100
        else:
            last_access = 1000
            access_count = 2
            heat_weight = 1
        objects.append(
            {
                "key": f"small/object-{index:03d}",
                "size_bytes": 120 + ((index * 73) % 640),
                "last_access_clock": last_access,
                "access_count": access_count,
                "heat_weight": heat_weight,
                "declared_tier": declared,
                "migratable": index != 23,
            }
        )
    return {"bucket": "small-a", "objects": objects}


def adaptive_manifest() -> dict[str, Any]:
    objects = []
    for index in range(1200):
        declared = "hot" if index % 2 == 0 else "cold"
        if index % 5 == 0:
            last_access = 5000
            access_count = 0
            heat_weight = 1
        else:
            last_access = 10000
            access_count = 5
            heat_weight = 100
        objects.append(
            {
                "key": f"adaptive/object-{index:05d}",
                "size_bytes": 80 + ((index * 37) % 420),
                "last_access_clock": last_access,
                "access_count": access_count,
                "heat_weight": heat_weight,
                "declared_tier": declared,
                "migratable": index % 97 != 0,
            }
        )
    return {"bucket": "adaptive-a", "objects": objects}


def initial_usage(manifest: dict[str, Any]) -> dict[str, int]:
    usage = {"hot": 0, "cold": 0}
    for obj in manifest["objects"]:
        usage[obj["declared_tier"]] += obj["size_bytes"]
    return usage


def small_config(manifest: dict[str, Any]) -> dict[str, Any]:
    usage = initial_usage(manifest)
    return {
        "hot_capacity_bytes": usage["hot"] + 700,
        "cold_capacity_bytes": usage["cold"] + 5000,
        "round_byte_budget": 1000,
        "migration_concurrency": 2,
        "hot_threshold": "5",
        "clock_tolerance": 100,
        "mode": "auto",
        "backoff": {
            "base_delay_clocks": 50,
            "max_delay_clocks": 200,
            "multiplier": 2,
            "max_consecutive_failures": 3,
        },
        "sampling": {
            "ratio": "0.25",
            "confidence_z": "1.96",
            "min_sample_per_stratum": 2,
            "random_seed": 7,
        },
        "adaptive": {
            "exact_max_objects": 200,
            "exact_max_candidates": 200,
            "max_estimated_error_ratio": "0.35",
            "exact_scan_unit_cost": "1",
            "exact_candidate_unit_cost": "0.05",
            "sampled_index_unit_cost": "0.1",
            "sampled_object_unit_cost": "0.2",
        },
    }


def adaptive_config(manifest: dict[str, Any]) -> dict[str, Any]:
    usage = initial_usage(manifest)
    return {
        "hot_capacity_bytes": usage["hot"] + 250000,
        "cold_capacity_bytes": usage["cold"] + 250000,
        "round_byte_budget": 60000,
        "migration_concurrency": 4,
        "hot_threshold": "5",
        "clock_tolerance": 50,
        "mode": "auto",
        "backoff": {
            "base_delay_clocks": 20,
            "max_delay_clocks": 160,
            "multiplier": 2,
            "max_consecutive_failures": 4,
        },
        "sampling": {
            "ratio": "0.15",
            "confidence_z": "1.96",
            "min_sample_per_stratum": 16,
            "random_seed": 11,
        },
        "adaptive": {
            "exact_max_objects": 300,
            "exact_max_candidates": 200,
            "max_estimated_error_ratio": "0.6",
            "exact_scan_unit_cost": "1",
            "exact_candidate_unit_cost": "0.05",
            "sampled_index_unit_cost": "0.1",
            "sampled_object_unit_cost": "0.2",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    output = Path(args.out)

    small = small_manifest()
    write_json(output / "small" / "manifest.json", small)
    write_json(output / "small" / "config.json", small_config(small))
    write_json(
        output / "small" / "clocks.json",
        {
            "bucket": "small-a",
            "steps": [
                {"step": 1, "clock": 1000},
                {"step": 2, "clock": 1100},
                {"step": 3, "clock": 1040},
                {"step": 4, "clock": 1200},
                {"step": 5, "clock": 1260},
                {"step": 6, "clock": 900},
            ],
        },
    )

    adaptive = adaptive_manifest()
    write_json(output / "adaptive" / "manifest.json", adaptive)
    write_json(output / "adaptive" / "config.json", adaptive_config(adaptive))
    write_json(
        output / "adaptive" / "clocks.json",
        {
            "bucket": "adaptive-a",
            "steps": [
                {"step": 1, "clock": 10000},
                {"step": 2, "clock": 10100},
                {"step": 3, "clock": 10200},
            ],
        },
    )


if __name__ == "__main__":
    main()
