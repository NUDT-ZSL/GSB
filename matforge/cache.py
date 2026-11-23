from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import fail
from .hashing import (
    array_digest,
    atomic_write_json,
    canonical_json,
    digest_bytes,
    digest_obj,
)
from .params import MaterialParams
from .pipeline import BlockTexture
from .plan import BlockPlan


def _npy_bytes(array: np.ndarray) -> bytes:
    import io

    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def _load_npy(path: Path) -> np.ndarray:
    try:
        return np.load(path, allow_pickle=False)
    except (OSError, ValueError) as error:
        fail("E_CACHE_CORRUPT", "缓存数组无法读取", path=str(path), reason=str(error))


@dataclass(frozen=True)
class CacheObject:
    cache_key: str
    content_hash: str
    metadata: dict[str, Any]
    channels: np.ndarray
    seam: np.ndarray
    path: str


class SliceCache:
    """Content-addressed disk cache with an atomic key index."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.index_path = self.root / "index.json"
        self.semantic_index_path = self.root / "semantic-index.json"
        self.objects.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] = {}
        self._semantic_index: dict[str, str] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.index_path.exists():
            return
        try:
            import json

            document = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            fail("E_CACHE_CORRUPT", "缓存索引损坏", path=str(self.index_path), reason=str(error))
        if not isinstance(document, dict) or document.get("schema") != "matforge.cache-index.v1":
            fail("E_CACHE_CORRUPT", "缓存索引 schema 不匹配", path=str(self.index_path))
        entries = document.get("entries")
        checksum = document.get("checksum")
        if not isinstance(entries, dict) or checksum != digest_obj(entries):
            fail("E_CACHE_CORRUPT", "缓存索引校验和不符", path=str(self.index_path))
        self._index = entries
        self._load_semantic_index()

    def _load_semantic_index(self) -> None:
        if not self.semantic_index_path.exists():
            return
        try:
            import json

            document = json.loads(self.semantic_index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            fail("E_CACHE_CORRUPT", "语义缓存索引损坏", path=str(self.semantic_index_path), reason=str(error))
        entries = document.get("entries")
        if not isinstance(entries, dict) or document.get("checksum") != digest_obj(entries):
            fail("E_CACHE_CORRUPT", "语义缓存索引校验和不符", path=str(self.semantic_index_path))
        self._semantic_index = entries

    def _save_index(self) -> None:
        entries = dict(sorted(self._index.items()))
        atomic_write_json(
            self.index_path,
            {"schema": "matforge.cache-index.v1", "entries": entries, "checksum": digest_obj(entries)},
        )
        semantic_entries = dict(sorted(self._semantic_index.items()))
        atomic_write_json(
            self.semantic_index_path,
            {"schema": "matforge.semantic-index.v1", "entries": semantic_entries, "checksum": digest_obj(semantic_entries)},
        )

    def _object_path(self, content_hash: str) -> Path:
        return self.objects / content_hash[:2] / content_hash

    def put(self, texture: BlockTexture, params: MaterialParams, plan: BlockPlan, plan_version: int) -> dict[str, Any]:
        channels_bytes = _npy_bytes(texture.channels)
        seam_bytes = _npy_bytes(texture.seam)
        payload_hash = digest_bytes(channels_bytes + seam_bytes)
        metadata = {
            "schema": "matforge.slice.v1",
            "cache_key": texture.cache_key,
            "block": texture.block.to_dict(),
            "pixel_range": texture.block.pixel_range,
            "channel_digest": texture.channel_digest,
            "seam_digest": texture.seam_digest,
            "payload_hash": payload_hash,
            "content_digest": texture.content_digest,
            "upstream_hash": texture.upstream_hash,
            "normalization_digest": texture.normalization_digest,
            "params_digest": params.digest(),
            "params_revision": params.revision,
            "plan_digest": plan.digest(),
            "plan_version": plan_version,
            "seed": params.seed,
            "octave_digest": params.noise.digest(),
            "node_path": texture.node_path,
            "semantic_key": texture.semantic_key,
        }
        content_hash = digest_obj({"payload_hash": payload_hash, "metadata": metadata})
        target = self._object_path(content_hash)
        with self._lock:
            if not (target / "meta.json").exists():
                temporary = self.objects / f".tmp-{content_hash}"
                if temporary.exists():
                    shutil.rmtree(temporary)
                temporary.mkdir(parents=True, exist_ok=True)
                (temporary / "channels.npy").write_bytes(channels_bytes)
                (temporary / "seam.npy").write_bytes(seam_bytes)
                atomic_write_json(temporary / "meta.json", metadata)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    temporary.replace(target)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary, ignore_errors=True)
            self._index[texture.cache_key] = {
                "content_hash": content_hash,
                "channel_digest": texture.channel_digest,
                "seam_digest": texture.seam_digest,
                "params_revision": params.revision,
                "plan_version": plan_version,
                "semantic_key": texture.semantic_key,
            }
            self._semantic_index[texture.semantic_key] = texture.cache_key
            self._save_index()
        return {
            "cache_key": texture.cache_key,
            "content_hash": content_hash,
            "channel_digest": texture.channel_digest,
            "seam_digest": texture.seam_digest,
            "path": str(target),
        }

    def has(self, cache_key: str) -> bool:
        with self._lock:
            return cache_key in self._index

    def find_by_semantic(self, semantic_key: str) -> str | None:
        with self._lock:
            return self._semantic_index.get(semantic_key)

    def get_by_semantic(self, semantic_key: str) -> CacheObject | None:
        cache_key = self.find_by_semantic(semantic_key)
        if cache_key is None:
            return None
        return self.get(cache_key)

    def get(self, cache_key: str) -> CacheObject:
        with self._lock:
            entry = self._index.get(cache_key)
        if entry is None:
            fail("E_CACHE_MISS", "缓存键不存在", cache_key=cache_key)
        content_hash = entry.get("content_hash")
        path = self._object_path(content_hash)
        metadata_path = path / "meta.json"
        if not metadata_path.exists():
            fail("E_CACHE_CORRUPT", "缓存对象元数据不存在", cache_key=cache_key, path=str(path))
        try:
            import json

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            fail("E_CACHE_CORRUPT", "缓存对象元数据损坏", cache_key=cache_key, reason=str(error))
        channels = _load_npy(path / "channels.npy")
        seam = _load_npy(path / "seam.npy")
        payload_hash = digest_bytes((path / "channels.npy").read_bytes() + (path / "seam.npy").read_bytes())
        expected_hash = digest_obj({"payload_hash": payload_hash, "metadata": metadata})
        if (
            payload_hash != metadata.get("payload_hash")
            or expected_hash != content_hash
            or array_digest(channels) != metadata.get("channel_digest")
            or array_digest(seam) != metadata.get("seam_digest")
            or metadata.get("cache_key") != cache_key
        ):
            fail(
                "E_CACHE_CORRUPT",
                "缓存对象内容或校验和不符",
                cache_key=cache_key,
                content_hash=content_hash,
            )
        return CacheObject(cache_key, content_hash, metadata, channels, seam, str(path))

    def index_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in sorted(self._index.items())}

    def verify_entry(self, cache_key: str) -> dict[str, Any]:
        obj = self.get(cache_key)
        return {
            "cache_key": cache_key,
            "content_hash": obj.content_hash,
            "channel_digest": array_digest(obj.channels),
            "seam_digest": array_digest(obj.seam),
            "ok": True,
        }

    def disk_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def put_ready_metadata(
        self,
        texture: BlockTexture,
        cache_record: dict[str, Any],
        params: MaterialParams,
        plan: BlockPlan,
        plan_version: int,
    ) -> dict[str, Any]:
        return {
            "block": texture.block.index,
            "pixel_range": texture.block.pixel_range,
            "cache_key": texture.cache_key,
            "content_hash": cache_record["content_hash"],
            "channel_digest": texture.channel_digest,
            "seam_digest": texture.seam_digest,
            "semantic_key": texture.semantic_key,
            "upstream_hash": texture.upstream_hash,
            "normalization_digest": texture.normalization_digest,
            "params_revision": params.revision,
            "params_digest": params.digest(),
            "plan_version": plan_version,
            "plan_digest": plan.digest(),
            "seed": params.seed,
            "node_path": texture.node_path,
            "duration_ms": round(texture.duration_ms, 6),
            "estimates": texture.estimates,
        }


def default_cache_root() -> Path:
    override = os.environ.get("MATFORGE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MatForge" / "cache"
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "MatForge" / "cache"
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "matforge"
    return Path.home() / ".cache" / "matforge"


def probe_cache_writable(root: str | Path | None = None) -> dict[str, Any]:
    target = Path(root) if root is not None else default_cache_root()
    target.mkdir(parents=True, exist_ok=True)
    payload = canonical_json({"probe": "matforge-cache", "schema": 1}) + b"\n"
    probe = target / ".write-probe"
    try:
        probe.write_bytes(payload)
        read_back = probe.read_bytes()
        if read_back != payload:
            fail("E_CACHE_READONLY", "缓存写入后读回内容不一致", path=str(target))
    except OSError as error:
        fail("E_CACHE_READONLY", "缓存目录不可写", path=str(target), reason=str(error))
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "ok": True,
        "path": str(target),
        "writable": True,
        "platform": "windows" if os.name == "nt" else ("windows-via-wsl" if str(target).startswith("/mnt/") else "posix"),
        "probe_bytes": len(payload),
    }

