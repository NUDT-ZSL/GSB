"""Content-addressed cache with explicit key composition."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import numpy as np

from .config import PipelineConfig
from .errors import fail
from .models import ManifestEntry, artifact_hash, canonical_bytes, read_json, sha256_bytes


NODE_TYPES = (
    "frame_features",
    "timbre_embedding",
    "conversion_params",
    "synth_pcm",
)

T = TypeVar("T")


def cache_key(components: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(components))


def feature_key_components(
    entry: ManifestEntry, audio_chunk_sha256: str, config: PipelineConfig
) -> dict[str, Any]:
    return {
        "node_type": "frame_features",
        "schema_version": 1,
        "audio_chunk_sha256": audio_chunk_sha256,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "precision_profile": entry.precision_profile,
        "extractor_version": config.extractor_version,
        "seed": entry.seed,
        "ref_revision_policy": "excluded_because_reference_independent",
    }


def timbre_key_components(
    entry: ManifestEntry,
    *,
    ref_audio_chunk_sha256: str,
    config: PipelineConfig,
) -> dict[str, Any]:
    return {
        "node_type": "timbre_embedding",
        "schema_version": 1,
        "audio_chunk_sha256": ref_audio_chunk_sha256,
        "ref_audio_chunk_sha256": ref_audio_chunk_sha256,
        "ref_id": entry.ref_id,
        "ref_revision": entry.ref_revision,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "precision_profile": entry.precision_profile,
        "embedding_version": config.embedding_version,
        "unit_count": config.unit_count,
    }


def params_key_components(
    entry: ManifestEntry,
    *,
    audio_chunk_sha256: str,
    ref_audio_chunk_sha256: str,
    upstream_hashes: dict[str, str],
    config: PipelineConfig,
) -> dict[str, Any]:
    return {
        "node_type": "conversion_params",
        "schema_version": 1,
        "audio_chunk_sha256": audio_chunk_sha256,
        "ref_audio_chunk_sha256": ref_audio_chunk_sha256,
        "ref_revision": entry.ref_revision,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "precision_profile": entry.precision_profile,
        "mapping_version": config.mapping_version,
        "mapping_config_hash": config.mapping_config_hash(),
        "upstream_artifact_hashes": dict(sorted(upstream_hashes.items())),
    }


def synth_key_components(
    entry: ManifestEntry,
    *,
    audio_chunk_sha256: str,
    ref_audio_chunk_sha256: str,
    params_artifact_hash: str,
    config: PipelineConfig,
) -> dict[str, Any]:
    return {
        "node_type": "synth_pcm",
        "schema_version": 1,
        "audio_chunk_sha256": audio_chunk_sha256,
        "ref_audio_chunk_sha256": ref_audio_chunk_sha256,
        "ref_revision": entry.ref_revision,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "precision_profile": entry.precision_profile,
        "mapping_version": config.mapping_version,
        "mapping_config_hash": config.mapping_config_hash(),
        "upstream_artifact_hashes": {"conversion_params": params_artifact_hash},
        "synth_version": config.synth_version,
    }


def pcm_artifact_from_array(
    pcm: np.ndarray,
    *,
    utterance_id: str,
    params_artifact_hash: str,
    sample_rate: int,
    frame_count: int,
) -> dict[str, Any]:
    raw = pcm.astype("<i2", copy=False).tobytes()
    return {
        "artifact_type": "synth_pcm",
        "schema_version": 1,
        "utterance_id": utterance_id,
        "sample_rate": sample_rate,
        "sample_count": int(len(pcm)),
        "frame_count": int(frame_count),
        "params_artifact_hash": params_artifact_hash,
        "pcm_sha256": sha256_bytes(raw),
        "pcm_b64": base64.b64encode(raw).decode("ascii"),
    }


def pcm_array_from_artifact(artifact: dict[str, Any]) -> np.ndarray:
    try:
        raw = base64.b64decode(artifact["pcm_b64"].encode("ascii"), validate=True)
    except (KeyError, ValueError) as exc:
        fail(
            "E_CACHE_CORRUPT",
            "PCM 缓存产物损坏",
            field="pcm_b64",
            details={"reason": str(exc)},
        )
    if sha256_bytes(raw) != artifact.get("pcm_sha256"):
        fail(
            "E_CACHE_CORRUPT",
            "PCM 缓存产物校验和不一致",
            field="pcm_sha256",
            details={"utterance_id": artifact.get("utterance_id")},
        )
    return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)


@dataclass
class CacheLookup:
    key: str
    components: dict[str, Any]
    artifact: Any
    artifact_hash: str
    hit: bool
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "key_components": self.components,
            "artifact_hash": self.artifact_hash,
            "hit": self.hit,
            "path": self.path,
        }


class ContentAddressedCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, node_type: str, key: str) -> Path:
        return self.root / node_type / key[:2] / f"{key}.json"

    def get_record(self, node_type: str, components: dict[str, Any]) -> CacheLookup | None:
        key = cache_key(components)
        path = self._path(node_type, key)
        if not path.is_file():
            return None
        try:
            record = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "缓存条目无法读取",
                field="cache_path",
                details={"path": str(path), "reason": str(exc)},
            )
        if record.get("key") != key or record.get("node_type") != node_type:
            fail(
                "E_CACHE_CORRUPT",
                "缓存键或节点类型与目录不一致",
                field="cache_key",
                details={"path": str(path)},
            )
        if record.get("key_components") != components:
            fail(
                "E_CACHE_CORRUPT",
                "缓存键组成与请求不一致",
                field="key_components",
                details={"path": str(path), "key": key},
            )
        artifact = record.get("artifact")
        actual_hash = artifact_hash(artifact)
        if actual_hash != record.get("artifact_hash"):
            fail(
                "E_CACHE_CORRUPT",
                "缓存产物哈希不一致",
                field="artifact_hash",
                details={"path": str(path), "key": key},
            )
        return CacheLookup(
            key=key,
            components=components,
            artifact=artifact,
            artifact_hash=actual_hash,
            hit=True,
            path=str(path),
        )

    def put(
        self,
        node_type: str,
        components: dict[str, Any],
        artifact: Any,
    ) -> CacheLookup:
        key = cache_key(components)
        computed_hash = artifact_hash(artifact)
        existing = self.get_record(node_type, components)
        if existing is not None:
            if existing.artifact_hash != computed_hash:
                fail(
                    "E_CACHE_CORRUPT",
                    "同一缓存键对应不同产物",
                    field="artifact_hash",
                    details={
                        "key": key,
                        "existing": existing.artifact_hash,
                        "computed": computed_hash,
                    },
                )
            return existing
        record = {
            "schema_version": 1,
            "node_type": node_type,
            "key": key,
            "key_components": components,
            "artifact_hash": computed_hash,
            "artifact": artifact,
        }
        path = self._path(node_type, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_bytes(record) + b"\n"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return CacheLookup(
            key=key,
            components=components,
            artifact=artifact,
            artifact_hash=computed_hash,
            hit=False,
            path=str(path),
        )

    def get_or_compute(
        self,
        node_type: str,
        components: dict[str, Any],
        compute: Callable[[], Any],
    ) -> CacheLookup:
        existing = self.get_record(node_type, components)
        if existing is not None:
            return existing
        return self.put(node_type, components, compute())

    def iter_records(self, node_type: str) -> list[dict[str, Any]]:
        base = self.root / node_type
        if not base.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(base.glob("*/*.json")):
            try:
                records.append(read_json(path))
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "E_CACHE_CORRUPT",
                    "缓存条目无法读取",
                    field="cache_path",
                    details={"path": str(path), "reason": str(exc)},
                )
        return records


class ResidentEmbeddingCache:
    """Small deterministic LRU used to enforce the resident embedding limit."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self._values: dict[str, Any] = {}
        self._order: list[str] = []
        self.evictions = 0

    def get(self, key: str) -> Any | None:
        if key not in self._values:
            return None
        self._order.remove(key)
        self._order.append(key)
        return self._values[key]

    def put(self, key: str, value: Any) -> None:
        if key in self._values:
            self._order.remove(key)
        self._values[key] = value
        self._order.append(key)
        while len(self._order) > self.maximum:
            oldest = self._order.pop(0)
            self._values.pop(oldest, None)
            self.evictions += 1

    def __len__(self) -> int:
        return len(self._values)

