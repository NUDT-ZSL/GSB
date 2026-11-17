"""Content-addressed stage artifact cache with deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import CACHE_SCHEMA_VERSION
from .util import canonicalize, artifact_digest, load_json, atomic_write_json


@dataclass
class CacheStats:
    stage: str
    reused: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": len(self.reused),
            "misses": len(self.recomputed),
            "reused": sorted(self.reused, key=lambda value: (value.lower(), value)),
            "recomputed": sorted(self.recomputed, key=lambda value: (value.lower(), value)),
            "corrupt": sorted(self.corrupt, key=lambda value: (value.lower(), value)),
        }


class ArtifactCache:
    def __init__(self, root: Path, *, enabled: bool = True):
        self.root = root
        self.enabled = enabled
        self.stats = {stage: CacheStats(stage) for stage in ("features", "scores", "fusion")}

    def _path(self, stage: str, key: str) -> Path:
        return self.root / stage / f"{key}.json"

    def get(self, stage: str, key: str, task_id: str) -> dict[str, Any] | None:
        stats = self.stats[stage]
        path = self._path(stage, key)
        if not self.enabled:
            stats.recomputed.append(task_id)
            return None
        if not path.exists():
            stats.recomputed.append(task_id)
            return None
        try:
            envelope = load_json(path)
            if envelope.get("schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("cache schema mismatch")
            if envelope.get("stage") != stage or envelope.get("cache_key") != key:
                raise ValueError("cache identity mismatch")
            artifact = envelope.get("artifact")
            if artifact_digest(artifact) != envelope.get("artifact_digest"):
                raise ValueError("artifact digest mismatch")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            stats.corrupt.append(task_id)
            stats.recomputed.append(task_id)
            return None
        stats.reused.append(task_id)
        return canonicalize(artifact)

    def put(
        self,
        stage: str,
        key: str,
        task_id: str,
        artifact: dict[str, Any],
        *,
        config_fingerprint: str,
        scale_tier: str,
        upstream_digest: str,
        injection_fingerprint: str,
    ) -> dict[str, Any]:
        canonical_artifact = canonicalize(artifact)
        envelope = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "stage": stage,
            "cache_key": key,
            "task_id": task_id,
            "config_fingerprint": config_fingerprint,
            "scale_tier": scale_tier,
            "upstream_digest": upstream_digest,
            "injection_fingerprint": injection_fingerprint,
            "artifact_digest": artifact_digest(canonical_artifact),
            "artifact": canonical_artifact,
        }
        atomic_write_json(self._path(stage, key), envelope)
        return canonical_artifact

    def summary(self) -> dict[str, Any]:
        return {stage: self.stats[stage].as_dict() for stage in ("features", "scores", "fusion")}
