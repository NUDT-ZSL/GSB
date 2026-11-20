"""Cache key construction.

Every node key is the content hash of a component dict. Components chain
upstream hashes, so changing any input or calibration parameter invalidates
exactly the dependent suffix of the pipeline. Components are exported per
node for diagnostics.
"""

from __future__ import annotations

from .codec import hash_obj
from .nodes import (
    DECISION_VERSION,
    FEATURES_VERSION,
    NOISE_VERSION,
    SEGMENTS_VERSION,
)

NODE_ORDER = ("features", "noise", "decision", "segments")


def build_components(pcm_hash, truth_hash, cfg, duration_ms):
    features = {
        "node": "features",
        "version": FEATURES_VERSION,
        "pcm_sha256": pcm_hash,
        "sample_rate": cfg.sample_rate,
        "frame_ms": cfg.frame_ms,
        "hop_ms": cfg.hop_ms,
    }
    noise = {
        "node": "noise",
        "version": NOISE_VERSION,
        "features": hash_obj(features),
        "truth_sha256": truth_hash,
        "percentile": cfg.percentile,
        "percentile_window": cfg.percentile_window,
        "param_version": cfg.param_version,
    }
    decision = {
        "node": "decision",
        "version": DECISION_VERSION,
        "features": hash_obj(features),
        "noise": hash_obj(noise),
        "mult_low": cfg.mult_low,
        "mult_high": cfg.mult_high,
        "param_version": cfg.param_version,
    }
    segments = {
        "node": "segments",
        "version": SEGMENTS_VERSION,
        "decision": hash_obj(decision),
        "min_speech_ms": cfg.min_speech_ms,
        "min_silence_ms": cfg.min_silence_ms,
        "duration_ms": duration_ms,
        "param_version": cfg.param_version,
    }
    return {
        "features": features,
        "noise": noise,
        "decision": decision,
        "segments": segments,
    }


def keys_from_components(components):
    return {node: hash_obj(comp) for node, comp in components.items()}
