"""Fixed-seed local sample/task/config generator.

Usage:
    python3 sample_generator.py --input-dir input --out-dir out --seed 42
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tii_app.configs import write_v1_document, write_v2_snapshot
from tii_app.models import Task
from tii_app.util import atomic_write_json, atomic_write_jsonl, sha256_obj


DEFAULT_SEED = 42
V2_EFFECTIVE_AT_MS = 1500


def task_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "case_id": "case-001",
            "data_size": 24,
            "feature_dim": 6,
            "label_dim": 3,
            "dtype": "int8",
            "quant_scale": 0.05,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 8, "col_block": 2, "priority": 1},
            "revision": 1,
            "logical_clock_ms": 100,
        },
        {
            "case_id": "case-002",
            "data_size": 40,
            "feature_dim": 8,
            "label_dim": 4,
            "dtype": "uint8",
            "quant_scale": 0.04,
            "quant_zero_point": 128,
            "block_plan": {"row_block": 10, "col_block": 4, "priority": 2},
            "revision": 1,
            "logical_clock_ms": 600,
        },
        {
            "case_id": "case-003",
            "data_size": 32,
            "feature_dim": 5,
            "label_dim": 3,
            "dtype": "int8",
            "quant_scale": 0.06,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 8, "col_block": 5, "priority": 1},
            "revision": 1,
            "logical_clock_ms": 900,
        },
        {
            "case_id": "case-004",
            "data_size": 56,
            "feature_dim": 7,
            "label_dim": 2,
            "dtype": "uint8",
            "quant_scale": 0.03,
            "quant_zero_point": 127,
            "block_plan": {"row_block": 14, "col_block": 7, "priority": 3},
            "revision": 1,
            "logical_clock_ms": 1200,
        },
        {
            "case_id": "case-005",
            "data_size": 64,
            "feature_dim": 8,
            "label_dim": 4,
            "dtype": "int16",
            "quant_scale": 0.02,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 16, "col_block": 4, "priority": 4},
            "revision": 1,
            "logical_clock_ms": 1600,
        },
        {
            "case_id": "case-006",
            "data_size": 48,
            "feature_dim": 6,
            "label_dim": 3,
            "dtype": "int8",
            "quant_scale": 0.05,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 12, "col_block": 3, "priority": 2},
            "revision": 1,
            "logical_clock_ms": 1800,
        },
        {
            "case_id": "case-007",
            "data_size": 72,
            "feature_dim": 8,
            "label_dim": 5,
            "dtype": "uint8",
            "quant_scale": 0.04,
            "quant_zero_point": 128,
            "block_plan": {"row_block": 18, "col_block": 2, "priority": 5},
            "revision": 2,
            "logical_clock_ms": 2100,
        },
        {
            "case_id": "case-008",
            "data_size": 96,
            "feature_dim": 10,
            "label_dim": 4,
            "dtype": "int16",
            "quant_scale": 0.02,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 16, "col_block": 5, "priority": 6},
            "revision": 2,
            "logical_clock_ms": 2400,
        },
        {
            "case_id": "case-009",
            "data_size": 36,
            "feature_dim": 6,
            "label_dim": 3,
            "dtype": "int8",
            "quant_scale": 0.05,
            "quant_zero_point": 0,
            "block_plan": {"row_block": 12, "col_block": 6, "priority": 1},
            "revision": 1,
            "logical_clock_ms": 2600,
        },
        {
            "case_id": "case-010",
            "data_size": 80,
            "feature_dim": 9,
            "label_dim": 4,
            "dtype": "uint8",
            "quant_scale": 0.04,
            "quant_zero_point": 120,
            "block_plan": {"row_block": 20, "col_block": 3, "priority": 3},
            "revision": 1,
            "logical_clock_ms": 2900,
        },
    ]
    return rows


def generate(input_dir: str | Path, output_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = task_rows()
    duplicate = dict(rows[4])
    file_rows = rows + [duplicate]
    tasks = sorted(
        [Task.from_dict(row) for row in rows],
        key=lambda item: (item.logical_clock_ms, -item.priority, item.case_id),
    )
    checksum = sha256_obj([task.to_dict() for task in tasks])
    atomic_write_jsonl(input_dir / "tasks.jsonl", file_rows)
    atomic_write_json(
        input_dir / "tasks.meta.json",
        {
            "version": 1,
            "checksum": checksum,
            "seed": seed,
            "unique_task_count": len(tasks),
            "duplicate_line_count": len(file_rows) - len(rows),
            "logical_clock_unit": "milliseconds",
            "data_size_drives": "per-case sample rows",
            "checksum_rule": "sha256(canonical_json(sorted unique tasks))",
        },
    )

    configs_dir = input_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    v1_document = {
        "format_version": 1,
        "config_version": 1,
        "effective_relative_seconds": 0.0,
        "hyperparameters": {
            "seed_offset": 7,
            "weight_scale": 0.8,
            "learning_rate": 0.01,
            "activation": "tanh",
            "rounding": "half_away_from_zero",
            "max_row_block": 64,
            "quantization_version": 1,
            "legacy_momentum": 0.9,
            "notes": "v1 single-file baseline",
        },
        "compatibility_version": 1,
    }
    write_v1_document(configs_dir / "config_v1.json", v1_document)

    v2_generations = [
        {
            "config_generation": 2,
            "effective_at_ms": V2_EFFECTIVE_AT_MS,
            "shards": [
                {
                    "hyperparameters": {
                        "seed_offset": 11,
                        "weight_scale": 0.95,
                        "learning_rate": 0.02,
                        "activation": "tanh",
                        "rounding": "half_away_from_zero",
                    }
                },
                {
                    "hyperparameters": {
                        "max_row_block": 48,
                        "quantization_version": 2,
                        "tensor_parallelism": 2,
                        "notes": "v2 sharded metadata snapshot",
                    }
                },
            ],
        }
    ]
    tombstones = [
        {
            "tombstone_id": "tombstone-001-legacy-momentum",
            "config_generation": 2,
            "field_path": "hyperparameters.legacy_momentum",
            "effective_at_ms": V2_EFFECTIVE_AT_MS,
            "reason": "legacy momentum superseded by generation-2 optimizer settings",
        }
    ]
    write_v2_snapshot(
        configs_dir / "v2",
        generations=v2_generations,
        tombstones=tombstones,
    )
    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "seed": seed,
        "task_file_rows": len(file_rows),
        "unique_tasks": len(tasks),
        "duplicate_lines": len(file_rows) - len(rows),
        "task_checksum": checksum,
        "v1_config": str(configs_dir / "config_v1.json"),
        "v2_config_dir": str(configs_dir / "v2"),
        "v2_effective_at_ms": V2_EFFECTIVE_AT_MS,
    }
    atomic_write_json(output_dir / "sample_generation.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="input")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    summary = generate(args.input_dir, args.out_dir, args.seed)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
