"""确定性任务清单与特征/权重数组生成。

所有数据由固定种子本地生成，无网络、无外部数据集：

* :class:`TaskSpec` —— JSONL 任务描述（task_id、source_domain、target_domain、
  adapter_kind、input_dim、sample_count、transfer_strength、seed、priority）；
* :func:`generate_task_catalog` —— 生成任务清单并落盘 JSONL；
* :func:`domain_statistics` —— 按域名派生的域级统计量，同域同维度全局一致、
  可在评估阶段作为只读数据共享；
* :func:`generate_task_arrays` —— 任务特征与真值权重数组。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .determinism import derive_seed, numpy_rng

ADAPTER_KINDS = ("linear", "lora_light", "adapter_pro")
DOMAINS = ("domain_a", "domain_b", "domain_c", "domain_d", "domain_e")


@dataclass(frozen=True)
class TaskSpec:
    """单个适配任务的描述，字段与 JSONL 行一一对应。"""

    task_id: str
    source_domain: str
    target_domain: str
    adapter_kind: str
    input_dim: int
    sample_count: int
    transfer_strength: float
    seed: int
    priority: int

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_json_line(line: str) -> "TaskSpec":
        payload = json.loads(line)
        return TaskSpec(**payload)


def write_catalog(tasks: list[TaskSpec], path: str | Path) -> Path:
    """按给定顺序写 JSONL（调用方负责排序，通常按 task_id 升序）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for task in tasks:
            handle.write(task.to_json_line())
            handle.write("\n")
    return path


def read_catalog(path: str | Path) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                tasks.append(TaskSpec.from_json_line(line))
    return tasks


def generate_task_catalog(
    master_seed: int,
    task_count: int = 8,
    input_dims: tuple[int, ...] = (8, 12, 16),
    sample_choices: tuple[int, ...] = (120, 200, 320, 480),
    near_threshold: bool = True,
) -> list[TaskSpec]:
    """由主种子确定性生成任务清单（按 task_id 升序返回）。

    ``near_threshold=True`` 时会刻意构造迁移强度落在 0.5 门限附近的任务，
    用于演示门限稳定性保护。
    """
    rng = numpy_rng("catalog", master_seed)
    tasks: list[TaskSpec] = []
    for index in range(task_count):
        task_id = f"task_{index:03d}"
        source = DOMAINS[int(rng.integers(0, len(DOMAINS)))]
        target = DOMAINS[int(rng.integers(0, len(DOMAINS)))]
        while target == source:
            target = DOMAINS[int(rng.integers(0, len(DOMAINS)))]
        adapter = ADAPTER_KINDS[int(rng.integers(0, len(ADAPTER_KINDS)))]
        input_dim = int(input_dims[int(rng.integers(0, len(input_dims)))])
        sample_count = int(sample_choices[int(rng.integers(0, len(sample_choices)))])
        strength = float(rng.uniform(0.1, 0.95))
        if near_threshold and index in (1, 4):
            # 恰好压在 0.5 门限上 / 落在 1e-9 容差带内，用于稳定性演示。
            strength = 0.5 if index == 1 else 0.5 + 1e-12
        priority = int(rng.integers(1, 6))  # 1 最高，5 最低
        seed = int(derive_seed("task-seed", master_seed, task_id))
        tasks.append(
            TaskSpec(
                task_id=task_id,
                source_domain=source,
                target_domain=target,
                adapter_kind=adapter,
                input_dim=input_dim,
                sample_count=sample_count,
                transfer_strength=float(strength),
                seed=seed,
                priority=priority,
            )
        )
    tasks.sort(key=lambda item: item.task_id)
    return tasks


def domain_statistics(domain: str, input_dim: int) -> dict[str, np.ndarray]:
    """域级只读统计量：均值向量与标准差向量。

    同 ``(domain, input_dim)`` 在任何进程、任何时间调用结果逐字节一致，
    因此同 target_domain 的评估统计量可全局共享、只计一次驻留占用。
    """
    rng = numpy_rng("domain-stats", domain, input_dim)
    mean = rng.normal(loc=0.0, scale=2.0, size=input_dim).astype(np.float64)
    raw_scale = rng.uniform(0.5, 1.5, size=input_dim)
    std = raw_scale.astype(np.float64)
    return {"mean": mean, "std": std}


def generate_task_arrays(
    task: TaskSpec,
    dtype: np.dtype = np.float64,
) -> dict[str, np.ndarray]:
    """生成任务的源域特征矩阵 X 与真值权重 w。

    特征由任务私有种子生成，叠加源域统计量形成域偏移；
    权重数组由独立派生子种子生成，保证两数组相关但不复用随机流。
    """
    rng = numpy_rng("task-arrays", task.seed, task.task_id)
    source_stats = domain_statistics(task.source_domain, task.input_dim)
    standard = rng.normal(size=(task.sample_count, task.input_dim))
    features = standard * source_stats["std"] + source_stats["mean"]
    weight_rng = numpy_rng("task-weights", task.seed, task.task_id)
    weights = weight_rng.normal(size=task.input_dim) / np.sqrt(task.input_dim)
    return {
        "features": np.ascontiguousarray(features, dtype=dtype),
        "weights": np.ascontiguousarray(weights, dtype=dtype),
    }
