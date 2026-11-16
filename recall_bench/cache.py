"""Content-addressed artifact cache and incremental invalidation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import (
    channel_param_fingerprint,
    evaluation_param_fingerprint,
    fusion_param_fingerprint,
)
from .util import atomic_write_json, read_json, stable_hash


@dataclass
class CacheLedger:
    reused: list[dict[str, str]] = field(default_factory=list)
    recomputed: list[dict[str, str]] = field(default_factory=list)

    def record(self, artifact: str, scale_id: str, key: str, reused: bool) -> None:
        entry = {"artifact": artifact, "scale_id": scale_id, "cache_key": key}
        (self.reused if reused else self.recomputed).append(entry)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {"reused": self.reused, "recomputed": self.recomputed}


class ArtifactCache:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, f"{key}.json")

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if os.path.exists(path):
            return read_json(path)
        return None

    def put(self, key: str, payload: Any) -> None:
        atomic_write_json(self._path(key), payload)


def parsed_cache_key(input_fingerprint: str, config: dict[str, Any]) -> str:
    return "parsed_" + stable_hash(
        {"input": input_fingerprint, "types": config["interaction_types"]}
    )


def recall_cache_key(parsed_key: str, config: dict[str, Any], channel: str) -> str:
    return f"recall_{channel}_" + stable_hash(
        {"upstream": parsed_key, "params": channel_param_fingerprint(config, channel)}
    )


def norm_cache_key(recall_keys: dict[str, str]) -> str:
    return "norm_" + stable_hash(recall_keys)


def fusion_cache_key(parsed_key: str, norm_key: str, recall_keys: dict[str, str], config: dict[str, Any]) -> str:
    return "fusion_" + stable_hash(
        {
            "parsed": parsed_key,
            "norm": norm_key,
            "recall": recall_keys,
            "params": fusion_param_fingerprint(config),
        }
    )


def evaluation_cache_key(fusion_key: str, config: dict[str, Any]) -> str:
    return "eval_" + stable_hash(
        {"fusion": fusion_key, "params": evaluation_param_fingerprint(config)}
    )


def decision_cache_key(parsed_key: str, declared_users: int) -> str:
    return "decision_" + stable_hash({"parsed": parsed_key, "declared_users": declared_users})
