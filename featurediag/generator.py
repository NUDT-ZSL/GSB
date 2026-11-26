"""Deterministic local fixtures generated from a fixed master seed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, atomic_write_jsonl, sha256_json, stable_int
from .models import Batch, RunConfig, Tolerance, parse_task
from .snapshot import FeatureEntry, Snapshot, Tombstone, _write_v2


MASTER_SEED = 20261127


def _task_rows(master_seed: int) -> list[dict[str, Any]]:
    return [
        {
            "task_id": "task-alpha",
            "feature_dim": 8,
            "feature_ids": [f"f{index}" for index in range(8)],
            "quant_scale": 0.125,
            "quant_zero_point": 2,
            "block_plan": [0, 3, 6, 8],
            "revision": 1,
            "logical_clock_ms": 2000,
            "quant_formula": "q=round(x/scale)+zero_point",
            "dequant_formula": "x_hat=(q-zero_point)*scale",
            "rounding_mode": "half_away_from_zero",
            "normalization": {"method": "zscore", "ddof": 0, "epsilon": 1e-12},
            "reduction_rule": "canonical_block_order_exact",
            "tolerance": {"abs_max": 2.0, "rel_max": 0.15, "relative_floor": 1e-12},
        },
        {
            "task_id": "task-beta",
            "feature_dim": 6,
            "feature_ids": [f"f{index}" for index in range(8, 14)],
            "quant_scale": 0.25,
            "quant_zero_point": -1,
            "block_plan": [[0, 2], [2, 5], [5, 6]],
            "revision": 1,
            "logical_clock_ms": 3000,
            "quant_formula": "q = round(x / quant_scale) + quant_zero_point",
            "dequant_formula": "x_hat=(q-quant_zero_point)*quant_scale",
            "rounding_mode": "half_to_even",
            "normalization": {"method": "zscore", "ddof": 0, "epsilon": 1e-12},
            "reduction_rule": "ordered_block_concat_exact",
            "tolerance": {"abs_max": 2.0, "rel_max": 0.15, "relative_floor": 1e-12},
        },
    ]


def _batch_rows(master_seed: int) -> list[dict[str, Any]]:
    base = stable_int(f"batches:{master_seed}") % 100000
    return [
        {
            "batch_id": "b-001",
            "task_id": "task-alpha",
            "sample_count": 32,
            "feature_seed": base + 11,
        },
        {
            "batch_id": "b-002",
            "task_id": "task-alpha",
            "sample_count": 24,
            "feature_seed": base + 12,
        },
        {
            "batch_id": "b-003",
            "task_id": "task-beta",
            "sample_count": 20,
            "feature_seed": base + 21,
        },
        {
            "batch_id": "b-004",
            "task_id": "task-beta",
            "sample_count": 18,
            "feature_seed": base + 22,
        },
    ]


def _task_next_rows(master_seed: int) -> list[dict[str, Any]]:
    rows = _task_rows(master_seed)
    rows[0] = json.loads(json.dumps(rows[0]))
    rows[0]["revision"] = 2
    rows[0]["block_plan"] = [0, 2, 5, 8]
    rows[0]["logical_clock_ms"] = 2500
    return rows


def _base_params(feature_id: str, generation: int) -> dict[str, Any]:
    feature_number = int(feature_id[1:])
    return {
        "transform": "identity",
        "offset": float(feature_number % 3) / 10.0,
        "scale": 1.0 + float(feature_number % 4) / 10.0,
        "generation": generation,
    }


def _v1_snapshot() -> dict[str, Any]:
    entries = []
    for index in range(14):
        entries.append(
            {
                "feature_id": f"f{index}",
                "effective_seconds": index * 0.1,
                "base_params": _base_params(f"f{index}", 1),
            }
        )
    return {
        "format_version": "v1",
        "snapshot_version": 1,
        "snapshot_id": "spec-v1",
        "logical_clock_ms": 4000,
        "epoch_ms": 0,
        "feature_generation": 1,
        "entries": entries,
    }


def _v2_snapshot(*, next_generation: bool = False) -> Snapshot:
    entries: list[FeatureEntry] = []
    for index in range(14):
        if index == 7:
            continue
        generation = 2 if index < 8 else 1
        if next_generation and index < 4:
            generation = 3
        entries.append(
            FeatureEntry(
                feature_id=f"f{index}",
                effective_ms=index * 100,
                generation=generation,
                base_params=_base_params(f"f{index}", generation),
            )
        )
    tombstones = [
        Tombstone(
            feature_id="f7",
            effective_ms=700,
            generation=3 if next_generation else 2,
            reason="feature_retired_by_v2_generation",
        )
    ]
    generation_clock = 5000
    return Snapshot(
        format_version="v2",
        snapshot_version=2,
        snapshot_id="spec-v2-next" if next_generation else "spec-v2",
        logical_clock_ms=generation_clock,
        entries=entries,
        tombstones=tombstones,
        provenance={
            "direct_v2_fixture": {
                "absolute_ms": "generated directly in metadata.sqlite",
                "generation": "generated directly in metadata.sqlite",
                "tombstones": "generated directly in metadata.sqlite",
            }
        },
    )


def init_data(root: Path, *, force: bool = False) -> dict[str, Any]:
    data_dir = root / "data"
    if data_dir.exists() and not force:
        return {
            "code": "OK",
            "generated": False,
            "reason": "data directory already exists; use --force to regenerate",
            "data_dir": str(data_dir),
        }
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    config = RunConfig(
        master_seed=MASTER_SEED,
        tolerance=Tolerance(2.0, 0.15, 1e-12),
        reduction_rule="canonical_block_order_exact",
    )
    atomic_write_json(
        data_dir / "config.json",
        {
            "schema": config.schema,
            "master_seed": config.master_seed,
            "tolerance": config.tolerance.to_dict(),
            "reduction_rule": config.reduction_rule,
        },
    )
    tasks = _task_rows(MASTER_SEED)
    batches = _batch_rows(MASTER_SEED)
    atomic_write_jsonl(data_dir / "tasks.jsonl", tasks)
    atomic_write_jsonl(data_dir / "tasks_next.jsonl", _task_next_rows(MASTER_SEED))
    atomic_write_jsonl(data_dir / "batches.jsonl", batches)
    atomic_write_json(data_dir / "spec_v1.json", _v1_snapshot())
    _write_v2(_v2_snapshot(), data_dir / "spec_v2")
    _write_v2(_v2_snapshot(next_generation=True), data_dir / "spec_v2_next")
    task_map = {
        raw["task_id"]: parse_task(
            raw,
            source=str(data_dir / "tasks.jsonl"),
            default_tolerance=config.tolerance,
        )
        for raw in tasks
    }
    batch_objects = [
        Batch(
            batch_id=raw["batch_id"],
            task_id=raw["task_id"],
            sample_count=raw["sample_count"],
            feature_seed=raw["feature_seed"],
            raw=raw,
        )
        for raw in batches
    ]
    return {
        "code": "OK",
        "generated": True,
        "data_dir": str(data_dir),
        "master_seed": MASTER_SEED,
        "task_count": len(task_map),
        "batch_count": len(batch_objects),
        "snapshots": {
            "v1": str(data_dir / "spec_v1.json"),
            "v2": str(data_dir / "spec_v2"),
            "v2_next": str(data_dir / "spec_v2_next"),
        },
        "config_sha256": sha256_json(
            {
                "master_seed": config.master_seed,
                "tolerance": config.tolerance.to_dict(),
                "reduction_rule": config.reduction_rule,
            }
        ),
    }
