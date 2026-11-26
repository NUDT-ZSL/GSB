from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, digest_json
from .errors import FormatCorrupt


@dataclass
class CacheStats:
    route_hits: int = 0
    route_misses: int = 0
    fusion_hits: int = 0
    fusion_misses: int = 0
    writes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "route_hits": self.route_hits,
            "route_misses": self.route_misses,
            "fusion_hits": self.fusion_hits,
            "fusion_misses": self.fusion_misses,
            "writes": self.writes,
        }


class ContentAddressedCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.route_root = self.root / "routes"
        self.fusion_root = self.root / "fusions"
        self.stats = CacheStats()

    def _path(self, category: str, key: str) -> Path:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError(f"invalid cache key: {key}")
        base = self.route_root if category == "route" else self.fusion_root
        return base / key[:2] / f"{key}.json"

    def _load(self, category: str, key: str) -> dict[str, Any] | None:
        path = self._path(category, key)
        if not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FormatCorrupt(
                f"corrupt cache entry {key}: {exc}", field="cache"
            ) from exc
        if envelope.get("cache_key") != key:
            raise FormatCorrupt(
                f"cache key mismatch in {key}", field="cache"
            )
        payload = envelope.get("payload")
        if digest_json(payload) != envelope.get("payload_hash"):
            raise FormatCorrupt(
                f"cache payload checksum mismatch in {key}", field="cache"
            )
        return payload

    def _put(self, category: str, key: str, payload: dict[str, Any]) -> None:
        path = self._path(category, key)
        envelope = {
            "cache_key": key,
            "payload": payload,
            "payload_hash": digest_json(payload),
        }
        atomic_write_json(path, envelope)
        self.stats.writes += 1

    def get_route(self, key: str) -> dict[str, Any] | None:
        payload = self._load("route", key)
        if payload is None:
            self.stats.route_misses += 1
        else:
            self.stats.route_hits += 1
        return payload

    def put_route(self, key: str, payload: dict[str, Any]) -> None:
        self._put("route", key, payload)

    def get_fusion(self, key: str) -> dict[str, Any] | None:
        payload = self._load("fusion", key)
        if payload is None:
            self.stats.fusion_misses += 1
        else:
            self.stats.fusion_hits += 1
        return payload

    def put_fusion(self, key: str, payload: dict[str, Any]) -> None:
        self._put("fusion", key, payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "stats": self.stats.to_dict(),
        }

