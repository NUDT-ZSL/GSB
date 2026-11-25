"""Four-stage content-addressed JSON cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .codes import EngineError, ErrorCode
from .hashing import canonical_hash, read_json, write_atomic_json


STAGES = (
    "input_validation",
    "bin_boundaries",
    "encoding_generation",
    "stats_export",
)


def make_cache_key(stage: str, key_spec: Dict[str, Any]) -> str:
    if stage not in STAGES:
        raise EngineError(
            ErrorCode.E_CONFIG_INVALID,
            f"unknown cache stage: {stage}",
            {"stage": stage, "supported": list(STAGES)},
        )
    return canonical_hash({"stage": stage, "key_spec": key_spec})


class ContentCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.reused_keys: Set[str] = set()
        self.recomputed_keys: Set[str] = set()
        self.affected_keys: Set[str] = set()

    def _path(self, stage: str, key: str) -> Path:
        return self.root / stage / f"{key}.json"

    def mark_affected(self, stage: str, key: str) -> str:
        identity = f"{stage}:{key}"
        self.affected_keys.add(identity)
        return identity

    def get(self, stage: str, key: str) -> Optional[Any]:
        identity = self.mark_affected(stage, key)
        path = self._path(stage, key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            envelope = read_json(path)
        except (json.JSONDecodeError, OSError) as error:
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                f"cache entry is unreadable: {path}",
                {"path": str(path), "reason": str(error)},
            )
        if not isinstance(envelope, dict):
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                "cache envelope must be an object",
                {"path": str(path)},
            )
        if envelope.get("stage") != stage or envelope.get("key") != key:
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                "cache envelope stage/key does not match content address",
                {"path": str(path)},
            )
        data = envelope.get("data")
        expected_checksum = envelope.get("checksum")
        actual_checksum = canonical_hash(data)
        if expected_checksum != actual_checksum:
            raise EngineError(
                ErrorCode.E_CACHE_CORRUPT,
                "cache checksum mismatch",
                {
                    "path": str(path),
                    "expected_checksum": expected_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        self.hits += 1
        self.reused_keys.add(identity)
        return data

    def put(self, stage: str, key: str, data: Any) -> str:
        identity = self.mark_affected(stage, key)
        checksum = canonical_hash(data)
        envelope = {
            "stage": stage,
            "key": key,
            "checksum": checksum,
            "data": data,
        }
        path = self._path(stage, key)
        write_atomic_json(path, envelope, pretty=True)
        self.writes += 1
        self.recomputed_keys.add(identity)
        return checksum

    def inventory(self) -> Dict[str, Any]:
        stages: Dict[str, Dict[str, int]] = {}
        total_entries = 0
        total_bytes = 0
        for stage in STAGES:
            stage_dir = self.root / stage
            entries = 0
            bytes_used = 0
            if stage_dir.exists():
                for path in sorted(stage_dir.glob("*.json")):
                    entries += 1
                    bytes_used += path.stat().st_size
            stages[stage] = {"entries": entries, "bytes": bytes_used}
            total_entries += entries
            total_bytes += bytes_used
        return {
            "root": "content-addressed-cache",
            "total_entries": total_entries,
            "total_bytes": total_bytes,
            "stages": stages,
        }

    def event_summary(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "reused_keys": sorted(self.reused_keys),
            "recomputed_keys": sorted(self.recomputed_keys),
            "affected_keys": sorted(self.affected_keys),
        }

    def consistency_report(self) -> Dict[str, Any]:
        reused = set(self.reused_keys)
        recomputed = set(self.recomputed_keys)
        affected = set(self.affected_keys)
        overlap = sorted(reused & recomputed)
        union = reused | recomputed
        missing = sorted(affected - union)
        extra = sorted(union - affected)
        ok = not overlap and not missing and not extra
        return {
            "ok": ok,
            "overlap": overlap,
            "missing_from_reuse_or_recompute": missing,
            "extra": extra,
            "affected_count": len(affected),
            "reused_count": len(reused),
            "recomputed_count": len(recomputed),
        }
