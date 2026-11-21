from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np

from .deterministic import (
    deterministic_floats,
    deterministic_matrix,
    deterministic_pixels,
    token_vector,
)
from .errors import PipelineError


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.sqrt(np.sum(vector * vector)))
    if norm == 0.0 or not np.isfinite(norm):
        raise PipelineError(
            "E_INCONSISTENT",
            "Cannot normalize a zero or non-finite vector",
            stage="align",
            field="vector",
        )
    return (vector / norm).astype(np.float64)


def _matvec(matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
    """Deterministic matrix-vector product without threaded BLAS reductions."""
    return np.sum(matrix * vector.reshape(1, -1), axis=1, dtype=np.float64)


def encode_image(sample: dict[str, Any], config: dict[str, Any]) -> dict[str, np.ndarray]:
    image = config["image"]
    shape = tuple(int(x) for x in image["shape"])
    block_h, block_w = (int(x) for x in image["block_size"])
    height, width, channels = shape
    pixels = deterministic_pixels(int(sample["image_seed"]), shape)
    block_means = pixels.astype(np.float64).reshape(
        height // block_h,
        block_h,
        width // block_w,
        block_w,
        channels,
    ).mean(axis=(1, 3))
    features = np.ascontiguousarray(block_means.reshape(-1), dtype=np.float64)
    projection = deterministic_matrix(
        int(image["embed_dim"]),
        int(features.size),
        tag=f"image_projection|seed={image['projection_seed']}",
    )
    embedding = _matvec(projection, features)
    return {"embedding": embedding.astype(np.float64)}


def encode_text(
    sample: dict[str, Any],
    vocab: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, np.ndarray]:
    lang = sample["text_lang"]
    weights = vocab["languages"][lang]["weights"]
    tokens = sample["text"].split()
    counts = Counter(tokens)
    dim = int(config["text"]["embed_dim"])
    version = int(vocab["version"])
    pooled = np.zeros(dim, dtype=np.float64)
    denominator = 0.0
    for token in sorted(counts):
        if token not in weights:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Token {token!r} is absent from vocab language {lang}",
                stage="text_encoder",
                field="text",
                pair_id=sample["pair_id"],
            )
        weight = float(weights[token]) * float(counts[token])
        vector = np.asarray(token_vector(lang, token, dim, version), dtype=np.float64)
        pooled += vector * weight
        denominator += weight
    if denominator <= 0.0:
        raise PipelineError(
            "E_TEXT_EMPTY",
            "Text produced zero pooled weight",
            stage="text_encoder",
            field="text",
            pair_id=sample["pair_id"],
        )
    pooled /= denominator
    return {"embedding": _normalize(pooled)}


def align_embeddings(
    image_embedding: np.ndarray,
    text_embedding: np.ndarray,
    config: dict[str, Any],
    *,
    pair_id: str,
) -> dict[str, np.ndarray]:
    align = config["align"]
    dim = int(align["dim"])
    scale = float(align.get("scale", 1.0))
    image_projection = deterministic_matrix(
        dim,
        int(image_embedding.size),
        tag=f"align_image|seed={align['seed']}|scale={scale:.17g}",
    )
    text_projection = deterministic_matrix(
        dim,
        int(text_embedding.size),
        tag=f"align_text|seed={align['seed']}|scale={scale:.17g}",
    )
    image_bias = deterministic_floats(
        f"align_image_bias|seed={align['seed']}|dim={dim}",
        dim,
    ) * 0.01
    text_bias = deterministic_floats(
        f"align_text_bias|seed={align['seed']}|dim={dim}",
        dim,
    ) * 0.01
    image_aligned = _normalize(
        (_matvec(image_projection, image_embedding) + image_bias) * scale
    )
    text_aligned = _normalize(
        (_matvec(text_projection, text_embedding) + text_bias) * scale
    )
    if not np.isfinite(image_aligned).all() or not np.isfinite(text_aligned).all():
        raise PipelineError(
            "E_INCONSISTENT",
            "Alignment produced non-finite values",
            stage="align",
            field="embedding",
            pair_id=pair_id,
        )
    return {
        "image_aligned": image_aligned.astype(np.float64),
        "text_aligned": text_aligned.astype(np.float64),
    }


def quantize_vectors(
    vectors: np.ndarray,
    centroids: np.ndarray,
) -> dict[str, np.ndarray]:
    vectors = np.asarray(vectors, dtype=np.float64)
    centroids = np.asarray(centroids, dtype=np.float64)
    if vectors.ndim != 2 or centroids.ndim != 2:
        raise PipelineError(
            "E_INCONSISTENT",
            "Vectors and centroids must be matrices",
            stage="index_build",
            field="shape",
        )
    if vectors.shape[1] != centroids.shape[1]:
        raise PipelineError(
            "E_INCONSISTENT",
            "Vector dimension does not match codebook dimension",
            stage="index_build",
            field="dim",
        )
    distances = np.sum(
        (vectors[:, None, :] - centroids[None, :, :]) ** 2,
        axis=2,
        dtype=np.float64,
    )
    codes = np.argmin(distances, axis=1).astype(np.int32)
    residuals = (vectors - centroids[codes]).astype(np.float64)
    return {
        "codes": codes,
        "residuals": residuals,
        "centroids": centroids.astype(np.float64),
    }


def cosine_scores(query: np.ndarray, targets: np.ndarray) -> np.ndarray:
    query = np.asarray(query, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    if targets.size == 0:
        return np.zeros(0, dtype=np.float64)
    query_norm = float(np.sqrt(np.sum(query * query)))
    target_norms = np.sqrt(np.sum(targets * targets, axis=1, dtype=np.float64))
    if query_norm == 0.0 or np.any(target_norms == 0.0):
        raise PipelineError(
            "E_INCONSISTENT",
            "Cosine score encountered a zero vector",
            stage="retrieve",
            field="vector",
        )
    dots = np.sum(targets * query.reshape(1, -1), axis=1, dtype=np.float64)
    return (dots / (query_norm * target_norms)).astype(np.float64)

