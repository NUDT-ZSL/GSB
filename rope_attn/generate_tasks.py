"""生成本地确定性任务清单 JSONL（不依赖任何外部数据）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import validate_tasks


DEFAULT_TASKS = [
    {
        "case_id": "case_std_001",
        "seq_len": 16,
        "head_dim": 8,
        "num_heads": 2,
        "base_theta": 10000.0,
        "pos_encoding_variant": "standard",
        "pos_scale": 1.0,
        "kv_revision": 1,
        "block_size": 4,
        "layers": 2,
        "causal": True,
        "seed": 20261124,
        "dtype": "float32",
    },
    {
        "case_id": "case_linear_002",
        "seq_len": 12,
        "head_dim": 16,
        "num_heads": 3,
        "base_theta": 5000.0,
        "pos_encoding_variant": "linear",
        "pos_scale": 0.5,
        "kv_revision": 1,
        "block_size": 6,
        "layers": 2,
        "causal": True,
        "seed": 20261125,
        "dtype": "float64",
    },
    {
        "case_id": "case_ntk_003",
        "seq_len": 10,
        "head_dim": 8,
        "num_heads": 2,
        "base_theta": 10000.0,
        "pos_encoding_variant": "ntk",
        "pos_scale": 2.0,
        "kv_revision": 1,
        "block_size": 3,
        "layers": 1,
        "causal": False,
        "seed": 20261126,
        "dtype": "float32",
    },
]


def write_tasks(path: str, tasks=None) -> None:
    tasks = DEFAULT_TASKS if tasks is None else tasks
    validate_tasks(tasks)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成确定性任务清单")
    parser.add_argument("--out", default="tasks.jsonl")
    args = parser.parse_args(argv)
    write_tasks(args.out)
    print(f"已写入 {args.out}，共 {len(DEFAULT_TASKS)} 条任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
