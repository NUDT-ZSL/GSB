"""算子中间产物的内容寻址缓存。

目录布局（全部在本地文件系统，无外部服务）：
    cache_dir/
        blobs/<sha256>.npz      内容寻址的 ndarray 产物；
        manifest.json           key_hash -> 条目（原子替换写）；
        manifest.tmp.<pid>      临时文件，提交后删除。

条目内容：节点 ID、缓存键组成、产物 blob 哈希、dtype/形状、创建版本。
键相同必须复用 blob；键不同必然得到不同 key_hash 从而重算。
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import numpy as np

from .hashing import canonical_json, digest_array, digest_obj
from .nodes import CacheKey


def atomic_write_text(path: Path, text: str) -> None:
    """先落同目录临时文件再 os.replace 原子替换（Windows/Linux 均支持）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


@dataclass
class CacheEntry:
    key_hash: str
    node: str
    key_payload: dict
    blob_hash: str
    shape: List[int]
    dtype: str

    @classmethod
    def from_dict(cls, payload: dict) -> "CacheEntry":
        return cls(
            key_hash=payload["key_hash"],
            node=payload["node"],
            key_payload=payload["key_payload"],
            blob_hash=payload["blob_hash"],
            shape=payload["shape"],
            dtype=payload["dtype"],
        )

    def to_dict(self) -> dict:
        return {
            "key_hash": self.key_hash,
            "node": self.node,
            "key_payload": self.key_payload,
            "blob_hash": self.blob_hash,
            "shape": self.shape,
            "dtype": self.dtype,
        }


class ContentCache:
    """进程内加锁的内容寻址缓存；多进程通过原子 manifest 协调。"""

    def __init__(self, cache_dir: str) -> None:
        self.root = Path(cache_dir)
        self.blobs = self.root / "blobs"
        self.manifest_path = self.root / "manifest.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".cache.lock"
        self._manifest: Dict[str, dict] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, dict]:
        if self.manifest_path.exists():
            with open(self.manifest_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return {}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """简单的跨进程文件锁（Windows msvcrt / POSIX fcntl 分支）。"""
        handle = open(self._lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._manifest = self._load_manifest()
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def key_hash_of(cache_key: CacheKey) -> str:
        return digest_obj(cache_key.to_dict())

    def lookup(self, cache_key: CacheKey) -> Optional[CacheEntry]:
        key_hash = self.key_hash_of(cache_key)
        with self._locked():
            payload = self._manifest.get(key_hash)
            return CacheEntry.from_dict(payload) if payload else None

    def contains(self, cache_key: CacheKey) -> bool:
        return self.lookup(cache_key) is not None

    def put(self, cache_key: CacheKey, array: np.ndarray) -> CacheEntry:
        key_hash = self.key_hash_of(cache_key)
        blob_hash = digest_array(array)
        blob_path = self.blobs / f"{blob_hash}.npz"
        if not blob_path.exists():
            fd, tmp_name = tempfile.mkstemp(
                prefix=blob_path.name + ".", suffix=".tmp", dir=str(self.blobs)
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    np.savez_compressed(handle, data=array)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, blob_path)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
        entry = CacheEntry(
            key_hash=key_hash,
            node=cache_key.node,
            key_payload=cache_key.to_dict(),
            blob_hash=blob_hash,
            shape=list(array.shape),
            dtype=str(array.dtype),
        )
        with self._locked():
            self._manifest = self._load_manifest()
            self._manifest[key_hash] = entry.to_dict()
            self._flush_manifest()
        return entry

    def get_array(self, entry: CacheEntry) -> np.ndarray:
        blob_path = self.blobs / f"{entry.blob_hash}.npz"
        if not blob_path.exists():
            raise FileNotFoundError(
                f"缓存产物丢失：node={entry.node} blob={entry.blob_hash}"
            )
        with np.load(blob_path) as data:
            return np.ascontiguousarray(data["data"])

    def get_or_compute(
        self, cache_key: CacheKey, compute
    ) -> tuple[np.ndarray, CacheEntry, bool]:
        """键相同复用，键不同重算。返回 (数组, 条目, 是否命中)。"""
        existing = self.lookup(cache_key)
        if existing is not None:
            return self.get_array(existing), existing, True
        array = np.ascontiguousarray(compute())
        entry = self.put(cache_key, array)
        return array, entry, False

    def _flush_manifest(self) -> None:
        atomic_write_text(
            self.manifest_path,
            canonical_json(self._manifest),
        )

    def entries(self) -> List[CacheEntry]:
        with self._locked():
            return [CacheEntry.from_dict(item) for item in self._manifest.values()]

    def entry_count(self) -> int:
        with self._locked():
            return len(self._manifest)

    def inspect_node(
        self, case_id: str, block: Optional[int] = None
    ) -> List[dict]:
        """按 case_id（可再按块序号）查询键组成与产物哈希。"""
        result = []
        with self._locked():
            for payload in self._manifest.values():
                node = payload["node"]
                if not node.startswith(f"{case_id}/"):
                    continue
                if block is not None:
                    if f"_b/{block}" not in node:
                        continue
                result.append(
                    {
                        "node": node,
                        "key_hash": payload["key_hash"],
                        "blob_hash": payload["blob_hash"],
                        "key_payload": payload["key_payload"],
                        "shape": payload["shape"],
                        "dtype": payload["dtype"],
                    }
                )
        result.sort(key=lambda item: item["node"])
        return result

    def verify_entry(self, entry: CacheEntry) -> dict:
        """重新计算数组内容哈希并与 manifest 核对，返回核对结果。"""
        array = self.get_array(entry)
        content_hash = digest_array(array)
        return {
            "node": entry.node,
            "blob_hash_declared": entry.blob_hash,
            "array_content_hash": content_hash,
            "content_ok": content_hash == entry.blob_hash,
        }

    def all_node_entries(self) -> Dict[str, CacheEntry]:
        """节点名 -> 最新一代条目（同节点多代时保留最后写入的一代）。

        manifest 以 key_hash 为主键，因此同节点不同键会保留多代；
        这里按节点名归并，调用方需要全量多代时使用 :meth:`entries`。
        """
        result: Dict[str, CacheEntry] = {}
        for entry in self.entries():
            result[entry.node] = entry
        return result

    def entries_for_node(self, node: str) -> List[CacheEntry]:
        """返回某节点名的全部代条目。"""
        return [entry for entry in self.entries() if entry.node == node]
