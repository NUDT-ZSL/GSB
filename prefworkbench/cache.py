"""Content-addressed local artifact cache with integrity validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, content_hash, read_json, sealed, verify_sealed
from .errors import E_CACHE_CORRUPT, BusinessError


class ArtifactCache:
    def __init__(self, root: str | Path, *, read_enabled: bool = True) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.read_enabled = read_enabled
        self.hits = {"quality": 0, "neardup": 0, "consistency": 0, "sampling": 0, "manifest": 0}
        self.misses = {"quality": 0, "neardup": 0, "consistency": 0, "sampling": 0, "manifest": 0}
        self.partial_quality: dict[str, bool] = {}

    def _path(self, stage: str, key: str) -> Path:
        return self.root / stage / f"{key.replace('sha256:', '')}.json"

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        path = self._path(stage, key)
        if not self.read_enabled or not path.exists():
            self.misses[stage] += 1
            return None
        try:
            raw = read_json(path)
            payload = verify_sealed(raw, context=f"{stage} cache {key}")
        except BusinessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BusinessError(
                E_CACHE_CORRUPT, f"cannot read {stage} cache entry: {exc}"
            ) from exc
        if payload.get("cache_key") != key:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"{stage} cache key mismatch",
                details={"expected": key, "actual": payload.get("cache_key")},
            )
        self.hits[stage] += 1
        return payload

    def put(self, stage: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("cache_key", None)
        body.pop("content_hash", None)
        body["cache_key"] = key
        artifact = sealed(body)
        atomic_write_json(self._path(stage, key), artifact, canonical=True)
        return artifact

    def stats(self) -> dict[str, Any]:
        return {
            "hits": dict(self.hits),
            "misses": dict(self.misses),
            "quality_partial": dict(sorted(self.partial_quality.items())),
        }


def pair_quality_key(
    *,
    pair_digest: str,
    quality_config_version: str,
    seed: int,
    precision: str,
    schema_version: str = "quality-cache/1.0",
) -> str:
    return content_hash(
        {
            "schema": schema_version,
            "stage": "quality",
            "pair_input_digest": pair_digest,
            "upstream_artifact_hash": None,
            "config_version": quality_config_version,
            "sampling_seed": seed,
            "precision": precision,
        }
    )


def stage_cache_key(
    *,
    stage: str,
    input_digest: str,
    upstream_artifact_hash: str | None,
    config_version: str,
    seed: int,
    extra: dict[str, Any] | None = None,
    schema_version: str,
) -> str:
    return content_hash(
        {
            "schema": schema_version,
            "stage": stage,
            "input_digest": input_digest,
            "upstream_artifact_hash": upstream_artifact_hash,
            "config_version": config_version,
            "sampling_seed": seed,
            "extra": extra or {},
        }
    )

