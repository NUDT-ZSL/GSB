"""固定种子驱动的 Q/K/V 确定性生成与 K/V 修订。

所有随机数只来自 NumPy ``default_rng``，种子由任务 seed、层号与
kv_revision 派生；不依赖任何真实模型或外部服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

from .config import Task
from .errors import RevisionRollbackError


def _rng(seed: int, *parts: int) -> np.random.Generator:
    # SeedSequence 把各部分混入派生流，保证层与修订之间互不相关且可复现。
    sequence = np.random.SeedSequence([int(seed), *[int(part) for part in parts]])
    return np.random.default_rng(sequence)


def qkv_shape(task: Task) -> Tuple[int, int, int]:
    return (task.seq_len, task.num_heads, task.head_dim)


def generate_q(task: Task, layer: int) -> np.ndarray:
    rng = _rng(task.seed, layer, 0x71)
    array = rng.normal(size=qkv_shape(task)).astype(task.dtype)
    return np.ascontiguousarray(array)


def generate_kv(
    task: Task, layer: int, kv_revision: int
) -> Tuple[np.ndarray, np.ndarray]:
    """按修订号生成 K/V。修订号进入随机流，同修订号逐字节一致。"""
    rng_k = _rng(task.seed, layer, 0x4B, kv_revision)
    rng_v = _rng(task.seed, layer, 0x56, kv_revision)
    key = rng_k.normal(size=qkv_shape(task)).astype(task.dtype)
    value = rng_v.normal(size=qkv_shape(task)).astype(task.dtype)
    return np.ascontiguousarray(key), np.ascontiguousarray(value)


@dataclass
class KVRevision:
    """某 case 某层的一次 K/V 修订记录。"""

    kv_revision: int
    key: np.ndarray
    value: np.ndarray


class KVRegistry:
    """case_id -> layer -> 最新 K/V 修订的本地注册表。"""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[int, KVRevision]] = {}

    def put(self, task: Task, layer: int, kv_revision: int) -> KVRevision:
        existing = self._store.setdefault(task.case_id, {}).get(layer)
        if existing is not None and kv_revision < existing.kv_revision:
            raise RevisionRollbackError(
                f"kv_revision 回退：已存修订 {existing.kv_revision}，"
                f"收到 {kv_revision}",
                field="kv_revision",
                case_id=task.case_id,
                layer=layer,
            )
        key, value = generate_kv(task, layer, kv_revision)
        record = KVRevision(kv_revision, key, value)
        self._store[task.case_id][layer] = record
        return record

    def get(self, task: Task, layer: int) -> KVRevision:
        case_store = self._store.setdefault(task.case_id, {})
        record = case_store.get(layer)
        if record is None or record.kv_revision != task.kv_revision:
            record = self.put(task, layer, task.kv_revision)
        return record
