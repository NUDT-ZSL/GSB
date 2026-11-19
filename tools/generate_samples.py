#!/usr/bin/env python3
"""Generate deterministic local batches, query, and precision fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # The runtime engine itself does not need NumPy.
    np = None


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def column_kind(values: list[Any]) -> str:
    if any(isinstance(value, float) for value in values):
        return "float"
    if any(isinstance(value, str) for value in values):
        return "string"
    if any(isinstance(value, bool) for value in values):
        return "boolean"
    return "integer"


def build_batch(
    seq: int,
    row_count: int,
    columns: dict[str, list[Any]],
) -> dict[str, Any]:
    body = {
        "seq": seq,
        "row_count": row_count,
        "columns": [
            {
                "name": name,
                "kind": column_kind(values),
                "values": values,
            }
            for name, values in columns.items()
        ],
    }
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return {
        "seq": seq,
        "row_count": row_count,
        "columns": columns,
        "content_digest": digest,
    }


def as_values(values: list[float]) -> list[float]:
    if np is not None:
        return np.asarray(values, dtype=np.float64).tolist()
    return values


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    batch_specs = [
        (
            0,
            ["A", "B", "A", "B", "C", "C", "A", "B"],
            [0.1, 0.1, 0.2, 0.2, 1.0000000001, 1.0000000002, 0.3, 0.3],
        ),
        (
            1,
            ["A", "B", "C", "A", "B", "C", "A", "B"],
            [0.4, 0.4, 0.7, 0.5, 0.5, 0.8, 0.6, 0.6],
        ),
        (
            2,
            ["A", "B", "C", "A", "B", "C", "A", "B"],
            [0.7, 0.7000000001, 0.9, 0.8, 0.8, 1.0, 0.9, 0.9],
        ),
    ]
    batches: list[dict[str, Any]] = []
    event_id = 0
    for seq, groups, values in batch_specs:
        weights = list(range(seq * 8 + 1, seq * 8 + 9))
        ids = list(range(event_id, event_id + len(groups)))
        event_id += len(groups)
        batches.append(
            build_batch(
                seq,
                len(groups),
                {
                    "event_id": ids,
                    "grp": groups,
                    "value": as_values(values),
                    "weight": weights,
                },
            )
        )
    document = {
        "schema_version": 1,
        "description": "Deterministic near-tie sample with cross-batch windows",
        "batches": batches,
    }
    (SAMPLES / "batches.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    query = {
        "plan_version": 1,
        "description": "Grouped aggregates and length-4 stride-2 windows",
        "group_by": ["grp"],
        "aggregates": [
            {"name": "value_sum", "op": "sum", "column": "value"},
            {"name": "value_mean", "op": "mean", "column": "value"},
            {"name": "value_var", "op": "variance", "column": "value"},
            {"name": "row_count", "op": "count"},
        ],
        "window": {
            "column": "value",
            "length": 4,
            "step": 2,
            "boundary": "emit",
            "functions": [
                {"name": "w4_sum", "op": "sum"},
                {"name": "w4_mean", "op": "mean"},
            ],
        },
    }
    (SAMPLES / "query.json").write_text(
        json.dumps(query, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    query_v2 = json.loads(json.dumps(query))
    query_v2["plan_version"] = 2
    query_v2["description"] = "Incremental plan v2: one new aggregate operator"
    query_v2["aggregates"].append(
        {"name": "weight_sum", "op": "sum", "column": "weight"}
    )
    (SAMPLES / "query_v2.json").write_text(
        json.dumps(query_v2, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    precision = {
        "accumulator": "fixed",
        "scale": 100,
        "relative_tolerance": 1e-6,
        "clock_step": 10,
        "clock_start": 1000,
        "int_bits": 64,
    }
    (SAMPLES / "precision.json").write_text(
        json.dumps(precision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    precision_float = dict(precision)
    precision_float["accumulator"] = "float64"
    (SAMPLES / "precision_float.json").write_text(
        json.dumps(precision_float, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "batches": len(batches),
                "digests": [batch["content_digest"] for batch in batches],
                "numpy": None if np is None else np.__version__,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
