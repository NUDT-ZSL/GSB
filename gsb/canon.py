"""规范化 JSON、稳定排序输出与内容摘要。"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """把 Decimal 等类型转换为 JSON 安全表示（Decimal -> 字符串以保精度）。"""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def canonical_bytes(obj: Any) -> bytes:
    """跨进程/重复运行稳定的规范字节：键排序、紧凑分隔、Decimal 字符串。"""
    return json.dumps(
        to_jsonable(obj),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_text(obj: Any) -> str:
    return canonical_bytes(obj).decode("utf-8")


def pretty_json(obj: Any) -> str:
    """status 等命令使用的稳定可读 JSON（键排序 + 缩进）。"""
    return json.dumps(
        to_jsonable(obj),
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()
