from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def write_dataset(root: Path, *, n_samples: int = 90, seed: int = 7719) -> Path:
    rng = random.Random(seed)
    path = root / "data" / "table.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    positives = 0
    for index in range(n_samples):
        x1 = rng.gauss(0.0, 1.0)
        x2 = rng.gauss(0.4, 1.2)
        x3 = rng.uniform(-1.5, 1.5)
        cat0 = rng.choice(["alpha", "beta", "gamma"])
        score = (
            1.2 * x1
            - 0.8 * x2
            + 0.5 * x3
            + {"alpha": 0.2, "beta": -0.4, "gamma": 0.7}[cat0]
            + rng.gauss(0.0, 0.7)
        )
        label = 1 if score > 0.0 else 0
        positives += label
        rows.append(
            {
                "row_id": f"row-{index:05d}",
                "x1": round(x1, 10),
                "x2": round(x2, 10),
                "x3": round(x3, 10),
                "cat0": cat0,
                "label": label,
            }
        )
    if positives == 0 or positives == n_samples:
        rows[0]["label"] = 1
        rows[1]["label"] = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def write_config(
    root: Path,
    *,
    data_path: Path,
    run_id: str,
    mode: str = "float64",
    workers: int = 1,
    strengths: tuple[float, ...] = (0.01, 0.05, 0.2),
    max_iterations: int = 500,
    tolerance: float | None = None,
    standardization: str = "zscore",
    fixed_scale: int = 4096,
    fixed_rounding: str = "nearest_even",
    block_size: int = 16,
    min_block_size: int = 4,
    memory_budget_bytes: int = 32 * 1024 * 1024,
    comparison_budget: int = 20_000_000,
    max_path_size: int = 10,
    max_iterations_per_strength: int = 2000,
    objective_abs_tolerance: float | None = None,
    objective_rel_tolerance: float | None = None,
    monotonicity_abs_tolerance: float | None = None,
    monotonicity_rel_tolerance: float | None = None,
    allow_small_block_downgrade: bool = True,
    allow_worker_downgrade: bool = True,
    resume: bool = True,
    snapshot_dir: Path | None = None,
    output_dir: Path | None = None,
    suffix: str = "",
) -> Path:
    if tolerance is None:
        tolerance = {
            "float64": 1.0e-8,
            "float32": 1.0e-5,
            "fixed_point": 5.0e-4,
        }[mode]
    if objective_abs_tolerance is None:
        objective_abs_tolerance = {
            "float64": 1.0e-7,
            "float32": 1.0e-4,
            "fixed_point": 3.0e-3,
        }[mode]
    if objective_rel_tolerance is None:
        objective_rel_tolerance = {
            "float64": 1.0e-8,
            "float32": 1.0e-5,
            "fixed_point": 3.0e-3,
        }[mode]
    if monotonicity_abs_tolerance is None:
        monotonicity_abs_tolerance = {
            "float64": 1.0e-10,
            "float32": 1.0e-5,
            "fixed_point": 2.0e-3,
        }[mode]
    if monotonicity_rel_tolerance is None:
        monotonicity_rel_tolerance = {
            "float64": 0.005,
            "float32": 0.01,
            "fixed_point": 0.02,
        }[mode]
    config = {
        "schema_version": 1,
        "run_id": run_id,
        "data": {
            "path": str(data_path),
            "row_id_column": "row_id",
            "label_column": "label",
            "numeric_columns": ["x1", "x2", "x3"],
            "categorical_columns": ["cat0"],
            "positive_label": 1,
            "negative_label": 0,
            "category_encoding": "one_hot_sorted",
        },
        "preprocessing": {"standardization": standardization},
        "model": {
            "loss": "squared_error",
            "regularization": {"type": "elastic_net", "l1_ratio": 0.25},
            "strengths": list(strengths),
        },
        "solver": {
            "max_iterations": max_iterations,
            "tolerance": tolerance,
            "lipschitz_safety": 0.99,
        },
        "precision": {
            "mode": mode,
            "fixed_point": {"scale": fixed_scale, "rounding": fixed_rounding},
        },
        "execution": {
            "block_size": block_size,
            "workers": workers,
            "min_block_size": min_block_size,
            "reduction_order": "block_index_ascending",
            "memory_budget_bytes": memory_budget_bytes,
            "comparison_budget": comparison_budget,
            "max_path_size": max_path_size,
            "max_iterations_per_strength": max_iterations_per_strength,
            "allow_small_block_downgrade": allow_small_block_downgrade,
            "allow_worker_downgrade": allow_worker_downgrade,
        },
        "snapshots": {
            "directory": str(snapshot_dir or (root / f"snap-{run_id}{suffix}")),
            "enabled": True,
            "resume": resume,
        },
        "output": {"directory": str(output_dir or (root / f"run-{run_id}{suffix}"))},
        "validation": {
            "monotonicity_abs_tolerance": monotonicity_abs_tolerance,
            "monotonicity_rel_tolerance": monotonicity_rel_tolerance,
            "objective_abs_tolerance": objective_abs_tolerance,
            "objective_rel_tolerance": objective_rel_tolerance,
        },
    }
    path = root / f"config-{run_id}{suffix}.json"
    path.write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def run_cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "linear_path_engine", *args],
        cwd=ROOT,
        env=cli_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

