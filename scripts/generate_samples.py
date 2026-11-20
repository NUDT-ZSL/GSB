from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Any

from colagg.canonical import canonical_dumps, sha256_json


SEED = 20261121
BATCH_COUNT = 12
ROWS_PER_BATCH = 400
SITES = [f"S{index:02d}" for index in range(40)]
TIERS = list(range(5))
COMBINATIONS = [(site, tier) for site in SITES for tier in TIERS]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    out = Path(args.out)
    batches_dir = out / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    global_index = 0
    for batch_seq in range(1, BATCH_COUNT + 1):
        sites: list[str] = []
        tiers: list[int] = []
        regions: list[str] = []
        values: list[Any] = []
        units: list[int] = []
        ok_flags: list[bool] = []
        for row_index in range(ROWS_PER_BATCH):
            if row_index < len(COMBINATIONS):
                site, tier = COMBINATIONS[row_index]
            else:
                site, tier = rng.choice(COMBINATIONS)
            region = f"R{SITES.index(site) % 5}"
            value = round(rng.uniform(-250.0, 1250.0), 3)
            if global_index and global_index % 997 == 0:
                value_cell: Any = "NaN"
            elif global_index and global_index % 1499 == 0:
                value_cell = "Infinity"
            elif global_index and global_index % 2003 == 0:
                value_cell = "-Infinity"
            else:
                value_cell = value
            sites.append(site)
            tiers.append(tier)
            regions.append(region)
            values.append(value_cell)
            units.append(rng.randint(1, 50))
            ok_flags.append(rng.random() >= 0.18)
            global_index += 1

        columns = [
            {"name": "site", "type": "string", "values": sites},
            {"name": "tier", "type": "int", "values": tiers},
            {"name": "region", "type": "string", "values": regions},
            {"name": "value", "type": "float", "values": values},
            {"name": "units", "type": "int", "values": units},
            {"name": "ok", "type": "bool", "values": ok_flags},
        ]
        payload = {
            "batch_seq": batch_seq,
            "row_count": ROWS_PER_BATCH,
            "columns": columns,
        }
        batch = {**payload, "batch_digest": sha256_json(payload)}
        _write_json(batches_dir / f"batch-{batch_seq:04d}.json", batch)

    query = {
        "query_id": "deterministic-group-window-v1",
        "group_by": ["site", "tier"],
        "aggregates": [
            {"name": "value_sum", "op": "sum", "column": "value"},
            {"name": "value_mean", "op": "mean", "column": "value"},
            {"name": "value_variance", "op": "variance", "column": "value"},
            {"name": "row_count", "op": "count", "column": "value"},
            {"name": "units_sum", "op": "sum", "column": "units"},
        ],
        "window": {
            "length": 8,
            "stride": 4,
            "column": "value",
            "outputs": ["count", "sum", "mean", "variance"],
        },
    }
    config = {
        "memory_budget_mib": 1,
        "max_batch_rows": 400,
        "spill_dir": "spill",
        "fixed_scale": 1000,
        "fixed_int_bits": 64,
        "float_tolerance": 1e-12,
        "logical_clock_step": 1,
        "numeric_mode": "fixed",
    }
    empty_manifest = {"injections": []}
    anomaly_manifest = {
        "injections": [
            {
                "id": "read-malformed-02",
                "phase": "read",
                "batch_seq": 2,
                "kind": "malformed_column",
                "column": "value",
            },
            {
                "id": "read-digest-04",
                "phase": "read",
                "batch_seq": 4,
                "kind": "digest_mismatch",
            },
            {
                "id": "aggregate-fixed-overflow-06",
                "phase": "aggregate",
                "batch_seq": 6,
                "kind": "fixed_overflow",
                "group": ["S00", 0],
            },
            {
                "id": "window-corrupt-08",
                "phase": "window",
                "batch_seq": 8,
                "kind": "window_corrupt",
                "group": ["S01", 1],
            },
            {
                "id": "write-spill-tamper-10",
                "phase": "write",
                "batch_seq": 10,
                "kind": "spill_tamper",
                "group": ["S02", 2],
            },
            {
                "id": "read-spill-corrupt-12",
                "phase": "read",
                "batch_seq": 12,
                "kind": "spill_readback_corrupt",
                "group": ["S03", 3],
            },
        ]
    }
    crash_manifest = {
        "injections": [
            {
                "id": "crash-after-spill-05",
                "phase": "write",
                "batch_seq": 5,
                "kind": "crash_after_spill",
                "group": ["S04", 4],
            }
        ]
    }
    _write_json(out / "query.json", query)
    _write_json(out / "config.json", config)
    _write_json(out / "injections.json", empty_manifest)
    _write_json(out / "injections-anomalies.json", anomaly_manifest)
    _write_json(out / "injections-crash.json", crash_manifest)
    return 0


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(canonical_dumps(payload) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
