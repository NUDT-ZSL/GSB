#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def batch_payload(index: int, row_count: int, columns: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_index": index,
        "row_count": row_count,
        "columns": {
            name: {"type": col["type"], "values": col["values"]}
            for name, col in sorted(columns.items())
        },
    }


def sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_batch(index: int, rows: int, *, anomalies: bool = False, near_ties: bool = False) -> dict[str, Any]:
    ids = list(range(index * 1000, index * 1000 + rows))
    region = [i % 4 for i in range(rows)]
    amount: list[Any] = []
    for i in range(rows):
        base = round(((index * 37 + i * 11) % 23) / 7.0 - 2.0, 6)
        if near_ties and i in {3, 7, 11}:
            base = round(base + [1e-9, 2e-9, -1e-9][i % 3], 12)
        amount.append(base)
    active = [((index + i) % 3) != 0 for i in range(rows)]
    columns: dict[str, Any] = {
        "id": {"type": "int", "values": ids},
        "region_id": {"type": "int", "values": region},
        "amount": {"type": "float", "values": amount},
        "active": {"type": "bool", "values": active},
    }
    if anomalies and index == 1:
        amount[1] = float("nan")
        amount[4] = float("inf")
        amount[9] = float("-inf")
        amount[10] = 1e20
    payload = batch_payload(index, rows, columns)
    payload["content_digest"] = {"algorithm": "sha256", "value": sha256(canonical(payload))}
    return payload


BASE_QUERY = {
    "query_id": "regional-amounts-v1",
    "group_keys": ["region_id"],
    "filters": [{"column": "active", "op": "eq", "value": True}],
    "aggregates": [
        {"name": "amount_sum", "op": "sum", "column": "amount"},
        {"name": "amount_mean", "op": "mean", "column": "amount"},
        {"name": "amount_var", "op": "var", "column": "amount"},
        {"name": "id_count", "op": "count", "column": "id"},
    ],
    "window": {
        "length": 5,
        "step": 2,
        "partition_by": ["region_id"],
        "aggregates": [
            {"name": "amount_sum", "op": "sum", "column": "amount"},
            {"name": "amount_mean", "op": "mean", "column": "amount"},
            {"name": "id_count", "op": "count", "column": "id"},
        ],
    },
}


def base_config(*, parallelism: int = 3, memory: int = 16, shards: int = 3) -> dict[str, Any]:
    return {
        "parallel_limit": parallelism,
        "per_task_memory_mib": memory,
        "total_memory_budget_mib": parallelism * memory,
        "fixed_scale": 4,
        "tolerance": 1e-6,
        "logical_clock_step": 3,
        "shards_per_batch": shards,
        "adaptive": {
            "row_threshold": 1000,
            "group_cardinality_threshold": 100,
            "coarse_divisor": 2,
        },
    }


def make_precision_batch(index: int) -> dict[str, Any]:
    rows = 8
    region = [0, 0, 1, 1, 2, 2, 3, 3]
    amounts = [5.0, 5.0, 5.00000005, 5.00000005, 10.0, 10.0, 15.0, 15.0]
    columns = {
        "id": {"type": "int", "values": list(range(index * 1000, index * 1000 + rows))},
        "region_id": {"type": "int", "values": region},
        "amount": {"type": "float", "values": amounts},
        "active": {"type": "bool", "values": [True] * rows},
    }
    payload = batch_payload(index, rows, columns)
    payload["content_digest"] = {"algorithm": "sha256", "value": sha256(canonical(payload))}
    return payload


def write_experiment(
    name: str,
    batches: list[dict[str, Any]],
    *,
    query: dict[str, Any],
    config: dict[str, Any],
    injections: dict[str, Any] | None = None,
) -> Path:
    target = SAMPLES / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    batch_files: list[str] = []
    for index, payload in enumerate(batches):
        filename = f"batch_{index:04d}.json"
        write_json(target / filename, payload)
        batch_files.append(filename)
    write_json(target / "query.json", query)
    write_json(target / "config.json", config)
    write_json(target / "injections.json", injections or {"version": 1, "injections": []})
    experiment = {
        "version": 1,
        "batch_files": batch_files,
        "query_file": "query.json",
        "config_file": "config.json",
        "injection_file": "injections.json",
    }
    write_json(target / "experiment.json", experiment)
    return target


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    basic_batches = [
        make_batch(index, 12, near_ties=index == 0) for index in range(4)
    ]
    write_experiment(
        "basic",
        basic_batches,
        query=BASE_QUERY,
        config=base_config(),
    )
    anomaly_query = dict(BASE_QUERY)
    anomaly_query["query_id"] = "anomaly-spectrum-v1"
    anomaly_query["window"] = {
        "length": 3,
        "step": 1,
        "partition_by": ["region_id"],
        "aggregates": [
            {"name": "amount_sum", "op": "sum", "column": "amount"},
            {"name": "amount_var", "op": "var", "column": "amount"},
        ],
    }
    write_experiment(
        "anomalies",
        [make_batch(index, 12, anomalies=True) for index in range(2)],
        query=anomaly_query,
        config={**base_config(), "fixed_scale": 2, "tolerance": 1e-12},
    )
    write_experiment(
        "budget",
        [make_batch(index, 12) for index in range(3)],
        query=BASE_QUERY,
        config=base_config(parallelism=2, memory=1, shards=4),
    )
    write_experiment(
        "crash",
        [make_batch(index, 12) for index in range(4)],
        query=BASE_QUERY,
        config=base_config(parallelism=1, memory=16, shards=2),
        injections={
            "version": 1,
            "injections": [
                {
                    "id": "kill-after-two-commits",
                    "action": "kill",
                    "trigger": {"after_committed_shards": 2},
                    "once": True,
                }
            ],
        },
    )
    write_experiment(
        "crash_running",
        [make_batch(index, 12) for index in range(3)],
        query=BASE_QUERY,
        config=base_config(parallelism=1, memory=16, shards=2),
        injections={
            "version": 1,
            "injections": [
                {
                    "id": "kill-during-second-shard",
                    "action": "kill",
                    "trigger": {"shard_id": "b000000-s0001"},
                    "once": True,
                }
            ],
        },
    )
    adaptive_config = base_config(parallelism=2, memory=16, shards=4)
    adaptive_config["adaptive"] = {
        "row_threshold": 8,
        "group_cardinality_threshold": 2,
        "coarse_divisor": 2,
    }
    write_experiment(
        "adaptive",
        [make_batch(index, 12) for index in range(3)],
        query=BASE_QUERY,
        config=adaptive_config,
    )
    fine_config = base_config(parallelism=2, memory=16, shards=4)
    write_experiment(
        "adaptive_fine",
        [make_batch(index, 12) for index in range(3)],
        query=BASE_QUERY,
        config=fine_config,
    )
    precision_query = {
        "query_id": "near-tie-precision-v1",
        "group_keys": ["region_id"],
        "filters": [{"column": "active", "op": "eq", "value": True}],
        "aggregates": [
            {"name": "amount_sum", "op": "sum", "column": "amount"},
            {"name": "amount_mean", "op": "mean", "column": "amount"},
            {"name": "id_count", "op": "count", "column": "id"},
        ],
        "window": {
            "length": 4,
            "step": 2,
            "aggregates": [
                {"name": "amount_sum", "op": "sum", "column": "amount"},
                {"name": "amount_mean", "op": "mean", "column": "amount"},
            ],
        },
    }
    write_experiment(
        "precision",
        [make_precision_batch(0), make_precision_batch(1)],
        query=precision_query,
        config={**base_config(parallelism=2, memory=16, shards=2), "tolerance": 1e-6},
    )


if __name__ == "__main__":
    main()
