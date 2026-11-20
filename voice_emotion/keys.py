"""Dependency-aware cache key construction.

Each node key contains only the dependencies that can change that node's
output.  Downstream keys carry both the parent cache key and the parent output
hash, so the final dialogue key transitively covers every upstream artifact.
The cache entry separately records the complete run context for audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .canonical import hash_value
from .config import (
    AggregationConfig,
    FeatureConfig,
    RegressionConfig,
    SpeakerConfig,
)


KEY_SCHEMA = "vem.node-cache-key.v1"


@dataclass(frozen=True)
class NodeKey:
    cache_key: str
    components: dict[str, Any]
    upstream_hash: str


def _finish(node: str, components: dict[str, Any]) -> NodeKey:
    body = {"schema": KEY_SCHEMA, "node": node, **components}
    return NodeKey(
        cache_key=hash_value(body),
        components=body,
        upstream_hash=str(components["upstream_hash"]),
    )


def feature_key(
    *,
    dialog_id: str,
    audio_identity: dict[str, Any],
    config: FeatureConfig,
    dtype: str,
) -> NodeKey:
    components = {
        "node": "features",
        "dialog_id": dialog_id,
        "upstream_node": "audio-pcm-chunks",
        "upstream_hash": str(audio_identity["chunk_root"]),
        "audio_pcm_hash": str(audio_identity["pcm_hash"]),
        "audio_chunk_hashes": list(audio_identity["chunk_hashes"]),
        "audio_chunk_root": str(audio_identity["chunk_root"]),
        "audio_sample_count": int(audio_identity["sample_count"]),
        "feature_params_version": config.version,
        "feature_params_hash": config.params_hash(),
        "frame_ms": float(config.frame_ms),
        "frame_shift_ms": float(config.frame_shift_ms),
        "frame_samples": config.frame_samples(),
        "hop_samples": config.hop_samples(),
        "fft_size": int(config.fft_size),
        "rolloff_percent": float(config.rolloff_percent),
        "window": config.window,
        "feature_dtype": dtype,
    }
    return _finish("features", components)


def assignment_key(
    *,
    dialog_id: str,
    speaker_ids: Sequence[int],
    feature_key_value: str,
    feature_hash: str,
    enrollment_version: str,
    feature_config: FeatureConfig,
    speaker_config: SpeakerConfig,
) -> NodeKey:
    components = {
        "node": "assignment",
        "dialog_id": dialog_id,
        "speaker_ids": sorted({int(item) for item in speaker_ids}),
        "upstream_node": "features",
        "upstream_cache_key": feature_key_value,
        "upstream_hash": feature_hash,
        "feature_params_version": feature_config.version,
        "feature_params_hash": feature_config.params_hash(),
        "enrollment_version": enrollment_version,
        "frame_shift_ms": float(feature_config.frame_shift_ms),
        "segment_threshold": int(speaker_config.segment_threshold),
        "speaker_config_hash": speaker_config.params_hash(),
        "similarity": speaker_config.similarity,
        "tie_break": speaker_config.tie_break,
    }
    return _finish("assignment", components)


def segment_key(
    *,
    dialog_id: str,
    segment_id: str,
    start_frame: int,
    end_frame: int,
    speaker_id: int,
    assignment_key_value: str,
    assignment_hash: str,
    truth_slice_hash: str,
    enrollment_version: str,
    feature_config: FeatureConfig,
    speaker_config: SpeakerConfig,
    regression_config: RegressionConfig,
) -> NodeKey:
    components = {
        "node": "segment",
        "dialog_id": dialog_id,
        "segment_id": segment_id,
        "start_frame": int(start_frame),
        "end_frame": int(end_frame),
        "speaker_id": int(speaker_id),
        "upstream_node": "assignment",
        "upstream_cache_key": assignment_key_value,
        "upstream_hash": assignment_hash,
        "truth_correction_version": truth_slice_hash,
        "truth_slice_hash": truth_slice_hash,
        "enrollment_version": enrollment_version,
        "feature_params_version": feature_config.version,
        "feature_params_hash": feature_config.params_hash(),
        "frame_shift_ms": float(feature_config.frame_shift_ms),
        "segment_threshold": int(speaker_config.segment_threshold),
        "regression_config_hash": regression_config.params_hash(),
        "regression_version": regression_config.version,
    }
    return _finish("segment", components)


def dialog_key(
    *,
    dialog_id: str,
    segment_cache_keys: Sequence[str],
    segment_hashes: Sequence[str],
    truth_version: str,
    enrollment_version: str,
    feature_config: FeatureConfig,
    speaker_config: SpeakerConfig,
    regression_config: RegressionConfig,
    aggregation_config: AggregationConfig,
) -> NodeKey:
    keys = [str(item) for item in segment_cache_keys]
    hashes = [str(item) for item in segment_hashes]
    if len(keys) != len(hashes):
        raise ValueError("segment key/hash counts differ")
    upstream_hash = hash_value(
        {"kind": "segment-set", "segment_cache_keys": keys, "segment_hashes": hashes}
    )
    components = {
        "node": "dialog",
        "dialog_id": dialog_id,
        "upstream_node": "segments",
        "upstream_cache_keys": keys,
        "segment_cache_keys": keys,
        "segment_hashes": hashes,
        "upstream_hash": upstream_hash,
        "truth_correction_version": truth_version,
        "truth_version": truth_version,
        "enrollment_version": enrollment_version,
        "feature_params_version": feature_config.version,
        "feature_params_hash": feature_config.params_hash(),
        "frame_shift_ms": float(feature_config.frame_shift_ms),
        "segment_threshold": int(speaker_config.segment_threshold),
        "regression_config_hash": regression_config.params_hash(),
        "aggregation_config_hash": aggregation_config.params_hash(),
        "aggregation_version": aggregation_config.version,
    }
    return _finish("dialog", components)
