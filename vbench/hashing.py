"""稳定哈希、内容指纹与确定性随机源。

不依赖 hash() 内建随机化；所有派生均基于 sha256，保证跨进程/跨平台一致。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def stable_hash(*parts: Any) -> str:
    """将任意可 JSON 序列化部件组合成 sha256 十六进制摘要。"""
    digest = hashlib.sha256()
    for part in parts:
        token = json.dumps(part, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest.update(token.encode("utf-8"))
        digest.update(b"|")
    return digest.hexdigest()


def int_seed(*parts: Any) -> int:
    """派生 [0, 2**63) 内的整数种子。"""
    return int(stable_hash(*parts)[:15], 16)


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_array(array: np.ndarray) -> str:
    """对 numpy 数组做内容指纹（含 dtype 与形状，按小端字节序）。"""
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.shape).encode("utf-8"))
    digest.update(b"|")
    digest.update(contiguous.dtype.str.encode("utf-8"))
    digest.update(b"|")
    digest.update(contiguous.astype("<f4", copy=False).tobytes()
                  if np.issubdtype(contiguous.dtype, np.floating)
                  else contiguous.tobytes())
    return digest.hexdigest()


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DeterministicRNG:
    """以命名部件派生的确定性 PCG 风格随机源（numpy PCG64，跨版本稳定）。"""

    def __init__(self, *parts: Any) -> None:
        self.seed = int_seed(*parts)
        self._ss = np.random.Generator(np.random.PCG64(self.seed))

    def normal(self, shape: Iterable[int], scale: float = 1.0) -> np.ndarray:
        return self._ss.normal(0.0, scale, tuple(shape)).astype(np.float32)

    def uniform(self, shape: Iterable[int], low: float = 0.0, high: float = 1.0) -> np.ndarray:
        return self._ss.uniform(low, high, tuple(shape)).astype(np.float32)

    def integers(self, low: int, high: int) -> int:
        return int(self._ss.integers(low, high))
