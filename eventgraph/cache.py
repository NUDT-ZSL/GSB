from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, content_hash, read_json


class ArtifactCache:
    """Content-addressed cache with dependency-aware key pointers."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.keys = self.root / "keys"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.keys.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "puts": 0,
            "object_reuses": 0,
            "hits_by_kind": {},
            "misses_by_kind": {},
        }
        self._recent: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.objects.mkdir(parents=True, exist_ok=True)
            self.keys.mkdir(parents=True, exist_ok=True)
            self._stats = {
                "hits": 0,
                "misses": 0,
                "puts": 0,
                "object_reuses": 0,
                "hits_by_kind": {},
                "misses_by_kind": {},
            }
            self._recent = []

    def get(self, kind: str, key: str) -> Any | None:
        with self._lock:
            pointer_path = self._pointer_path(kind, key)
            if not pointer_path.exists():
                self._miss(kind)
                return None
            try:
                pointer = read_json(pointer_path)
                digest = str(pointer["content_hash"])
                object_path = self._object_path(digest)
                if not object_path.exists():
                    self._miss(kind)
                    return None
                payload = read_json(object_path)
                if content_hash(payload) != digest:
                    self._miss(kind)
                    return None
            except (KeyError, OSError, ValueError):
                self._miss(kind)
                return None
            self._stats["hits"] += 1
            self._stats["hits_by_kind"][kind] = (
                self._stats["hits_by_kind"].get(kind, 0) + 1
            )
            return payload

    def put(
        self,
        kind: str,
        key: str,
        payload: Any,
        *,
        key_material: dict[str, Any] | None = None,
    ) -> str:
        with self._lock:
            digest = content_hash(payload)
            object_path = self._object_path(digest)
            if not object_path.exists():
                atomic_write_json(object_path, payload)
            else:
                self._stats["object_reuses"] += 1
            pointer_path = self._pointer_path(kind, key)
            pointer = {
                "kind": kind,
                "key": key,
                "content_hash": digest,
                "key_material": key_material or {},
            }
            atomic_write_json(pointer_path, pointer)
            self._stats["puts"] += 1
            self._recent.append(
                {
                    "kind": kind,
                    "key": key,
                    "content_hash": digest,
                }
            )
            self._recent = self._recent[-20:]
            return digest

    def put_content(self, payload: Any) -> str:
        with self._lock:
            digest = content_hash(payload)
            object_path = self._object_path(digest)
            if not object_path.exists():
                atomic_write_json(object_path, payload)
            else:
                self._stats["object_reuses"] += 1
            self._stats["puts"] += 1
            return digest

    def get_content(self, digest: str) -> Any | None:
        with self._lock:
            path = self._object_path(digest)
            if not path.exists():
                return None
            payload = read_json(path)
            if content_hash(payload) != digest:
                return None
            return payload

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._stats,
                "recent_artifacts": list(self._recent),
                "object_count": sum(1 for _ in self.objects.glob("*/*.json")),
                "pointer_count": sum(1 for _ in self.keys.glob("*/*/*.json")),
            }

    def _pointer_path(self, kind: str, key: str) -> Path:
        return self.keys / kind / key[:2] / f"{key}.json"

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / f"{digest}.json"

    def _miss(self, kind: str) -> None:
        self._stats["misses"] += 1
        self._stats["misses_by_kind"][kind] = (
            self._stats["misses_by_kind"].get(kind, 0) + 1
        )
