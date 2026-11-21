"""确定性样本生成：任务清单 JSONL 与待注入异常参数文件。"""

from __future__ import annotations

import json
from pathlib import Path

from latent_diag.tasks import TaskSpec, write_manifest

# 正常任务：覆盖三种精度档、三种解码模式、多种分块网格
_NORMAL_TASKS = [
    TaskSpec("task-alpha", "a red cube on a blue plane", 101,
             [4, 32, 32], [2, 2], 6, 1.0, "tanh", 1000.0, "half_even",
             "float64", "whole"),
    TaskSpec("task-beta", "mist over a quiet lake", 202,
             [4, 32, 32], [4, 4], 5, 0.8, "sigmoid", 500.0, "half_up",
             "float32", "whole"),
    TaskSpec("task-gamma", "low poly mountain at dawn", 303,
             [4, 32, 32], [2, 4], 7, 1.2, "linear", 250.0, "floor",
             "fixed", "whole"),
    TaskSpec("task-delta", "chrome sphere in fog", 404,
             [4, 32, 32], [4, 2], 4, 0.6, "tanh", 100.0, "trunc",
             "fixed", "whole"),
    TaskSpec("task-epsilon", "paper crane on a desk", 505,
             [4, 32, 32], [8, 8], 8, 1.5, "sigmoid", 2000.0, "half_even",
             "float32", "whole"),
    TaskSpec("task-zeta", "neon alley after rain", 606,
             [4, 32, 32], [1, 1], 3, 0.9, "linear", 800.0, "half_up",
             "float64", "whole"),
]

# 注入任务：参数本身合法，异常由 anomalies.json 覆盖触发
_INJECT_TASKS = [
    TaskSpec("task-inj-seed", "broken seed case", 700,
             [4, 32, 32], [2, 2], 4, 1.0, "tanh", 1000.0, "half_even",
             "float64", "whole"),
    TaskSpec("task-inj-shape", "broken shape case", 701,
             [4, 32, 32], [2, 2], 4, 1.0, "tanh", 1000.0, "half_even",
             "float64", "whole"),
    TaskSpec("task-inj-grid", "indivisible grid case", 702,
             [4, 32, 32], [2, 2], 4, 1.0, "tanh", 1000.0, "half_even",
             "float64", "whole"),
    TaskSpec("task-inj-scale", "bad quant scale case", 703,
             [4, 32, 32], [2, 2], 4, 1.0, "tanh", 1000.0, "half_even",
             "fixed", "whole"),
    TaskSpec("task-inj-round", "unknown rounding case", 704,
             [4, 32, 32], [2, 2], 4, 1.0, "tanh", 1000.0, "half_even",
             "fixed", "whole"),
]

# 异常参数文件：不整除的 block_grid、非正 quant_scale、未知 rounding_mode 等
_ANOMALIES = [
    {"task_id": "task-inj-seed",
     "overrides": {"prompt_seed": -7}},
    {"task_id": "task-inj-shape",
     "overrides": {"latent_shape": [4, 0, 32]}},
    {"task_id": "task-inj-grid",
     "overrides": {"block_grid": [3, 3]}},
    {"task_id": "task-inj-scale",
     "overrides": {"quant_scale": -100.0}},
    {"task_id": "task-inj-round",
     "overrides": {"rounding_mode": "round_magic"}},
]


def generate(sample_dir="samples"):
    """生成清单与异常参数文件，返回 (manifest_path, anomalies_path)。"""
    sample_dir = Path(sample_dir)
    sample_dir.mkdir(parents=True, exist_ok=True)
    tasks = _NORMAL_TASKS + _INJECT_TASKS
    manifest = sample_dir / "tasks.jsonl"
    write_manifest(tasks, manifest)
    anomalies = sample_dir / "anomalies.json"
    with open(anomalies, "w", encoding="utf-8") as fh:
        json.dump(_ANOMALIES, fh, indent=2, sort_keys=True, ensure_ascii=False)
    return str(manifest), str(anomalies)


if __name__ == "__main__":
    m, a = generate()
    print(f"manifest={m}\nanomalies={a}")
