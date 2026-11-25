from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .generator import generate_batch, write_batch_file
from .parsing import parse_bundle


FIXED_GLOBAL_SEED = 430


def sample_raw(seed: int = FIXED_GLOBAL_SEED) -> dict[str, dict[str, Any]]:
    table = {
        "global_seed": seed,
        "tables": [
            {
                "name": "orders",
                "skew_column": "user_id",
                "skew_ratio": "0.72",
                "window": {"type": "tumbling", "size_ms": 1000, "slide_ms": 1000},
                "columns": [
                    {"name": "user_id", "type": "bigint", "ndv": 16},
                    {"name": "region", "type": "string", "ndv": 4},
                    {"name": "amount", "type": "decimal", "ndv": 32, "decimal_scale": 2},
                    {"name": "event_ts", "type": "timestamp", "ndv": 64},
                ],
            }
        ],
    }
    aggregations = [
        {"op": "count", "column": "*", "alias": "row_count"},
        {"op": "sum", "column": "amount", "alias": "gmv"},
        {"op": "avg", "column": "amount", "alias": "avg_amount"},
        {"op": "min", "column": "amount", "alias": "min_amount"},
        {"op": "max", "column": "amount", "alias": "max_amount"},
    ]
    plan = {
        "plan_id": "local-deterministic-plan",
        "table": "orders",
        "window_order": ["w1", "w2", "w3"],
        "windows": [
            {
                "window_id": "w1",
                "table": "orders",
                "group_by": ["user_id", "region"],
                "aggregations": aggregations,
                "operator": "sort",
                "size_ms": 1000,
                "cost_estimate": {"input_rows": 60, "output_cardinality": 8, "elapsed_ms": "1.25"},
            },
            {
                "window_id": "w2",
                "table": "orders",
                "group_by": ["user_id", "region"],
                "aggregations": aggregations,
                "operator": "sort",
                "size_ms": 1000,
                "cost_estimate": {"input_rows": 180, "output_cardinality": 12, "elapsed_ms": "3.5"},
            },
            {
                "window_id": "w3",
                "table": "orders",
                "group_by": ["user_id", "region"],
                "aggregations": aggregations,
                "operator": "sort",
                "size_ms": 1000,
                "cost_estimate": {"input_rows": 180, "output_cardinality": 12, "elapsed_ms": "3.5"},
            },
        ],
        "initial_form": {
            "form_id": "sort-baseline-v1",
            "target_version": 1,
            "window_order": ["w1", "w2", "w3"],
            "operators": {"w1": "sort", "w2": "sort", "w3": "sort"},
        },
        "candidates": [
            {
                "form_id": "hash-balanced-v2",
                "target_version": 2,
                "window_order": ["w1", "w2", "w3"],
                "operators": {"w1": "hash", "w2": "hash", "w3": "hash"},
                "setup_cost_ms": "18.4200003",
                "projected_runtime_ms": "8.9999999",
            },
            {
                "form_id": "hash-compact-v3",
                "target_version": 3,
                "window_order": ["w1", "w2", "w3"],
                "operators": {"w1": "hash", "w2": "hash", "w3": "hash"},
                "setup_cost_ms": "18.4200002",
                "projected_runtime_ms": "8.9999999",
            },
            {
                "form_id": "sort-reorder-v4",
                "target_version": 4,
                "window_order": ["w2", "w1", "w3"],
                "operators": {"w1": "sort", "w2": "sort", "w3": "sort"},
                "setup_cost_ms": "5.0",
                "projected_runtime_ms": "40.0",
            },
            {
                "form_id": "expensive-full-rehash-v9",
                "target_version": 9,
                "window_order": ["w1", "w2", "w3"],
                "operators": {"w1": "hash", "w2": "hash", "w3": "hash"},
                "setup_cost_ms": "900.0",
                "projected_runtime_ms": "1.0",
            },
        ],
    }
    arrivals = {
        "global_seed": seed,
        "batches": [
            {"seq": 1, "table": "orders", "window_id": "w1", "arrival_ms": 1, "row_count": 120, "window_end": False},
            {"seq": 2, "table": "orders", "window_id": "w1", "arrival_ms": 8, "row_count": 180, "window_end": True},
            {"seq": 3, "table": "orders", "window_id": "w2", "arrival_ms": 16, "row_count": 300, "window_end": False},
            {"seq": 4, "table": "orders", "window_id": "w2", "arrival_ms": 24, "row_count": 420, "window_end": True},
            {"seq": 5, "table": "orders", "window_id": "w3", "arrival_ms": 32, "row_count": 300, "window_end": False},
            {"seq": 6, "table": "orders", "window_id": "w3", "arrival_ms": 40, "row_count": 420, "window_end": True},
        ],
    }
    config = {
        "drift_threshold": "0.20",
        "cooldown_ms": 100,
        "switch_cost_budget_ms": "120",
        "max_parallel_tasks": 3,
        "logical_time_budget_per_round_ms": 100000,
        "fixed_point_scale": 1000000,
        "memory_budget_bytes": 67108864,
        "cost_tolerance_ms": "0.01",
        "min_observation_batches": 1,
        "rounding_mode": "floor",
    }
    return {"table": table, "plan": plan, "arrivals": arrivals, "config": config}


def write_sample_files(out_dir: str | Path, seed: int = FIXED_GLOBAL_SEED) -> dict[str, Any]:
    root = Path(out_dir)
    (root / "batches").mkdir(parents=True, exist_ok=True)
    raw = sample_raw(seed)
    for name in ("table", "plan", "arrivals", "config"):
        with (root / f"{name}.json").open("w", encoding="utf-8") as handle:
            json.dump(raw[name], handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    bundle = parse_bundle(raw["table"], raw["plan"], raw["arrivals"], raw["config"])
    table = bundle.tables[0]
    manifest: list[dict[str, Any]] = []
    for spec in bundle.arrivals.batches:
        envelope = generate_batch(table, table.global_seed, spec)
        write_batch_file(root / "batches" / f"batch-{spec.seq:04d}.json", envelope)
        manifest.append(envelope.summary.to_dict())
    with (root / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump({"global_seed": seed, "generator_version": manifest[0]["generator_version"], "batches": manifest}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {"out_dir": str(root), "batch_count": len(manifest), "manifest": manifest}
