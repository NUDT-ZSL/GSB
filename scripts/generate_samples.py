#!/usr/bin/env python3
"""Generate deterministic, network-free sample data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def task(
    name: str,
    artifact_name: str,
    size_bytes: int,
    *,
    depends_on: list[str] | None = None,
    hit_count: int = 0,
    pinned: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "depends_on": depends_on or [],
        "artifact_name": artifact_name,
        "size_bytes": size_bytes,
        "written_at_ms": 1000,
        "last_hit_at_ms": 1000,
        "hit_count": hit_count,
        "pinned": pinned,
    }


def promotion_sample() -> None:
    path = SAMPLES / "promotion"
    tasks = [
        task("build", "build.tar", 700),
        task("test", "test.tar", 600, depends_on=["build"]),
        task("package", "package.tar", 500, depends_on=["test"]),
        task("docs", "docs.tar", 300, depends_on=["package"], pinned=True),
    ]
    pipelines = {
        "schema_version": 1,
        "pipelines": [
            {
                "name": "release",
                "tasks": ["build", "test", "package", "docs"],
                "runs": [
                    {"run_seq": 1, "at_ms": 1000},
                    {"run_seq": 2, "at_ms": 1100},
                    {"run_seq": 3, "at_ms": 1200},
                    {"run_seq": 4, "at_ms": 1300},
                ],
            },
            {
                "name": "keepalive",
                "tasks": ["docs"],
                "runs": [{"run_seq": 1, "at_ms": 5000}],
            },
            {
                "name": "build_only",
                "tasks": ["build"],
                "runs": [{"run_seq": 1, "at_ms": 9000}],
            },
        ],
    }
    config = {
        "version": 1,
        "tier_capacity_bytes": {"cold": 100000, "warm": 100000, "hot": 100000},
        "promotion_min_hits": 2,
        "demotion_idle_ms": 1000,
        "eviction_batch_size": 2,
        "reclaim_byte_budget": 100000,
        "fixed_point_scale": 1000,
        "comparison_tolerance": 500,
        "clock_rollback_tolerance_ms": 0,
        "logical_clock_step_ms": 100,
    }
    write_json(path / "tasks.json", {"schema_version": 1, "tasks": tasks})
    write_json(path / "pipelines.json", pipelines)
    write_json(path / "config.json", config)


def eviction_sample() -> None:
    path = SAMPLES / "eviction"
    tasks = [
        task("alpha", "alpha.bin", 600, hit_count=0),
        task("beta", "beta.bin", 600, depends_on=["alpha"], hit_count=1),
        task("gamma", "gamma.bin", 600, depends_on=["beta"], hit_count=2),
        task("delta", "delta.bin", 600, depends_on=["gamma"], hit_count=3),
        task("epsilon", "epsilon.bin", 600, depends_on=["delta"], hit_count=4),
        task("keep", "keep.bin", 200, pinned=True),
    ]
    pipelines = {
        "schema_version": 1,
        "pipelines": [
            {
                "name": "release",
                "tasks": ["alpha", "beta", "gamma", "delta", "epsilon", "keep"],
                "runs": [
                    {"run_seq": 1, "at_ms": 1000},
                    {"run_seq": 2, "at_ms": 1100},
                ],
            }
        ],
    }
    config = {
        "version": 1,
        "tier_capacity_bytes": {"cold": 2000, "warm": 2000, "hot": 2000},
        "promotion_min_hits": 10,
        "demotion_idle_ms": 1000,
        "eviction_batch_size": 2,
        "reclaim_byte_budget": 1300,
        "fixed_point_scale": 1000,
        "comparison_tolerance": 150000,
        "clock_rollback_tolerance_ms": 0,
        "logical_clock_step_ms": 100,
    }
    write_json(path / "tasks.json", {"schema_version": 1, "tasks": tasks})
    write_json(path / "pipelines.json", pipelines)
    write_json(path / "config.json", config)


def main() -> None:
    promotion_sample()
    eviction_sample()
    print(f"generated samples under {SAMPLES}")


if __name__ == "__main__":
    main()
