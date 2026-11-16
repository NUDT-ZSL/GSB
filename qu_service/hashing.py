"""确定性哈希与指纹工具。全项目禁止随机数，排序键一律由 sha256 派生。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical(obj) -> str:
    """对象 -> 规范 JSON 字符串（键排序、紧凑分隔符、保留中文）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(obj) -> str:
    """阶段结果指纹。"""
    return sha256_text(canonical(obj))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_sort_key(query_id: str, stage: str) -> str:
    """由查询 ID 与阶段名派生的稳定排序键。"""
    return sha256_text(f"{query_id}::{stage}")


def unit_interval_from_id(query_id: str) -> float:
    """把查询 ID 确定性地映射到 [0,1) 区间，用于畸形查询比例注入。"""
    return int(sha256_text(query_id)[:16], 16) / float(1 << 64)
