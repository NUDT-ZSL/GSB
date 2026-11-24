"""规范化序列化与内容摘要工具(全项目统一的确定性编码)."""

from __future__ import annotations

import hashlib
import json


def canonical(obj) -> bytes:
    """确定性 JSON 编码: 键排序、无空白、UTF-8."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_obj(obj) -> str:
    return sha256_bytes(canonical(obj))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
