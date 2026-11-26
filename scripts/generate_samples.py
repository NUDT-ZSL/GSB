"""固定种子样本生成脚本。

同一种子多次运行逐字节生成相同的本地样本：任务清单 JSONL、状态数组、
两代配置快照，以及用于失效传播的三类变更清单。全程离线、无外部依赖。
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statediag.model import TransformConfig  # noqa: E402
from statediag.snapshots import write_v1, write_v2  # noqa: E402
from statediag.transforms import array_checksum, generate_state  # noqa: E402

MASTER_SEED = 20261127
STATE_DIM = 12
ORIGIN_MS = 1_000_000


def _write_jsonl(path, rows):
    lines = [json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def base_tasks():
    return [
        {"task_id": "t00", "state_seed": 101, "num_steps": 4,
         "block_size": 2, "quant_scale": 100, "revision": 1,
         "logical_clock_ms": 1000},
        {"task_id": "t01", "state_seed": 202, "num_steps": 5,
         "block_size": 3, "quant_scale": 100, "revision": 1,
         "logical_clock_ms": 2000},
        {"task_id": "t02", "state_seed": 303, "num_steps": 6,
         "block_size": 4, "quant_scale": 1000, "revision": 2,
         "logical_clock_ms": 3000},
        {"task_id": "t03", "state_seed": 404, "num_steps": 3,
         "block_size": 6, "quant_scale": 1000, "revision": 2,
         "logical_clock_ms": 4000},
        {"task_id": "t04", "state_seed": 505, "num_steps": 5,
         "block_size": 2, "quant_scale": 100, "revision": 3,
         "logical_clock_ms": 5000},
        {"task_id": "t05", "state_seed": 606, "num_steps": 4,
         "block_size": 4, "quant_scale": 1000, "revision": 3,
         "logical_clock_ms": 6000},
    ]


def main(out_dir):
    rng = np.random.default_rng(MASTER_SEED)  # 仅用于演示性派生，主状态仍逐种子生成
    _ = rng.integers(0, 10)
    os.makedirs(out_dir, exist_ok=True)
    state_dir = os.path.join(out_dir, "states")
    os.makedirs(state_dir, exist_ok=True)

    tasks = base_tasks()
    manifest_path = os.path.join(out_dir, "tasks.jsonl")
    _write_jsonl(manifest_path, tasks)

    manifest_index = []
    for row in tasks:
        state = generate_state(row["state_seed"], STATE_DIM)
        state_path = os.path.join(state_dir, f"{row['task_id']}.npy")
        np.save(state_path, state)
        manifest_index.append(
            {
                "task_id": row["task_id"],
                "state_file": os.path.relpath(state_path, out_dir),
                "dim": STATE_DIM,
                "checksum": array_checksum(state),
            }
        )
    with open(os.path.join(out_dir, "states.index.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest_index, handle, sort_keys=True, indent=2,
                  ensure_ascii=False)

    configs = {
        "float64": TransformConfig(
            algebra=1, precision="float64", rounding_mode="half-even",
            tolerance_abs=1e-12, tolerance_rel=1e-9,
            effective_ms=ORIGIN_MS + 5000),
        "float32": TransformConfig(
            algebra=1, precision="float32", rounding_mode="half-even",
            tolerance_abs=1e-5, tolerance_rel=1e-4,
            effective_ms=ORIGIN_MS + 5000),
        "fixed": TransformConfig(
            algebra=1, precision="fixed", rounding_mode="half-even",
            tolerance_abs=1e-1, tolerance_rel=5e-2,
            effective_ms=ORIGIN_MS + 5000),
        "fixed_strict": TransformConfig(
            algebra=1, precision="fixed", rounding_mode="half-even",
            tolerance_abs=1e-9, tolerance_rel=1e-9,
            effective_ms=ORIGIN_MS + 5000),
    }
    snap_dir = os.path.join(out_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    write_v1(os.path.join(snap_dir, "v1"), configs["float64"],
             origin_ms=ORIGIN_MS)
    tombstones = [
        {"tombstone_id": "tb_algebra0_legacy_quant", "algebra": 1,
         "reason": "legacy quant operator retired"},
    ]
    write_v2(os.path.join(snap_dir, "v2"), configs["float64"],
             tombstones=tombstones)

    # 情形一：仅 revision 提升（t03 及之后后缀）。
    revision_tasks = [dict(row) for row in tasks]
    for row in revision_tasks:
        if row["task_id"] in ("t03", "t04", "t05"):
            row["revision"] += 1
    _write_jsonl(os.path.join(out_dir, "tasks_revision.jsonl"), revision_tasks)

    # 情形二：仅配置代数提升（任务内容不变）。
    algebra_config = TransformConfig(
        algebra=2, precision="float64", rounding_mode="half-even",
        tolerance_abs=1e-12, tolerance_rel=1e-9,
        effective_ms=ORIGIN_MS + 9000)
    write_v2(os.path.join(snap_dir, "v2_algebra2"), algebra_config,
             tombstones=tombstones)

    # 情形三：block_size 变化（t02 由 4 变为 6，仍整除 12）。
    block_tasks = [dict(row) for row in tasks]
    for row in block_tasks:
        if row["task_id"] == "t02":
            row["block_size"] = 6
    _write_jsonl(os.path.join(out_dir, "tasks_blocksize.jsonl"), block_tasks)

    meta = {
        "master_seed": MASTER_SEED,
        "state_dim": STATE_DIM,
        "origin_ms": ORIGIN_MS,
        "tasks_file": "tasks.jsonl",
        "precision_configs": {k: v.to_dict() for k, v in configs.items()},
        "change_manifests": {
            "revision_bump": "tasks_revision.jsonl",
            "algebra_bump_snapshot": "snapshots/v2_algebra2",
            "block_size_change": "tasks_blocksize.jsonl",
        },
    }
    with open(os.path.join(out_dir, "sample_meta.json"), "w",
              encoding="utf-8") as handle:
        json.dump(meta, handle, sort_keys=True, indent=2, ensure_ascii=False)
    print(f"样本已生成于 {os.path.abspath(out_dir)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成确定性本地样本")
    parser.add_argument("--out", default="data/sample")
    args = parser.parse_args()
    main(args.out)
