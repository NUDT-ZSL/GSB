#!/usr/bin/env python3
from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gsb.util import sha256_json, stable_json


BATCH_COUNT = 8
ROWS_PER_BATCH = 2000
HOT_COUNTS = {"hot-A": 1100, "hot-B": 360, "hot-C": 160}
TAIL_COUNT = 60


def shuffled(values: list[str], seed: int) -> list[str]:
    data = list(values)
    state = seed & 0xFFFFFFFF
    for index in range(len(data) - 1, 0, -1):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        other = state % (index + 1)
        data[index], data[other] = data[other], data[index]
    return data


def group_sequence(batch_number: int) -> list[str]:
    values: list[str] = []
    for key, count in HOT_COUNTS.items():
        values.extend([key] * count)
    tail_total = ROWS_PER_BATCH - len(values)
    base, remainder = divmod(tail_total, TAIL_COUNT)
    for index in range(TAIL_COUNT):
        count = base + (1 if index < remainder else 0)
        values.extend([f"tail-{index:02d}"] * count)
    if len(values) != ROWS_PER_BATCH:
        raise AssertionError(len(values))
    return shuffled(values, 0x5EED0000 + batch_number)


def amount_for(group: str, global_row: int) -> float | str:
    if group == "hot-A":
        if global_row % 257 == 0:
            return "NaN"
        if global_row % 389 == 0:
            return "Infinity"
        if global_row % 521 == 0:
            return "-Infinity"
        base = 10.25
    elif group == "hot-B":
        base = -3.5
    elif group == "hot-C":
        base = 7.75
    else:
        tail_index = int(group.split("-")[1])
        base = 0.5 + tail_index * 0.03125
    return base + ((global_row * 17) % 1000) / 100.0


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(payload), encoding="utf-8")


def main() -> int:
    sample = ROOT / "samples" / "tilted"
    if sample.exists():
        shutil.rmtree(sample)
    (sample / "batches").mkdir(parents=True)
    config = {
        "time_budget_ms": 36.0,
        "max_batch_rows": 2500,
        "hot_key_min_rows": 2000,
        "skew_ratio": 0.15,
        "memory_limit_mib": 0.02,
        "fixed_scale": 1000,
        "float_tolerance": 1e-12,
        "logical_clock_step_ms": 1.5,
        "exact_cost_ms_per_1k_rows": 5.0,
        "accelerated_cost_ms_per_1k_rows": 0.8,
        "switch_overhead_ms": 1.0,
        "digest_algorithm": "sha256",
    }
    queries = {
        "queries": [
            {
                "query_id": "tilted-main",
                "group_key": "region",
                "aggregates": [
                    {"name": "amount_sum", "op": "sum", "column": "amount"},
                    {"name": "amount_avg", "op": "avg", "column": "amount"},
                    {"name": "amount_var", "op": "var", "column": "amount"},
                    {"name": "amount_count", "op": "count", "column": "amount"},
                    {"name": "rows", "op": "count", "column": "*"},
                ],
            },
            {
                "query_id": "tilted-window",
                "group_key": "region",
                "aggregates": [
                    {"name": "amount_sum", "op": "sum", "column": "amount"},
                    {"name": "rows", "op": "count", "column": "*"},
                ],
                "filter": {"column": "mark", "op": "eq", "value": 1},
                "window": {"length": 4000, "step": 3500},
            },
        ]
    }
    write_json(sample / "config.json", config)
    write_json(sample / "queries.json", queries)
    global_row = 0
    for batch_number in range(1, BATCH_COUNT + 1):
        regions = group_sequence(batch_number)
        amounts: list[float] = []
        marks: list[int] = []
        row_ids: list[int] = []
        for local_row, region in enumerate(regions):
            value = amount_for(region, global_row)
            if value == "NaN":
                numeric = math.nan
            elif value == "Infinity":
                numeric = math.inf
            elif value == "-Infinity":
                numeric = -math.inf
            else:
                numeric = float(value)
            amounts.append(numeric)
            marks.append(1 if (global_row * 7) % 5 != 0 else 0)
            row_ids.append(global_row)
            global_row += 1
        columns = {
            "region": regions,
            "amount": amounts,
            "mark": marks,
            "row_id": row_ids,
        }
        content = {
            "batch_index": batch_number,
            "row_count": ROWS_PER_BATCH,
            "columns": columns,
        }
        payload = dict(content)
        payload["digest"] = sha256_json(content)
        write_json(
            sample / "batches" / f"batch-{batch_number:04d}.json",
            payload,
        )
    print(sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

