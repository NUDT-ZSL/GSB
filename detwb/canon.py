"""规范化序列化与哈希工具：保证跨运行逐字节一致。"""

import hashlib
import json


def norm(obj):
    """递归规范化：float 统一保留 6 位小数，dict 键排序由 dumps 保证。"""
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [norm(v) for v in obj]
    return obj


def canonical_json(obj):
    return json.dumps(norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj):
    return sha256_text(canonical_json(obj))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()
