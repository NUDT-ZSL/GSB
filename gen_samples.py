#!/usr/bin/env python3
"""Generate deterministic diagnostic samples with seed 20250501.

Outputs:
  data/jobs.jsonl                 flat JSONL task manifest
  data/arrays/*.npy               deterministic input and kernel arrays
  configs/v1.json                 single-file v1 snapshot
  configs/v2/                     fragment JSON plus SQLite metadata
  configs/migration_v1_to_v2.json per-record migration provenance
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from diag_engine import (
    QuantConfigRecord,
    SEED,
    atomic_write_json,
    canonical_json,
    migrate_v1_to_v2,
    sha256_json,
    write_v1_records,
    write_v2_records,
)


CONFIGS: List[QuantConfigRecord] = [
    QuantConfigRecord(
        config_key="qcfg_conv_a",
        effective_abs_ms=0,
        generation=1,
        quant_scale=1.0 / 64.0,
        quant_zero_point=3,
        block_size=4,
        revision=1,
    ),
    QuantConfigRecord(
        config_key="qcfg_conv_b",
        effective_abs_ms=20,
        generation=1,
        quant_scale=1.0 / 128.0,
        quant_zero_point=-2,
        block_size=8,
        revision=1,
    ),
    QuantConfigRecord(
        config_key="qcfg_interp_bilinear",
        effective_abs_ms=30,
        generation=1,
        quant_scale=1.0 / 128.0,
        quant_zero_point=5,
        block_size=2,
        revision=1,
    ),
    QuantConfigRecord(
        config_key="qcfg_interp_nearest",
        effective_abs_ms=40,
        generation=1,
        quant_scale=1.0 / 64.0,
        quant_zero_point=0,
        block_size=3,
        revision=1,
    ),
]

TOMBSTONE = QuantConfigRecord(
    config_key="qcfg_retired",
    effective_abs_ms=50,
    generation=1,
    quant_scale=1.0 / 32.0,
    quant_zero_point=0,
    block_size=4,
    revision=1,
    tombstone=True,
)


def _config_for(key: str) -> QuantConfigRecord:
    for config in CONFIGS:
        if config.config_key == key:
            return config
    raise KeyError(key)


JOB_SPECS: List[Dict[str, Any]] = [
    {
        "job_id": "conv_pad_a",
        "op_kind": "conv2d",
        "input_shape": [1, 5, 5, 2],
        "kernel_shape": [2, 3, 3, 2],
        "stride": [1, 1],
        "padding": [1, 1],
        "config_key": "qcfg_conv_a",
        "logical_clock_ms": 100,
        "revision": 3,
    },
    {
        "job_id": "conv_stride_a",
        "op_kind": "conv2d",
        "input_shape": [1, 6, 6, 1],
        "kernel_shape": [2, 3, 3, 1],
        "stride": [2, 2],
        "padding": [0, 0],
        "config_key": "qcfg_conv_a",
        "logical_clock_ms": 110,
        "revision": 1,
    },
    {
        "job_id": "conv_edge_b",
        "op_kind": "conv2d",
        "input_shape": [1, 4, 4, 3],
        "kernel_shape": [1, 2, 2, 3],
        "stride": [1, 1],
        "padding": [1, 1],
        "config_key": "qcfg_conv_b",
        "logical_clock_ms": 120,
        "revision": 2,
    },
    {
        "job_id": "interp_bilinear_2x",
        "op_kind": "interpolate",
        "input_shape": [1, 4, 4, 2],
        "scale": [2.0, 2.0],
        "stride": [1, 1],
        "padding": [0, 0],
        "interp_mode": "bilinear",
        "config_key": "qcfg_interp_bilinear",
        "logical_clock_ms": 130,
        "revision": 1,
    },
    {
        "job_id": "interp_nearest_15",
        "op_kind": "interpolate",
        "input_shape": [1, 3, 5, 1],
        "scale": [1.5, 1.5],
        "stride": [1, 1],
        "padding": [0, 0],
        "interp_mode": "nearest",
        "config_key": "qcfg_interp_nearest",
        "logical_clock_ms": 140,
        "revision": 1,
    },
    {
        "job_id": "interp_bilinear_mixed",
        "op_kind": "interpolate",
        "input_shape": [1, 3, 4, 2],
        "scale": [1.5, 1.75],
        "stride": [1, 1],
        "padding": [0, 0],
        "interp_mode": "bilinear",
        "config_key": "qcfg_interp_bilinear",
        "logical_clock_ms": 150,
        "revision": 1,
    },
]


def _job_record(spec: Dict[str, Any], input_path: str, kernel_path: Optional[str]) -> Dict[str, Any]:
    config = _config_for(str(spec["config_key"]))
    record = {
        **spec,
        "quant_scale": config.quant_scale,
        "quant_zero_point": config.quant_zero_point,
        "block_size": config.block_size,
        "input_path": input_path,
        "tolerance": 0.25,
    }
    if kernel_path is not None:
        record["kernel_path"] = kernel_path
    return record


def generate(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    data_dir = root / "data"
    arrays_dir = data_dir / "arrays"
    configs_dir = root / "configs"
    v1_path = configs_dir / "v1.json"
    v2_dir = configs_dir / "v2"
    report_path = configs_dir / "migration_v1_to_v2.json"

    if (data_dir / "jobs.jsonl").exists():
        shutil.rmtree(data_dir)
    if v2_dir.exists():
        shutil.rmtree(v2_dir)
    configs_dir.mkdir(parents=True, exist_ok=True)
    arrays_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SEED)
    manifest_rows: List[Dict[str, Any]] = []
    array_hashes: Dict[str, str] = {}

    for spec in JOB_SPECS:
        job_id = str(spec["job_id"])
        input_array = rng.uniform(-1.0, 1.0, size=tuple(spec["input_shape"]))
        input_array = np.round(input_array, 8)
        input_rel = f"data/arrays/{job_id}_input.npy"
        np.save(root / input_rel, input_array, allow_pickle=False)
        array_hashes[input_rel] = sha256_json(
            {"shape": list(input_array.shape), "values": input_array.tolist()}
        )
        kernel_path: Optional[str] = None
        if spec["op_kind"] == "conv2d":
            kernel = rng.normal(0.0, 0.5, size=tuple(spec["kernel_shape"]))
            kernel = np.round(kernel, 8)
            kernel_rel = f"data/arrays/{job_id}_kernel.npy"
            np.save(root / kernel_rel, kernel, allow_pickle=False)
            array_hashes[kernel_rel] = sha256_json({"shape": list(kernel.shape), "values": kernel.tolist()})
            kernel_path = kernel_rel
        manifest_rows.append(_job_record(spec, input_rel, kernel_path))

    with (data_dir / "jobs.jsonl").open("w", encoding="utf-8") as fh:
        for row in manifest_rows:
            fh.write(canonical_json(row) + "\n")

    write_v1_records(v1_path, CONFIGS, base_epoch_ms=0, relative_epoch_seconds=0.0)
    migrate_v1_to_v2(v1_path, v2_dir, base_epoch_ms=0, report_path=report_path)
    write_v2_records(v2_dir, [*CONFIGS, TOMBSTONE])

    manifest_hash = sha256_json(manifest_rows)
    summary = {
        "schema": "sample-generation-report-v1",
        "seed": SEED,
        "root": str(root),
        "job_count": len(manifest_rows),
        "manifest_hash": manifest_hash,
        "manifest": "data/jobs.jsonl",
        "v1_config": "configs/v1.json",
        "v2_config": "configs/v2",
        "v2_sqlite": "configs/v2/quant_config_v2.sqlite3",
        "migration_report": "configs/migration_v1_to_v2.json",
        "array_hashes": array_hashes,
        "config_active_records": [config.config_key for config in CONFIGS],
        "config_tombstones": [TOMBSTONE.config_key],
    }
    atomic_write_json(root / "data" / "generation_report.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic operator diagnostics samples")
    parser.add_argument("--root", default=None, help="workspace root; defaults to this script's directory")
    args = parser.parse_args()
    root = Path(args.root) if args.root else Path(__file__).resolve().parent
    summary = generate(root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
