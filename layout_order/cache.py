"""Content-addressed local artifact cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, canonical_json, sha256_obj


class ContentAddressedCache:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else None
        self._memory: dict[str, Any] = {}
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(
        node: str,
        page_id: str,
        dependencies: dict[str, Any],
        config: dict[str, Any],
    ) -> str:
        return sha256_obj(
            {
                "node": node,
                "page_id": page_id,
                "dependencies": dependencies,
                "config": config,
            }
        )

    def get(self, key: str) -> Any | None:
        if key in self._memory:
            return self._memory[key]
        if self.directory is None:
            return None
        path = self.directory / f"{key}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("cache_key") != key:
                return None
            value = payload.get("value")
            if payload.get("value_hash") != sha256_obj(value):
                return None
        except Exception:
            return None
        self._memory[key] = value
        return value

    def put(
        self, key: str, value: Any, *, metadata: dict[str, Any] | None = None
    ) -> None:
        self._memory[key] = value
        if self.directory is None:
            return
        payload = {
            "cache_key": key,
            "value_hash": sha256_obj(value),
            "metadata": metadata or {},
            "value": value,
        }
        atomic_write_json(self.directory / f"{key}.json", payload)

    def clear(self) -> None:
        self._memory.clear()
        if self.directory is not None:
            for path in sorted(self.directory.glob("*.json")):
                path.unlink()

    def inspect(self, value: Any) -> str:
        return sha256_obj(value)

