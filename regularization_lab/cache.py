"""Verified-result cache keyed by scheme, penalty, mixture and data fingerprint."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, canonical_json, read_json
from .errors import E_CACHE_INVALID, Failure

CACHE_SCHEMA_VERSION = 1


def _checksum(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


@dataclass
class CacheLookup:
    hit: bool
    reason: str
    entry: dict[str, Any] | None = None
    failures: list[Failure] | None = None


class ResultCache:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.cache_dir = self.state_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    def lookup(self, *, cache_key: str, training_fingerprint: str) -> CacheLookup:
        path = self._path(cache_key)
        if not path.exists():
            return CacheLookup(hit=False, reason="cache_miss", failures=[])
        try:
            entry = read_json(path)
            if not isinstance(entry, dict):
                raise ValueError("cache entry is not an object")
            if entry.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("unsupported cache schema version")
            stored_checksum = entry.get("checksum")
            if not isinstance(stored_checksum, str) or stored_checksum != _checksum(entry):
                raise ValueError("cache checksum mismatch")
            if entry.get("cache_key") != cache_key:
                raise ValueError("cache key mismatch")
            if entry.get("training_fingerprint") != training_fingerprint:
                return CacheLookup(
                    hit=False,
                    reason="training_fingerprint_changed",
                    failures=[],
                )
            if not entry.get("converged", False):
                return CacheLookup(hit=False, reason="cached_result_not_converged", failures=[])
            result = entry.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("loss_trajectory"), list):
                raise ValueError("cache result payload is incomplete")
            return CacheLookup(hit=True, reason="verified_converged_cache_hit", entry=entry, failures=[])
        except Exception as exc:
            failure = Failure(
                business_code=E_CACHE_INVALID,
                phase="cache_read",
                stage="cache_validate",
                message=f"cache entry is invalid and will be recomputed: {exc}",
                exception_type=type(exc).__name__,
                details={"cache_key": cache_key, "cache_file": str(path)},
            )
            return CacheLookup(hit=False, reason="cache_invalid", failures=[failure])

    def probe(self, *, cache_key: str, training_fingerprint: str) -> dict[str, Any]:
        lookup = self.lookup(cache_key=cache_key, training_fingerprint=training_fingerprint)
        if lookup.hit and lookup.entry is not None:
            result = lookup.entry["result"]
            rounds = [int(item["round"]) for item in result["loss_trajectory"]]
            return {
                "cache_key": cache_key,
                "hit": True,
                "reason": lookup.reason,
                "reusable_rounds": rounds,
                "recompute_rounds": [],
            }
        return {
            "cache_key": cache_key,
            "hit": False,
            "reason": lookup.reason,
            "reusable_rounds": [],
            "recompute_rounds": "pending_training",
        }

    def store(
        self,
        *,
        cache_key: str,
        training_fingerprint: str,
        optimizer_fingerprint: str,
        regularization: dict[str, Any],
        data_fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        entry: dict[str, Any] = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "cache_key": cache_key,
            "training_fingerprint": training_fingerprint,
            "optimizer_fingerprint": optimizer_fingerprint,
            "regularization": regularization,
            "data_fingerprint": data_fingerprint,
            "converged": bool(result.get("converged", False)),
            "status": result.get("status", "unknown"),
            "result": result,
        }
        entry["checksum"] = _checksum(entry)
        atomic_write_json(self._path(cache_key), entry, indent=2)
