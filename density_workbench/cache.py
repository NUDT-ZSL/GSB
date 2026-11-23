from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_dumps, sha256_obj


@dataclass
class CacheAction:
    stage: str
    cache_key: str
    hit: bool
    object_hash: str | None
    sample_ids: list[str] = field(default_factory=list)
    dependencies: dict[str, Any] = field(default_factory=dict)
    artifact: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "cache_key": self.cache_key,
            "hit": self.hit,
            "object_hash": self.object_hash,
            "sample_ids": self.sample_ids,
            "dependencies": self.dependencies,
            "artifact": self.artifact,
        }


@dataclass
class CacheLookup:
    action: CacheAction
    payload: Any | None


class ContentAddressedCache:
    """Content-addressed JSON object store with key-to-content indirection."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.keys_dir = self.root / "keys"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.actions: list[CacheAction] = []

    def make_key(self, stage: str, dependencies: dict[str, Any]) -> str:
        return sha256_obj({"stage": stage, "dependencies": dependencies})

    def _object_path(self, object_hash: str) -> Path:
        return self.objects_dir / object_hash[:2] / f"{object_hash}.json"

    def _key_path(self, cache_key: str) -> Path:
        return self.keys_dir / f"{cache_key}.json"

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(text)}")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def read(self, cache_key: str) -> tuple[Any | None, str | None]:
        key_path = self._key_path(cache_key)
        if not key_path.exists():
            return None, None
        try:
            key_record = json.loads(key_path.read_text(encoding="utf-8"))
            object_hash = str(key_record["object_hash"])
            object_path = self._object_path(object_hash)
            payload = json.loads(object_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        if sha256_obj(payload) != object_hash:
            return None, None
        return payload, object_hash

    def write(self, cache_key: str, payload: Any) -> str:
        object_hash = sha256_obj(payload)
        object_path = self._object_path(object_hash)
        if not object_path.exists():
            self._atomic_write(object_path, canonical_dumps(payload))
        else:
            try:
                existing = json.loads(object_path.read_text(encoding="utf-8"))
                valid = sha256_obj(existing) == object_hash
            except (OSError, ValueError):
                valid = False
            if not valid:
                self._atomic_write(object_path, canonical_dumps(payload))
        key_record = {"cache_key": cache_key, "object_hash": object_hash}
        self._atomic_write(self._key_path(cache_key), canonical_dumps(key_record))
        return object_hash

    def get_or_compute(
        self,
        cache_key: str,
        builder: Callable[[], Any],
        *,
        stage: str,
        dependencies: dict[str, Any],
        sample_ids: list[str] | None = None,
        artifact: str | None = None,
        force: bool = False,
    ) -> tuple[Any, CacheAction]:
        sample_ids = list(sample_ids or [])
        if not force:
            payload, object_hash = self.read(cache_key)
            if payload is not None:
                action = CacheAction(
                    stage=stage,
                    cache_key=cache_key,
                    hit=True,
                    object_hash=object_hash,
                    sample_ids=sample_ids,
                    dependencies=dependencies,
                    artifact=artifact,
                )
                self.actions.append(action)
                return payload, action
        payload = builder()
        object_hash = self.write(cache_key, payload)
        action = CacheAction(
            stage=stage,
            cache_key=cache_key,
            hit=False,
            object_hash=object_hash,
            sample_ids=sample_ids,
            dependencies=dependencies,
            artifact=artifact,
        )
        self.actions.append(action)
        return payload, action

    def put(
        self,
        cache_key: str,
        payload: Any,
        *,
        stage: str,
        dependencies: dict[str, Any],
        sample_ids: list[str] | None = None,
        artifact: str | None = None,
    ) -> CacheAction:
        object_hash = self.write(cache_key, payload)
        action = CacheAction(
            stage=stage,
            cache_key=cache_key,
            hit=False,
            object_hash=object_hash,
            sample_ids=list(sample_ids or []),
            dependencies=dependencies,
            artifact=artifact,
        )
        self.actions.append(action)
        return action

    def reset_actions(self) -> None:
        self.actions = []

    def action_report(self) -> dict[str, Any]:
        hit_actions = [action for action in self.actions if action.hit]
        miss_actions = [action for action in self.actions if not action.hit]
        reused_objects = sorted(
            {action.object_hash for action in hit_actions if action.object_hash}
        )
        written_objects = sorted(
            {action.object_hash for action in miss_actions if action.object_hash}
        )
        return {
            "actions": [action.to_dict() for action in self.actions],
            "hit_count": len(hit_actions),
            "miss_count": len(miss_actions),
            "reused_object_hashes": reused_objects,
            "written_object_hashes": written_objects,
        }
