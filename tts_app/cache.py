"""内容寻址缓存：键由规范化键组成 JSON 的 sha256 派生，条目可反查键组成。"""

import hashlib
import json
import os
import pickle

from .errors import E_RESOURCE_LIMIT, TtsError


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def make_key(components):
    """由键组成字典构造缓存键。"""
    return hashlib.sha256(canonical_json(components).encode("utf-8")).hexdigest()


class ContentCache:
    """本地目录缓存：每个条目 = <key>.meta（键组成）+ <key>.bin（产物）。"""

    def __init__(self, root, max_entries=10000, on_limit="error"):
        self.root = root
        self.max_entries = max_entries
        self.on_limit = on_limit
        self.degraded = []
        os.makedirs(root, exist_ok=True)

    def _paths(self, key):
        return (os.path.join(self.root, key + ".meta"),
                os.path.join(self.root, key + ".bin"))

    def _entry_count(self):
        return sum(1 for f in os.listdir(self.root) if f.endswith(".meta"))

    def get(self, key):
        meta_path, bin_path = self._paths(key)
        if os.path.exists(meta_path) and os.path.exists(bin_path):
            with open(bin_path, "rb") as fh:
                return pickle.load(fh)
        return None

    def put(self, key, components, payload):
        if self.get(key) is not None:
            return key
        if self._entry_count() >= self.max_entries:
            if self.on_limit == "evict":
                self._evict_oldest()
                self.degraded.append("cache_evict")
            else:
                raise TtsError(E_RESOURCE_LIMIT, "缓存条目总量超限",
                               limit=self.max_entries)
        meta_path, bin_path = self._paths(key)
        with open(bin_path, "wb") as fh:
            pickle.dump(payload, fh, protocol=4)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "components": components}, fh,
                      ensure_ascii=False, indent=1, sort_keys=True)
        return key

    def _evict_oldest(self):
        metas = [f for f in os.listdir(self.root) if f.endswith(".meta")]
        metas.sort(key=lambda f: os.path.getmtime(os.path.join(self.root, f)))
        victim = metas[0][:-5]
        for path in self._paths(victim):
            if os.path.exists(path):
                os.remove(path)

    def describe(self, key):
        """反查缓存条目的键组成。"""
        meta_path, _ = self._paths(key)
        if not os.path.exists(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def stats(self):
        return {"entries": self._entry_count(),
                "max_entries": self.max_entries,
                "degraded": list(self.degraded)}
