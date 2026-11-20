"""确定性序列化与校验和工具（保证逐字节一致的基础）。"""

import hashlib
import json

import numpy as np


def to_native(obj):
    """把 NumPy 类型递归转换为原生 Python 类型。"""
    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def canonical_bytes(obj) -> bytes:
    """规范 JSON 字节：键排序、无空格、ASCII 安全，浮点保留 round-trip 精度。"""
    native = to_native(obj)
    return json.dumps(
        native, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def checksum(obj) -> str:
    """对任意可序列化对象计算 SHA-256 内容校验和。"""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def array_bytes(arr: np.ndarray) -> bytes:
    """NumPy 数组的原始字节（同形态同 dtype 下逐字节可比）。"""
    return np.ascontiguousarray(arr).tobytes()

