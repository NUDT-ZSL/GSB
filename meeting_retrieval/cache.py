"""Content-addressed node cache with full key provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from .canonical import atomic_write_json, canonical_json_bytes, digest, payload_checksum, read_json, sha256_hex
from .errors import BusinessError, E_CACHE_CORRUPT, E_CACHE_MISS, E_RESOURCE_LIMIT


class ContentAddressedCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(components: Dict[str, Any]) -> str:
        return sha256_hex(canonical_json_bytes(components))

    def entry_path(self, key: str) -> Path:
        return self.entries_dir / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.entry_path(key).exists()

    def count(self) -> int:
        return sum(1 for _ in self.entries_dir.glob("*.json"))

    def existing_keys(self) -> Set[str]:
        return {path.stem for path in self.entries_dir.glob("*.json")}

    def ensure_capacity(self, keys: Iterable[str], max_entries: int) -> None:
        keys_set = set(keys)
        current = self.existing_keys()
        missing = keys_set - current
        final_count = len(current | keys_set)
        if final_count > max_entries:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "缓存条目总量超过配置上限",
                field="max_cache_entries",
                details={
                    "current_entries": len(current),
                    "required_new_entries": len(missing),
                    "max_cache_entries": max_entries,
                },
            )

    def get(self, key: str, allow_missing: bool = False) -> Optional[Dict[str, Any]]:
        path = self.entry_path(key)
        if not path.exists():
            if allow_missing:
                return None
            raise BusinessError(E_CACHE_MISS, "缓存条目不存在", details={"cache_key": key})
        envelope = read_json(path, error_code=E_CACHE_CORRUPT, field="cache_entry")
        if not isinstance(envelope, dict):
            raise BusinessError(E_CACHE_CORRUPT, "缓存条目格式损坏", details={"cache_key": key})
        checksum = envelope.get("checksum")
        body = {field: value for field, value in envelope.items() if field != "checksum"}
        if checksum != digest(body) or envelope.get("key") != key:
            raise BusinessError(E_CACHE_CORRUPT, "缓存条目校验和不匹配", details={"cache_key": key})
        payload = envelope.get("payload")
        if payload_checksum(payload) != envelope.get("artifact_hash"):
            raise BusinessError(E_CACHE_CORRUPT, "缓存产物哈希不匹配", details={"cache_key": key})
        return envelope

    def put(
        self,
        key: str,
        node: str,
        pair_id: str,
        key_components: Dict[str, Any],
        versions: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> bool:
        path = self.entry_path(key)
        artifact_hash = payload_checksum(payload)
        if path.exists():
            existing = self.get(key)
            if existing is not None and existing["artifact_hash"] == artifact_hash:
                return False
            raise BusinessError(
                E_CACHE_CORRUPT,
                "相同缓存键产生了不同产物",
                pair_id=pair_id,
                details={"cache_key": key},
            )
        body: Dict[str, Any] = {
            "key": key,
            "node": node,
            "pair_id": pair_id,
            "key_components": key_components,
            "versions": versions,
            "artifact_hash": artifact_hash,
            "payload": payload,
        }
        envelope = dict(body)
        envelope["checksum"] = digest(body)
        atomic_write_json(path, envelope)
        return True

    def describe(self, key: str) -> Dict[str, Any]:
        envelope = self.get(key)
        assert envelope is not None
        return {
            "cache_key": key,
            "node": envelope["node"],
            "pair_id": envelope["pair_id"],
            "artifact_hash": envelope["artifact_hash"],
            "content_checksum": envelope["checksum"],
            "versions": envelope["versions"],
            "key_components": envelope["key_components"],
        }

    def verify_all(self) -> Dict[str, Any]:
        total = 0
        errors = []
        for path in sorted(self.entries_dir.glob("*.json")):
            total += 1
            try:
                self.get(path.stem)
            except BusinessError as exc:
                errors.append(exc.to_dict()["error"])
        return {"ok": not errors, "entries": total, "errors": errors}
