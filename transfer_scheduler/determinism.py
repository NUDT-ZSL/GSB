"""确定性基础工具。

* :func:`derive_seed` —— 基于 splitmix64 的纯 Python 种子派生，跨平台、
  跨 Python 版本稳定，不依赖 PYTHONHASHSEED；
* :func:`numpy_rng` —— 由派生种子构造 :class:`numpy.random.Generator`；
* :func:`stable_hash` / :func:`fingerprint_arrays` —— 字节级稳定摘要，
  用于逐字节一致性校验与快照校验和。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import numpy as np

_MASK64 = (1 << 64) - 1


def splitmix64(state: int) -> int:
    """splitmix64 单步混合，输入输出均为 64 位非负整数。"""
    state = (state + 0x9E3779B97F4A7C15) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return z ^ (z >> 31)


def derive_seed(*parts: Any) -> int:
    """把任意可 ``repr`` 的输入混合为确定性的 64 位种子。

    顺序敏感（同一组参数顺序不同得到不同种子），但与字典遍历顺序无关：
    调用方必须先对无序集合自行排序后再传入。
    """
    state = 0x123456789ABCDEF0
    for part in parts:
        token = repr(part).encode("utf-8")
        digest = hashlib.blake2b(token, digest_size=8).digest()
        token_int = int.from_bytes(digest, "little", signed=False)
        state = splitmix64(state ^ token_int)
    return state


def numpy_rng(*parts: Any) -> np.random.Generator:
    """由任意标识参数构造确定性 NumPy 随机数生成器。"""
    return np.random.default_rng(derive_seed(*parts))


def stable_hash(value: Any) -> str:
    """对可 JSON 化结构计算稳定 blake2b 摘要（十六进制）。"""
    payload = canonical_json(value).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def fingerprint_arrays(arrays: Iterable[np.ndarray]) -> str:
    """对一组数组按给定顺序计算字节级指纹。

    数组以连续内存序列化，形状、dtype 与字节内容均参与摘要，
    可用于逐字节一致性断言。
    """
    hasher = hashlib.blake2b(digest_size=16)
    for index, array in enumerate(arrays):
        arr = np.ascontiguousarray(array)
        hasher.update(str(index).encode("ascii"))
        hasher.update(str(arr.shape).encode("ascii"))
        hasher.update(str(arr.dtype).encode("ascii"))
        hasher.update(arr.tobytes())
    return hasher.hexdigest()


def canonical_json(value: Any) -> str:
    """递归排序键的 JSON 序列化，保证同内容逐字节一致。"""
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
