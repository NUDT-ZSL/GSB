"""生成任务清单（JSONL）的加载与确定性生成。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TASK_FIELDS = (
    "task_id", "prompt", "prompt_seed", "latent_shape", "block_grid",
    "sampler_steps", "noise_scale", "decode_mode", "quant_scale",
    "rounding_mode", "dtype_profile", "ref_mode",
)


@dataclass
class TaskSpec:
    task_id: str
    prompt: str
    prompt_seed: int
    latent_shape: list          # [C, H, W]
    block_grid: list            # [gh, gw]
    sampler_steps: int
    noise_scale: float
    decode_mode: str            # tanh | sigmoid | linear
    quant_scale: float
    rounding_mode: str
    dtype_profile: str          # float64 | float32 | fixed
    ref_mode: str = "whole"
    # 运行期附加（不计入清单语义）：
    degraded: bool = field(default=False, compare=False)
    degradation_category: str = field(default=None, compare=False)
    original_block_grid: list = field(default=None, compare=False)

    @staticmethod
    def from_dict(data):
        return TaskSpec(**{k: data[k] for k in TASK_FIELDS})

    def to_dict(self):
        return {k: getattr(self, k) for k in TASK_FIELDS}


def load_manifest(path):
    tasks = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            missing = [k for k in TASK_FIELDS if k not in data]
            if missing:
                raise ValueError(f"manifest line {line_no} missing fields: {missing}")
            tasks.append(TaskSpec.from_dict(data))
    return tasks


def write_manifest(tasks, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for task in tasks:
            fh.write(json.dumps(task.to_dict(), sort_keys=True) + "\n")
