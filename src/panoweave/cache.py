"""Content-addressed cache for deterministic resampling intermediates."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CacheRead:
    image: np.ndarray
    mask: np.ndarray
    margin: np.ndarray


@dataclass(frozen=True)
class CacheWrite:
    created: bool
    evicted: tuple[str, ...] = ()


class ContentAddressedCache:
    """A deterministic LRU-like cache keyed by semantic content hashes.

    Files use the key as their filename stem. A small index records insertion
    order so eviction does not depend on filesystem timestamps.
    """

    def __init__(self, root: str | Path, max_entries: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(0, int(max_entries))
        self.index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, int]:
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {str(key): int(value) for key, value in data.items()}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_index(self) -> None:
        payload = json.dumps(self._index, sort_keys=True, separators=(",", ":")) + "\n"
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, self.index_path)

    def _paths(self, key: str) -> dict[str, Path]:
        return {
            "image": self.root / f"{key}.image.npy",
            "mask": self.root / f"{key}.mask.npy",
            "margin": self.root / f"{key}.margin.npy",
            "meta": self.root / f"{key}.meta.json",
        }

    def _entry_valid(self, key: str) -> bool:
        paths = self._paths(key)
        if not all(path.exists() for path in paths.values()):
            return False
        try:
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            return (
                meta.get("key") == key
                and meta.get("image_sha") == sha256_file(paths["image"])
                and meta.get("mask_sha") == sha256_file(paths["mask"])
                and meta.get("margin_sha") == sha256_file(paths["margin"])
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def get(self, key: str) -> CacheRead | None:
        if key not in self._index or not self._entry_valid(key):
            return None
        paths = self._paths(key)
        return CacheRead(
            image=np.load(paths["image"], allow_pickle=False),
            mask=np.load(paths["mask"], allow_pickle=False).astype(bool),
            margin=np.load(paths["margin"], allow_pickle=False),
        )

    def _remove(self, key: str) -> None:
        for path in self._paths(key).values():
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def put(
        self,
        key: str,
        image: np.ndarray,
        mask: np.ndarray,
        margin: np.ndarray,
    ) -> CacheWrite:
        paths = self._paths(key)
        if self.max_entries <= 0:
            return CacheWrite(created=False)
        if key in self._index and self._entry_valid(key):
            return CacheWrite(created=False)
        evicted: list[str] = []
        if key not in self._index:
            while len(self._index) >= self.max_entries and self._index:
                oldest = min(self._index, key=lambda item: (self._index[item], item))
                self._remove(oldest)
                del self._index[oldest]
                evicted.append(oldest)
        image_array = np.ascontiguousarray(image, dtype=np.float64)
        mask_array = np.ascontiguousarray(mask, dtype=np.bool_)
        margin_array = np.ascontiguousarray(margin, dtype=np.float64)
        payloads = {
            "image": image_array,
            "mask": mask_array,
            "margin": margin_array,
        }
        shas: dict[str, str] = {}
        for name, array in payloads.items():
            temporary = paths[name].with_suffix(".npy.tmp")
            with temporary.open("wb") as handle:
                np.save(handle, array, allow_pickle=False)
            os.replace(temporary, paths[name])
            shas[name] = sha256_file(paths[name])
        meta = {
            "key": key,
            "image_sha": shas["image"],
            "mask_sha": shas["mask"],
            "margin_sha": shas["margin"],
            "dtype": {"image": "float64", "mask": "bool", "margin": "float64"},
        }
        temporary = paths["meta"].with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, paths["meta"])
        sequence = max(self._index.values(), default=0) + 1
        self._index[key] = sequence
        self._save_index()
        return CacheWrite(created=True, evicted=tuple(evicted))

    def clear(self) -> None:
        for child in list(self.root.iterdir()):
            if child.is_file() and (
                child.name.startswith("index.json") or child.suffix in {".npy", ".json"}
            ):
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass
        self._index = {}

    def report(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "max_entries": self.max_entries,
            "entries": len(self._index),
            "keys": sorted(self._index, key=lambda item: (self._index[item], item)),
        }

