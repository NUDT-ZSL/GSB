from __future__ import annotations

import numpy as np

from columnar_engine.engine import Engine
from columnar_engine.ops import fixed_convert

from conftest import SAMPLE, values_by_column, write_json


def test_fixed_rounding_and_nan_policy() -> None:
    values = np.asarray([1.25, -1.25, 1.24, np.nan], dtype=np.float64)
    converted = fixed_convert(values, 1, node_id="test", field="value")
    assert converted.tolist() == [13, -13, 12, 0]


def test_nan_aggregate_policy_is_deterministic(tmp_path) -> None:
    batches = {
        "batches": [
            {
                "batch_id": 1,
                "rows": 3,
                "columns": {
                    "value": {"type": "float64", "values": [1.0, float("nan"), 3.0]}
                },
            }
        ]
    }
    plan = {
        "root": "agg",
        "nodes": [
            {"id": "scan", "type": "scan", "columns": ["value"]},
            {
                "id": "agg",
                "type": "aggregate",
                "input": "scan",
                "group_by": [],
                "aggregates": [
                    {"name": "count", "op": "count", "column": "value"},
                    {"name": "sum", "op": "sum", "column": "value"},
                    {"name": "sumsq", "op": "sumsq", "column": "value"},
                    {"name": "avg", "op": "avg", "column": "value"},
                    {"name": "fixed", "op": "fixed_sum", "column": "value", "scale": 1},
                ],
            },
        ],
    }
    config = {
        "vector_width": 2,
        "max_parallel_tasks": 2,
        "scan_memory_budget_mib": 1.0,
        "global_memory_budget_mib": 2.0,
        "round_time_budget_ms": 20,
        "logical_clock_step_ms": 5,
        "fixed_scale": 1,
        "float_tolerance": 0.00001,
        "operator_buffer_budget_mib": 1.0,
        "verify_rewrite": True,
    }
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_paths(
        write_json(tmp_path / "batches.json", batches),
        write_json(tmp_path / "plan.json", plan),
        write_json(tmp_path / "config.json", config),
    )
    assert summary.status == "COMPLETE"
    assert values_by_column(summary.result, "count") == [2]
    assert values_by_column(summary.result, "sum") == [4.0]
    assert values_by_column(summary.result, "sumsq") == [10.0]
    assert values_by_column(summary.result, "avg") == [2.0]
    assert values_by_column(summary.result, "fixed") == [40]
    assert summary.error_spectrum["nan_policy"] == "IGNORE_AND_COUNT"

