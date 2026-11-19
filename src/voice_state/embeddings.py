"""Versioned registry embedding cache with whole-entry replacement semantics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, checksum_value, read_json
from .config import AnalysisConfig
from .dataset import Dataset
from .errors import BusinessError, E_SNAPSHOT_CORRUPT
from .features import compute_embedding_statistics, spectral_descriptors
from .synth import registry_clip


def cache_filename(registry_version: int, registry_checksum: str) -> str:
    return f"registry-v{registry_version}-{registry_checksum[:16]}.json"


def load_or_build_embedding_cache(
    dataset: Dataset,
    config: AnalysisConfig,
    cache_dir: Path,
) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_filename(dataset.registry_version, dataset.registry_checksum)
    if path.is_file():
        cached = _load_cache(path, dataset, config)
        return cached
    speakers: dict[str, Any] = {}
    for speaker_id in dataset.speaker_ids:
        clip_descriptors = []
        for row in dataset.registry_rows:
            if int(row["speaker_id"]) != speaker_id:
                continue
            clip = registry_clip(
                speaker_id=speaker_id,
                clip_seed=int(row["clip_seed"]),
                clip_ms=int(row["clip_ms"]),
            )
            clip_descriptors.append(spectral_descriptors(clip, config))
        descriptors = (
            np.concatenate(clip_descriptors, axis=0)
            if clip_descriptors
            else np.zeros((0, 8), dtype=np.float64)
        )
        statistics = compute_embedding_statistics(descriptors, config)
        speaker_payload = {
            "speaker_id": speaker_id,
            "registry_version": dataset.registry_version,
            "mean": statistics["mean"],
            "std": statistics["std"],
            "frame_count": statistics["frame_count"],
        }
        speaker_payload["checksum"] = checksum_value(speaker_payload)
        speakers[str(speaker_id)] = speaker_payload
    payload: dict[str, Any] = {
        "schema_version": 1,
        "cache_kind": "speaker_embedding_statistics",
        "registry_version": dataset.registry_version,
        "registry_checksum": dataset.registry_checksum,
        "config_checksum": config.checksum(),
        "speakers": speakers,
    }
    payload["checksum"] = checksum_value(payload)
    atomic_write_json(path, payload)
    return payload


def _load_cache(path: Path, dataset: Dataset, config: AnalysisConfig) -> dict[str, Any]:
    try:
        payload = read_json(path)
        stored_checksum = payload.pop("checksum")
        actual_checksum = checksum_value(payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "Speaker embedding cache is corrupt",
            {
                "stage": "segmentation",
                "component": "speaker_registry",
                "version": dataset.registry_version,
                "path": str(path),
            },
        ) from exc
    if actual_checksum != stored_checksum:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "Speaker embedding cache checksum mismatch",
            {
                "stage": "segmentation",
                "component": "speaker_registry",
                "version": dataset.registry_version,
                "expected_checksum": stored_checksum,
                "actual_checksum": actual_checksum,
            },
        )
    if (
        payload.get("registry_version") != dataset.registry_version
        or payload.get("registry_checksum") != dataset.registry_checksum
        or payload.get("config_checksum") != config.checksum()
    ):
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "Speaker embedding cache metadata does not match the active registry",
            {
                "stage": "segmentation",
                "component": "speaker_registry",
                "version": dataset.registry_version,
                "path": str(path),
            },
        )
    for speaker_id, entry in payload.get("speakers", {}).items():
        try:
            stored = entry.pop("checksum")
            actual = checksum_value(entry)
        except (KeyError, TypeError, AttributeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Speaker embedding entry is corrupt",
                {
                    "stage": "segmentation",
                    "component": "speaker_registry",
                    "version": dataset.registry_version,
                    "speaker_id": speaker_id,
                },
            ) from exc
        if actual != stored:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Speaker embedding entry checksum mismatch",
                {
                    "stage": "segmentation",
                    "component": "speaker_registry",
                    "version": dataset.registry_version,
                    "speaker_id": speaker_id,
                },
            )
        entry["checksum"] = stored
    return {**payload, "checksum": stored_checksum}


def embedding_statistics(cache: dict[str, Any]) -> dict[int, dict]:
    return {
        int(speaker_id): {
            "mean": entry["mean"],
            "std": entry["std"],
            "frame_count": entry["frame_count"],
        }
        for speaker_id, entry in cache["speakers"].items()
    }

