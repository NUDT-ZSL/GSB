#!/usr/bin/env python3
"""Generate fixed-seed JSONL data and self-contained experiment configs."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def generate_rows(*, seed: int, samples: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    categories_a = ["alpha", "beta", "gamma"]
    categories_b = ["east", "west"]
    rows: list[dict[str, Any]] = []
    positive = 0
    for index in range(samples):
        x1 = rng.gauss(0.0, 1.0)
        x2 = rng.gauss(0.5, 1.4)
        x3 = rng.uniform(-2.0, 2.0)
        x4 = 0.6 * x1 + rng.gauss(0.0, 0.5)
        x5 = rng.gauss(-0.3, 0.8)
        cat0 = rng.choice(categories_a)
        cat1 = rng.choice(categories_b)
        cat_effect = {"alpha": 0.3, "beta": -0.4, "gamma": 0.8}[cat0]
        cat_effect += {"east": 0.2, "west": -0.2}[cat1]
        noise = rng.gauss(0.0, 0.9)
        score = 1.4 * x1 - 1.0 * x2 + 0.7 * x3 + 0.3 * x4 - 0.5 * x5 + cat_effect + noise
        label = 1 if score > 0.0 else 0
        positive += label
        rows.append(
            {
                "row_id": f"r{index:06d}",
                "x1": round(x1, 10),
                "x2": round(x2, 10),
                "x3": round(x3, 10),
                "x4": round(x4, 10),
                "x5": round(x5, 10),
                "cat0": cat0,
                "cat1": cat1,
                "label": label,
            }
        )
    if positive == 0 or positive == samples:
        raise RuntimeError("generated sample is not binary; change the seed")
    return rows


def _base_config(
    *,
    run_id: str,
    data_path: str,
    snapshot_dir: str,
    output_dir: str,
    precision: str,
    workers: int,
    max_iterations: int,
    tolerance: float,
    objective_abs_tolerance: float,
    objective_rel_tolerance: float,
    monotonicity_abs_tolerance: float,
    monotonicity_rel_tolerance: float,
    fixed_scale: int = 4096,
    fixed_rounding: str = "nearest_even",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "data": {
            "path": data_path,
            "row_id_column": "row_id",
            "label_column": "label",
            "numeric_columns": ["x1", "x2", "x3", "x4", "x5"],
            "categorical_columns": ["cat0", "cat1"],
            "positive_label": 1,
            "negative_label": 0,
            "category_encoding": "one_hot_sorted",
        },
        "preprocessing": {"standardization": "zscore"},
        "model": {
            "loss": "squared_error",
            "regularization": {"type": "elastic_net", "l1_ratio": 0.3},
            "strengths": [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1],
        },
        "solver": {
            "max_iterations": max_iterations,
            "tolerance": tolerance,
            "lipschitz_safety": 0.99,
        },
        "precision": {
            "mode": precision,
            "fixed_point": {"scale": fixed_scale, "rounding": fixed_rounding},
        },
        "execution": {
            "block_size": 48,
            "workers": workers,
            "min_block_size": 8,
            "reduction_order": "block_index_ascending",
            "memory_budget_bytes": 64 * 1024 * 1024,
            "comparison_budget": 100_000_000,
            "max_path_size": 16,
            "max_iterations_per_strength": 5000,
            "allow_small_block_downgrade": True,
            "allow_worker_downgrade": True,
        },
        "snapshots": {"directory": snapshot_dir, "enabled": True, "resume": True},
        "output": {"directory": output_dir},
        "validation": {
            "monotonicity_abs_tolerance": monotonicity_abs_tolerance,
            "monotonicity_rel_tolerance": monotonicity_rel_tolerance,
            "objective_abs_tolerance": objective_abs_tolerance,
            "objective_rel_tolerance": objective_rel_tolerance,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--seed", type=int, default=20261122)
    parser.add_argument("--samples", type=int, default=240)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    data_dir = root / "data"
    config_dir = root / "configs"
    rows = generate_rows(seed=args.seed, samples=args.samples)
    data_path = data_dir / "table.jsonl"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with data_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    configs = {
        "float64.json": _base_config(
            run_id="demo-float64",
            data_path="../data/table.jsonl",
            snapshot_dir="../artifacts/snapshots-float64",
            output_dir="../artifacts/run-float64",
            precision="float64",
            workers=4,
            max_iterations=1200,
            tolerance=1.0e-8,
            objective_abs_tolerance=1.0e-7,
            objective_rel_tolerance=1.0e-8,
            monotonicity_abs_tolerance=1.0e-10,
            monotonicity_rel_tolerance=0.005,
        ),
        "float64-serial.json": _base_config(
            run_id="demo-float64-serial",
            data_path="../data/table.jsonl",
            snapshot_dir="../artifacts/snapshots-float64-serial",
            output_dir="../artifacts/run-float64-serial",
            precision="float64",
            workers=1,
            max_iterations=1200,
            tolerance=1.0e-8,
            objective_abs_tolerance=1.0e-7,
            objective_rel_tolerance=1.0e-8,
            monotonicity_abs_tolerance=1.0e-10,
            monotonicity_rel_tolerance=0.005,
        ),
        "float32.json": _base_config(
            run_id="demo-float32",
            data_path="../data/table.jsonl",
            snapshot_dir="../artifacts/snapshots-float32",
            output_dir="../artifacts/run-float32",
            precision="float32",
            workers=4,
            max_iterations=1000,
            tolerance=2.0e-5,
            objective_abs_tolerance=2.0e-4,
            objective_rel_tolerance=3.0e-5,
            monotonicity_abs_tolerance=2.0e-5,
            monotonicity_rel_tolerance=0.01,
        ),
        "fixed-point.json": _base_config(
            run_id="demo-fixed-point",
            data_path="../data/table.jsonl",
            snapshot_dir="../artifacts/snapshots-fixed-point",
            output_dir="../artifacts/run-fixed-point",
            precision="fixed_point",
            workers=4,
            max_iterations=800,
            tolerance=5.0e-4,
            objective_abs_tolerance=2.0e-3,
            objective_rel_tolerance=2.0e-3,
            monotonicity_abs_tolerance=2.0e-3,
            monotonicity_rel_tolerance=0.02,
            fixed_scale=4096,
            fixed_rounding="nearest_even",
        ),
    }
    l2_config = _base_config(
        run_id="demo-float64-l2",
        data_path="../data/table.jsonl",
        snapshot_dir="../artifacts/snapshots-float64-l2",
        output_dir="../artifacts/run-float64-l2",
        precision="float64",
        workers=4,
        max_iterations=1200,
        tolerance=1.0e-8,
        objective_abs_tolerance=1.0e-7,
        objective_rel_tolerance=1.0e-8,
        monotonicity_abs_tolerance=1.0e-10,
        monotonicity_rel_tolerance=0.005,
    )
    l2_config["model"]["regularization"] = {"type": "l2"}
    configs["float64-l2.json"] = l2_config
    for name, config in configs.items():
        _write_json(config_dir / name, config)
    print(
        json.dumps(
            {
                "seed": args.seed,
                "samples": len(rows),
                "data": str(data_path),
                "configs": sorted(configs),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

