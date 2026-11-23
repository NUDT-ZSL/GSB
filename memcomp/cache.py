"""三类节点的内容寻址缓存与键构造。

缓存键组成：
- 窗口键：会话内窗口跨度内容哈希（会话内容哈希的窗口级粒度）+ 受控词表版本 + 窗口参数版本
- 摘要键：上游窗口键 + 摘要参数 + 受控词表版本
- 条目键：上游成员摘要键集合 + 合并参数版本
"""

import hashlib
import json
from pathlib import Path

from .merge import MERGE_VERSION
from .vocab import VOCAB_VERSION

NODE_TYPES = ("windows", "summaries", "entries")


def content_hash(*parts):
    blob = json.dumps(list(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def window_cache_key(window, spec, vocab_version=VOCAB_VERSION):
    span_hash = hashlib.sha256(
        json.dumps(list(window.tokens), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return content_hash(
        "window",
        window.session_id,
        window.token_start,
        window.token_end,
        span_hash,
        vocab_version,
        spec.param_version(),
    )


def summary_cache_key(window_key, spec, vocab_version=VOCAB_VERSION):
    return content_hash("summary", window_key, spec.summary_k, vocab_version)


def entry_cache_key(member_summary_keys, spec):
    return content_hash(
        "entry", sorted(member_summary_keys), MERGE_VERSION, spec.entry_window_span
    )


class CacheStore:
    """落盘的内容寻址缓存，root 由调用方通过命令行参数传入。"""

    def __init__(self, root):
        self.root = Path(root) / "cache"
        for node_type in NODE_TYPES:
            (self.root / node_type).mkdir(parents=True, exist_ok=True)

    def _path(self, node_type, key):
        return self.root / node_type / f"{key}.json"

    def has(self, node_type, key):
        return self._path(node_type, key).exists()

    def get(self, node_type, key):
        path = self._path(node_type, key)
        if not path.exists():
            return None
        return path.read_bytes()

    def put(self, node_type, key, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._path(node_type, key).write_bytes(payload)
