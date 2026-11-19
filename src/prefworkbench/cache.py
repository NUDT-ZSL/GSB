from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write,
    canonical_bytes,
    hash_value,
    sha256_bytes,
    without_field,
)
from .errors import E_CACHE_CORRUPT, WorkbenchError

CACHE_SCHEMA = 1


def cache_key_from_components(components: list[dict[str, Any]]) -> str:
    return "sha256:" + sha256_bytes(canonical_bytes(components)).removeprefix("sha256:")


class CacheStore:
    """Immutable content-addressed node outputs.

    The object key includes every input digest, upstream payload hash and node
    parameter. The entry envelope also carries a checksum over the exact payload
    and key components, so partial or tampered data is never consumed.
    """

    def __init__(self, stage_root: Path | str) -> None:
        self.root = Path(stage_root) / "cache" / "objects"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, node: str, cache_key: str) -> Path:
        digest = cache_key.removeprefix("sha256:")
        return self.root / node / f"{digest}.json"

    def get(
        self,
        node: str,
        cache_key: str,
        *,
        version_hint: int | None = None,
    ) -> dict[str, Any] | None:
        path = self.path_for(node, cache_key)
        if not path.exists():
            return None
        raw = path.read_bytes()
        try:
            entry = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            version_match = re.search(rb'"created_version"\s*:\s*(\d+)', raw)
            recovered_version = int(version_match.group(1)) if version_match else version_hint
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache entry is not valid JSON",
                {
                    "category": "CACHE_JSON_CORRUPT",
                    "path": str(path),
                    "cache_key": cache_key,
                    "version_unknown": recovered_version is None,
                },
                node=node,
                version=recovered_version,
            ) from exc
        self._validate_entry(entry, node, cache_key, path, version_hint)
        return entry

    def _validate_entry(
        self,
        entry: Any,
        node: str,
        cache_key: str,
        path: Path,
        version_hint: int | None,
    ) -> None:
        version = entry.get("created_version") if isinstance(entry, dict) else None
        if not isinstance(version, int):
            version = version_hint

        def corrupt(category: str, message: str, **extra: Any) -> None:
            details = {
                "category": category,
                "path": str(path),
                "cache_key": cache_key,
                **extra,
            }
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                message,
                details,
                node=node,
                version=version,
            )

        if not isinstance(entry, dict):
            corrupt("CACHE_ENTRY_NOT_OBJECT", "cache entry root must be an object")
        if entry.get("schema_version") != CACHE_SCHEMA:
            corrupt("CACHE_SCHEMA_MISMATCH", "cache entry schema version mismatch")
        if entry.get("node") != node:
            corrupt("CACHE_NODE_MISMATCH", "cache entry node does not match its path")
        if entry.get("cache_key") != cache_key:
            corrupt("CACHE_KEY_FIELD_MISMATCH", "cache key field does not match lookup key")
        components = entry.get("key_components")
        if not isinstance(components, list):
            corrupt("CACHE_KEY_COMPONENTS_MISSING", "cache key components are missing")
        expected_key = cache_key_from_components(components)
        if expected_key != cache_key:
            corrupt(
                "CACHE_KEY_COMPONENTS_MISMATCH",
                "cache key cannot be reconstructed from its components",
                reconstructed_key=expected_key,
            )
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            corrupt("CACHE_PAYLOAD_MISSING", "cache payload is missing")
        payload_hash = hash_value(payload)
        if entry.get("payload_hash") != payload_hash:
            corrupt(
                "CACHE_PAYLOAD_HASH_MISMATCH",
                "cache payload hash does not match payload",
                recorded_hash=entry.get("payload_hash"),
                computed_hash=payload_hash,
            )
        recorded_checksum = entry.get("entry_checksum")
        computed_checksum = hash_value(without_field(entry, "entry_checksum"))
        if recorded_checksum != computed_checksum:
            corrupt(
                "CACHE_ENTRY_CHECKSUM_MISMATCH",
                "cache entry checksum does not match envelope",
                recorded_checksum=recorded_checksum,
                computed_checksum=computed_checksum,
            )

    def put(
        self,
        node: str,
        cache_key: str,
        key_components: list[dict[str, Any]],
        payload: dict[str, Any],
        *,
        version: int,
    ) -> dict[str, Any]:
        path = self.path_for(node, cache_key)
        payload_hash = hash_value(payload)
        if path.exists():
            existing = self.get(node, cache_key, version_hint=version)
            if existing is None:
                raise WorkbenchError(
                    E_CACHE_CORRUPT,
                    "cache entry disappeared during read",
                    {"path": str(path)},
                    node=node,
                    version=version,
                )
            if existing["payload_hash"] != payload_hash:
                raise WorkbenchError(
                    E_CACHE_CORRUPT,
                    "content-addressed cache collision or nondeterministic node output",
                    {
                        "category": "CACHE_CONTENT_COLLISION",
                        "existing_hash": existing["payload_hash"],
                        "new_hash": payload_hash,
                    },
                    node=node,
                    version=version,
                )
            return existing
        entry = {
            "schema_version": CACHE_SCHEMA,
            "node": node,
            "cache_key": cache_key,
            "created_version": version,
            "key_components": key_components,
            "payload_hash": payload_hash,
            "payload": payload,
        }
        entry["entry_checksum"] = hash_value(entry)
        atomic_write(path, canonical_bytes(entry))
        return entry

    def payload(self, entry: dict[str, Any]) -> dict[str, Any]:
        return entry["payload"]
