from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import canonical_hash, read_json, write_json_atomic


class ContentCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "cache_index.json"
        self.index: dict[str, dict[str, Any]] = {}
        if self.index_path.exists():
            loaded = read_json(self.index_path)
            if isinstance(loaded, dict):
                self.index = loaded

    def object_path(self, key: str) -> Path:
        return self.objects / key[:2] / f"{key}.json"

    def has(self, key: str) -> bool:
        path = self.object_path(key)
        if not path.exists():
            return False
        try:
            envelope = read_json(path)
            return envelope.get("cache_key") == key
        except Exception:
            return False

    def get(self, key: str) -> Any:
        envelope = read_json(self.object_path(key))
        if envelope.get("cache_key") != key:
            raise ValueError(f"cache key/checksum mismatch for {key}")
        return envelope.get("payload")

    def put(self, key: str, payload: Any, metadata: dict[str, Any] | None = None) -> Path:
        path = self.object_path(key)
        envelope = {
            "cache_key": key,
            "payload_sha256": canonical_hash(payload),
            "payload": payload,
        }
        write_json_atomic(path, envelope)
        self.index[key] = {
            "cache_key": key,
            "payload_sha256": envelope["payload_sha256"],
            **(metadata or {}),
        }
        return path

    def save_index(self) -> None:
        ordered = {key: self.index[key] for key in sorted(self.index)}
        write_json_atomic(self.index_path, ordered)

    def persistent_entry_count(self) -> int:
        return sum(1 for _ in self.objects.rglob("*.json"))

    def trace(self, key_or_utterance: str) -> list[dict[str, Any]]:
        if key_or_utterance in self.index:
            return [self.index[key_or_utterance]]
        return [
            entry
            for key, entry in sorted(self.index.items())
            if entry.get("utterance_id") == key_or_utterance
        ]


def build_cache_key(*, stage: str, utterance_id: str, **components: Any) -> str:
    payload = {
        "stage": stage,
        "utterance_id": utterance_id,
        "components": components,
    }
    return canonical_hash(payload)
