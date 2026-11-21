#!/usr/bin/env python3
"""Generate the fixed, wall-clock-free sample set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def build_layers() -> list[dict[str, Any]]:
    return [
        {
            "name": "base-runtime",
            "digest": digest("base-runtime:v3"),
            "version": 3,
            "default_init_ms": 9,
            "tombstone": False,
            "env": {"PYTHONUNBUFFERED": "1"},
            "depends_on": [],
            "referenced_by": ["classify-text", "report-csv", "resize-image"],
        },
        {
            "name": "image-libs",
            "digest": digest("image-libs:v2"),
            "version": 2,
            "default_init_ms": 21,
            "tombstone": False,
            "env": {"OMP_NUM_THREADS": "1"},
            "depends_on": ["base-runtime"],
            "referenced_by": ["resize-image"],
        },
        {
            "name": "nlp-model",
            "digest": digest("nlp-model:v5"),
            "version": 5,
            "default_init_ms": 40,
            "tombstone": False,
            "env": {"TOKENIZER_CACHE": "local"},
            "depends_on": ["base-runtime"],
            "referenced_by": ["classify-text"],
        },
        {
            "name": "legacy-layer",
            "digest": digest("legacy-layer:v1"),
            "version": 1,
            "default_init_ms": 10,
            "tombstone": False,
            "env": {"LEGACY_MODE": "1"},
            "depends_on": [],
            "referenced_by": ["legacy-handler"],
        },
    ]


def build_functions() -> dict[str, Any]:
    return {
        "global_concurrency_budget": 4,
        "functions": [
            {
                "name": "resize-image",
                "runtime": "python3.11",
                "layer_refs": ["base-runtime", "image-libs"],
                "layer_params": {
                    "base-runtime": {
                        "env": {"PYTHONHASHSEED": "0"},
                        "init_logical_ms": 9,
                    },
                    "image-libs": {
                        "env": {"OMP_NUM_THREADS": "1", "PILLOW_SIMD": "1"},
                        "init_logical_ms": 21,
                    },
                },
                "memory_mib": 512,
                "max_concurrency": 2,
                "warm_pool_target": 2,
                "warm_ttl_ms": 2000,
                "init_base_ms": 15,
            },
            {
                "name": "classify-text",
                "runtime": "python3.11",
                "layer_refs": ["base-runtime", "nlp-model"],
                "layer_params": {
                    "base-runtime": {
                        "env": {"PYTHONHASHSEED": "0"},
                        "init_logical_ms": 10,
                    },
                    "nlp-model": {
                        "env": {"TOKENIZER_CACHE": "local"},
                        "init_logical_ms": 40,
                    },
                },
                "memory_mib": 1024,
                "max_concurrency": 1,
                "warm_pool_target": 1,
                "warm_ttl_ms": 2500,
                "init_base_ms": 25,
            },
            {
                "name": "report-csv",
                "runtime": "python3.11",
                "layer_refs": ["base-runtime"],
                "layer_params": {
                    "base-runtime": {
                        "env": {"PYTHONHASHSEED": "0"},
                        "init_logical_ms": 8,
                    }
                },
                "memory_mib": 256,
                "max_concurrency": 3,
                "warm_pool_target": 0,
                "warm_ttl_ms": 1500,
                "init_base_ms": 12,
            },
            {
                "name": "legacy-handler",
                "runtime": "python3.11",
                "layer_refs": ["legacy-layer"],
                "layer_params": {
                    "legacy-layer": {
                        "env": {"LEGACY_MODE": "1"},
                        "init_logical_ms": 10,
                    }
                },
                "memory_mib": 256,
                "max_concurrency": 1,
                "warm_pool_target": 1,
                "warm_ttl_ms": 3000,
                "init_base_ms": 5,
            },
        ],
    }


def build_v1() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "clock": {
            "base_ms": 0,
            "scale_num": 5,
            "scale_den": 4,
            "rounding": "half_up",
        },
        "observations": [
            {
                "function": "resize-image",
                "relative_init_ms": 38,
                "warm_hit": True,
                "warm_generation": 1,
                "observed_clock_ms": 10,
            },
            {
                "function": "classify-text",
                "relative_init_ms": 49,
                "warm_hit": True,
                "warm_generation": 1,
                "observed_clock_ms": 12,
            },
            {
                "function": "report-csv",
                "relative_init_ms": 27,
                "warm_hit": False,
                "warm_generation": 1,
                "observed_clock_ms": 14,
            },
            {
                "function": "legacy-handler",
                "relative_init_ms": 18,
                "warm_hit": True,
                "warm_generation": 1,
                "observed_clock_ms": 9,
            },
        ],
    }


def build_v2(samples_dir: Path) -> None:
    v2_dir = samples_dir / "snapshots" / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    shards = [
        (
            "v2_shard_00.json",
            "s0",
            {
                "function": "resize-image",
                "absolute_init_ms": 47,
                "warm_generation": 2,
                "warm_hit": True,
                "observed_clock_ms": 20,
            },
        ),
        (
            "v2_shard_01.json",
            "s1",
            {
                "function": "classify-text",
                "absolute_init_ms": 61,
                "warm_generation": 2,
                "warm_hit": True,
                "observed_clock_ms": 22,
            },
        ),
        (
            "v2_shard_02.json",
            "s2",
            {
                "function": "legacy-handler",
                "absolute_init_ms": 23,
                "warm_generation": 2,
                "warm_hit": True,
                "observed_clock_ms": 18,
            },
        ),
    ]
    for filename, shard_id, observation in shards:
        write_json(
            v2_dir / filename,
            {
                "schema_version": 2,
                "shard_id": shard_id,
                "observations": [observation],
            },
        )
    metadata_rows = [
        ("resize-image", 20, 2, 7, "s0"),
        ("classify-text", 22, 2, 4, "s1"),
        ("legacy-handler", 18, 2, 2, "s2"),
    ]
    metadata_path = v2_dir / "v2_meta.sqlite"
    metadata_path.unlink(missing_ok=True)
    connection = sqlite3.connect(metadata_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            """
            CREATE TABLE snapshot_metadata (
                function TEXT PRIMARY KEY,
                logical_clock_ms INTEGER NOT NULL,
                warm_generation INTEGER NOT NULL,
                hit_watermark INTEGER NOT NULL,
                shard_id TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO snapshot_metadata VALUES (?, ?, ?, ?, ?)",
            metadata_rows,
        )
        connection.commit()
    finally:
        connection.close()
    write_json(
        v2_dir / "index.json",
        {
            "schema_version": 2,
            "metadata_db": "v2_meta.sqlite",
            "shards": [filename for filename, _, _ in shards],
        },
    )


def build_patches(samples_dir: Path) -> None:
    write_json(
        samples_dir / "patches" / "layer-update.json",
        {
            "patch_id": "layer-update-001",
            "base_config_version": 1,
            "declared_clock_ms": 1000,
            "operations": [
                {
                    "op": "modify",
                    "layer": "image-libs",
                    "version": 3,
                    "digest": digest("image-libs:v3"),
                    "default_init_ms": 20,
                    "depends_on": ["base-runtime"],
                    "env": {"OMP_NUM_THREADS": "2"},
                    "tombstone": False,
                },
                {
                    "op": "tombstone",
                    "layer": "legacy-layer",
                    "version": 2,
                    "digest": digest("legacy-layer:v1"),
                    "default_init_ms": 10,
                    "depends_on": [],
                    "env": {"LEGACY_MODE": "1"},
                    "tombstone": True,
                },
                {
                    "op": "add",
                    "layer": "cold-cache",
                    "version": 1,
                    "digest": digest("cold-cache:v1"),
                    "default_init_ms": 7,
                    "depends_on": [],
                    "env": {"CACHE_MODE": "deterministic"},
                    "tombstone": False,
                },
            ],
        },
    )
    write_json(
        samples_dir / "patches" / "runtime-update.json",
        {
            "patch_id": "runtime-update-001",
            "base_config_version": 1,
            "declared_clock_ms": 2005,
            "operations": [
                {
                    "op": "modify",
                    "layer": "base-runtime",
                    "version": 4,
                    "digest": digest("base-runtime:v4"),
                    "default_init_ms": 11,
                    "depends_on": [],
                    "env": {"PYTHONUNBUFFERED": "1"},
                    "tombstone": False,
                }
            ],
        },
    )


def build_traces(samples_dir: Path) -> None:
    write_json(
        samples_dir / "traces" / "before.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 1,
                    "function": "resize-image",
                    "arrival_clock_ms": 100,
                    "occupancy": 1,
                    "duration_ms": 100,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 2,
                    "function": "resize-image",
                    "arrival_clock_ms": 105,
                    "occupancy": 1,
                    "duration_ms": 100,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 3,
                    "function": "resize-image",
                    "arrival_clock_ms": 110,
                    "occupancy": 1,
                    "duration_ms": 50,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 4,
                    "function": "classify-text",
                    "arrival_clock_ms": 115,
                    "occupancy": 1,
                    "duration_ms": 80,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 5,
                    "function": "report-csv",
                    "arrival_clock_ms": 120,
                    "occupancy": 2,
                    "duration_ms": 60,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 6,
                    "function": "report-csv",
                    "arrival_clock_ms": 300,
                    "occupancy": 2,
                    "duration_ms": 60,
                    "wait_budget_ms": 100,
                },
                {
                    "seq": 7,
                    "function": "resize-image",
                    "arrival_clock_ms": 400,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 8,
                    "function": "classify-text",
                    "arrival_clock_ms": 500,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 9,
                    "function": "legacy-handler",
                    "arrival_clock_ms": 700,
                    "occupancy": 1,
                    "duration_ms": 30,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 10,
                    "function": "report-csv",
                    "arrival_clock_ms": 800,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 100,
                },
            ],
        },
    )
    write_json(
        samples_dir / "traces" / "after.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 11,
                    "function": "classify-text",
                    "arrival_clock_ms": 1100,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 12,
                    "function": "report-csv",
                    "arrival_clock_ms": 1110,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 13,
                    "function": "resize-image",
                    "arrival_clock_ms": 1120,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 100,
                },
                {
                    "seq": 14,
                    "function": "legacy-handler",
                    "arrival_clock_ms": 1130,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 0,
                },
                {
                    "seq": 15,
                    "function": "resize-image",
                    "arrival_clock_ms": 1200,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 100,
                },
            ],
        },
    )
    write_json(
        samples_dir / "traces" / "crash-report.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 1,
                    "function": "report-csv",
                    "arrival_clock_ms": 2000,
                    "occupancy": 1,
                    "duration_ms": 60,
                    "wait_budget_ms": 1000,
                }
            ],
        },
    )
    write_json(
        samples_dir / "traces" / "crash-report-after.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 2,
                    "function": "report-csv",
                    "arrival_clock_ms": 2100,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 100,
                }
            ],
        },
    )
    write_json(
        samples_dir / "traces" / "init-deadline.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 1,
                    "function": "report-csv",
                    "arrival_clock_ms": 2000,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 2,
                }
            ],
        },
    )
    write_json(
        samples_dir / "traces" / "active-cancel.json",
        {
            "schema_version": 1,
            "calls": [
                {
                    "seq": 16,
                    "function": "report-csv",
                    "arrival_clock_ms": 850,
                    "occupancy": 1,
                    "duration_ms": 40,
                    "wait_budget_ms": 100,
                    "cancel_after_ms": 10,
                }
            ],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "samples"),
    )
    args = parser.parse_args()
    samples_dir = Path(args.out).resolve()
    samples_dir.mkdir(parents=True, exist_ok=True)
    write_json(samples_dir / "functions.json", build_functions())
    write_json(samples_dir / "layers.json", {"layers": build_layers()})
    write_json(
        samples_dir / "snapshots" / "v1.json",
        build_v1(),
    )
    build_v2(samples_dir)
    build_patches(samples_dir)
    build_traces(samples_dir)
    write_json(
        samples_dir / "sample_manifest.json",
        {
            "sample_id": "local-serverless-coldstart-v1",
            "clock_semantics": "all times are injected integer logical milliseconds",
            "wall_clock_used": False,
            "files": {
                "functions": "functions.json",
                "layers": "layers.json",
                "snapshot_v1": "snapshots/v1.json",
                "snapshot_v2_index": "snapshots/v2/index.json",
                "trace_before": "traces/before.json",
                "trace_after": "traces/after.json",
                "trace_crash": "traces/crash-report.json",
                "trace_crash_after": "traces/crash-report-after.json",
                "trace_init_deadline": "traces/init-deadline.json",
                "trace_active_cancel": "traces/active-cancel.json",
                "patch_layers": "patches/layer-update.json",
                "patch_runtime_crash": "patches/runtime-update.json",
            },
        },
    )
    print(json.dumps({"ok": True, "samples_dir": str(samples_dir)}, sort_keys=True))


if __name__ == "__main__":
    main()
