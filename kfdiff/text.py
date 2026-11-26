"""Controlled vocabulary and deterministic TF-weighted condition embeddings.

The token embedding table and the canonical normalization statistics are
derived from the fixed vocabulary seed.  Both the one-shot whole-clip path and
the block path call :func:`condition_embedding` and therefore share exactly
the same source and normalization.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

import numpy as np

from .hashing import hash_json


CONTROLLED_VOCAB: Dict[str, Tuple[str, ...]] = {
    "style": ("cinematic", "anime", "documentary", "noir", "watercolor", "cyberpunk"),
    "subject": ("lighthouse", "fox", "train", "market", "astronaut", "harbor", "forest", "dancer"),
    "action": ("walking", "running", "glowing", "drifting", "turning", "ascending"),
    "scene": ("cliff", "neon_street", "pine_forest", "rooftop", "river_bank", "station"),
    "lighting": ("golden_hour", "moonlight", "soft_rain", "backlit_fog", "blue_neon", "dawn"),
    "motion": ("slow_pan", "dolly_forward", "aerial_orbit", "handheld", "static_wide", "tracking"),
}


def build_vocab_table(embedding_dim: int, vocab_seed: int) -> Dict[str, np.ndarray]:
    """Deterministic token vectors for every controlled token."""
    rng = np.random.default_rng(vocab_seed)
    table: Dict[str, np.ndarray] = {}
    for tokens in CONTROLLED_VOCAB.values():
        for token in tokens:
            if token not in table:
                table[token] = rng.standard_normal(embedding_dim).astype(np.float64)
    table["<unk>"] = rng.standard_normal(embedding_dim).astype(np.float64)
    return table


def canonical_norm_stats(table: Dict[str, np.ndarray], eps: float) -> Dict[str, Any]:
    """Pooled mean/std over the fixed vocabulary matrix (same source for all)."""
    matrix = np.stack(list(table.values()), axis=0)
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0) + eps
    return {
        "mean": mean.astype(np.float64),
        "std": std.astype(np.float64),
        "eps": eps,
        "stats_hash": hash_json(
            {"mean": mean.round(12).tolist(), "std": std.round(12).tolist(), "eps": eps}
        ),
    }


_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(prompt: str) -> List[str]:
    return _TOKEN_RE.findall(prompt.lower())


def term_frequencies(tokens: List[str]) -> Dict[str, int]:
    tf: Dict[str, int] = {}
    for token in tokens:
        tf[token] = tf.get(token, 0) + 1
    return tf


def condition_embedding(prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the TF-weighted pooled + canonically normalized embedding.

    The result includes every provenance hash needed by cache keys and by the
    independent-recomputation self-check.
    """
    dim = int(config["embedding_dim"])
    vocab_seed = int(config["vocab_seed"])
    eps = float(config["diffusion"]["normalization_eps"])
    table = build_vocab_table(dim, vocab_seed)
    stats = canonical_norm_stats(table, eps)

    tokens = tokenize(prompt)
    tf = term_frequencies(tokens)
    pooled = np.zeros(dim, dtype=np.float64)
    weight_sum = 0.0
    used_weights: Dict[str, float] = {}
    for token, count in tf.items():
        vector = table.get(token, table["<unk>"])
        weight = float(count)  # pure term-frequency weighting
        pooled += weight * vector
        weight_sum += weight
        used_weights[token] = weight
    if weight_sum > 0:
        pooled /= weight_sum
    normalized = (pooled - stats["mean"]) / stats["std"]

    embedding = normalized.astype(np.float32)
    embedding_hash = hash_json(
        {
            "prompt": prompt,
            "embedding_dim": dim,
            "vocab_seed": vocab_seed,
            "weights": used_weights,
            "vector": [round(float(v), 8) for v in embedding],
            "norm_stats_hash": stats["stats_hash"],
        }
    )
    return {
        "prompt": prompt,
        "tokens": tokens,
        "term_frequencies": tf,
        "weights": used_weights,
        "embedding": embedding,
        "embedding_hash": embedding_hash,
        "norm_stats_hash": stats["stats_hash"],
    }


def prompt_channel_projection(embedding: np.ndarray, frame_channels: int, vocab_seed: int) -> Tuple[np.ndarray, np.ndarray]:
    """Project the condition embedding to per-channel scale/shift vectors."""
    dim = int(embedding.shape[0])
    rng = np.random.default_rng(vocab_seed + 4242 + frame_channels)
    proj = rng.standard_normal((dim, 2 * frame_channels)).astype(np.float64) / np.sqrt(dim)
    out = embedding.astype(np.float64) @ proj
    scale = np.tanh(out[:frame_channels]) * 0.6
    shift = np.tanh(out[frame_channels:]) * 0.5
    return scale.astype(np.float32), shift.astype(np.float32)
