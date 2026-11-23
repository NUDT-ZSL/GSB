"""确定性哈希与规范化 JSON 序列化工具。

跨进程、跨机器（同字节序与 NumPy 版本约束下）稳定：
    * 结构化参数使用 sort_keys 的紧凑 JSON（UTF-8、默认分隔符）；
    * ndarray 以 C 连续字节 + dtype/形状元信息入哈希；
    * 文件内容直接流式 SHA-256。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def canonical_json(obj: Any) -> str:
    """键排序、无多余空白的规范化 JSON 字符串。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_obj(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def digest_array(array: np.ndarray) -> str:
    """ndarray 内容哈希：纳入 dtype、形状与原始字节，保证逐字节口径。"""
    array = np.ascontiguousarray(array)
    hasher = hashlib.sha256()
    header = canonical_json(
        {"dtype": str(array.dtype), "shape": list(array.shape)}
    ).encode("utf-8")
    hasher.update(header)
    hasher.update(array.tobytes(order="C"))
    return hasher.hexdigest()


def digest_file(path: str, chunk_size: int = 1 << 20) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_iterable(values: Iterable[Any]) -> str:
    """对可迭代项逐个做规范化拼接哈希（顺序敏感）。"""
    hasher = hashlib.sha256()
    for value in values:
        hasher.update(canonical_json(value).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def short_hash(value: str, length: int = 16) -> str:
    return value[:length]
