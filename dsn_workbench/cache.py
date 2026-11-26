from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ConfigBundle
from .errors import E_CACHE_CORRUPT, WorkbenchError
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    content_hash,
    json_type_name,
    text_hash,
)


STAGES = ("normalize", "candidates", "disambiguation", "tagging")


def stage_config_hash(stage: str, bundle: ConfigBundle) -> str:
    if stage == "normalize":
        return bundle.normalization_hash
    if stage == "candidates":
        return content_hash(
            {
                "dictionary": bundle.dictionary_hash,
                "rules": bundle.rule_hash,
            }
        )
    if stage == "disambiguation":
        return content_hash(
            {
                "disambiguation": bundle.disambiguation_hash,
                "dictionary": bundle.dictionary_hash,
                "rules": bundle.rule_hash,
            }
        )
    if stage == "tagging":
        return bundle.tagging_hash
    raise ValueError(f"unknown stage: {stage}")


def make_stage_key(
    *,
    stage: str,
    sentence_id: str,
    raw_text: str,
    config: ConfigBundle,
    upstream_hash: str,
    domain: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "stage-cache-key-v1",
        "stage": stage,
        "sentence_id": sentence_id,
        "domain": domain if domain is not None else config.data["domain"],
        "locale": locale if locale is not None else config.data["locale"],
        "raw_text_hash": text_hash(raw_text),
        "dictionary_version": config.data["dictionary_version"],
        "rule_version": config.data["rule_version"],
        "config_parameter_version": config.effective_hash,
        "upstream_hash": upstream_hash,
        "stage_config_hash": stage_config_hash(stage, config),
    }


class ContentAddressedCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.keys_dir = self.root / "keys"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.keys_dir.mkdir(parents=True, exist_ok=True)

    def _object_path(self, object_hash: str) -> Path:
        return self.objects_dir / object_hash[:2] / f"{object_hash}.json"

    def _key_path(self, key_hash: str) -> Path:
        return self.keys_dir / key_hash[:2] / f"{key_hash}.json"

    def put_object(self, payload: Any) -> str:
        object_bytes = canonical_json_bytes(payload)
        object_hash = content_hash(payload)
        path = self._object_path(object_hash)
        if not path.exists():
            atomic_write_json(path, payload)
        return object_hash

    def get_object(self, object_hash: str) -> Any:
        path = self._object_path(object_hash)
        if not path.exists():
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache object is missing",
                {"object_hash": object_hash, "path": str(path)},
            )
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache object cannot be read",
                {"object_hash": object_hash, "path": str(path), "error": str(exc)},
            ) from exc
        actual = content_hash(payload)
        if actual != object_hash:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache object content hash mismatch",
                {
                    "object_hash": object_hash,
                    "actual": actual,
                    "path": str(path),
                },
            )
        return payload

    def put(
        self, stage: str, key: dict[str, Any], payload: dict[str, Any]
    ) -> str:
        key_hash = content_hash(key)
        object_hash = self.put_object(payload)
        envelope = {
            "schema": "stage-cache-entry-v1",
            "stage": stage,
            "key_hash": key_hash,
            "key": key,
            "object_hash": object_hash,
        }
        atomic_write_json(self._key_path(key_hash), envelope)
        return object_hash

    def get(
        self, stage: str, key: dict[str, Any]
    ) -> tuple[dict[str, Any], str] | None:
        key_hash = content_hash(key)
        path = self._key_path(key_hash)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache key entry cannot be read",
                {"key_hash": key_hash, "path": str(path), "error": str(exc)},
            ) from exc
        if not isinstance(envelope, dict):
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache key entry is not an object",
                {"key_hash": key_hash, "type": json_type_name(envelope)},
            )
        if (
            envelope.get("stage") != stage
            or envelope.get("key_hash") != key_hash
            or envelope.get("key") != key
        ):
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache key entry metadata mismatch",
                {"key_hash": key_hash, "stage": stage},
            )
        object_hash = envelope.get("object_hash")
        if not isinstance(object_hash, str):
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache key entry has no object hash",
                {"key_hash": key_hash},
            )
        return self.get_object(object_hash), object_hash

    def alias(
        self,
        old_key: dict[str, Any],
        new_key: dict[str, Any],
        stage: str,
        *,
        expected_object_hash: str | None = None,
    ) -> bool:
        old_loaded = self.get(stage, old_key)
        if old_loaded is None:
            return False
        _, object_hash = old_loaded
        if expected_object_hash is not None and object_hash != expected_object_hash:
            return False
        existing = self.get(stage, new_key)
        if existing is not None:
            return existing[1] == object_hash
        new_key_hash = content_hash(new_key)
        envelope = {
            "schema": "stage-cache-entry-v1",
            "stage": stage,
            "key_hash": new_key_hash,
            "key": new_key,
            "object_hash": object_hash,
        }
        atomic_write_json(self._key_path(new_key_hash), envelope)
        return True

    def drop_key_hash(self, key_hash: str) -> bool:
        path = self._key_path(key_hash)
        if path.exists():
            path.unlink()
            return True
        return False

    def stats(self) -> dict[str, int]:
        object_count = sum(1 for _ in self.objects_dir.rglob("*.json"))
        key_count = sum(1 for _ in self.keys_dir.rglob("*.json"))
        return {"objects": object_count, "keys": key_count}

