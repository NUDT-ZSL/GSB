"""The four collaborative inference stages as deterministic pure functions.

Stage semantics:
  image  : block mean pooling -> fixed-weight projection -> image embedding.
  text   : frequency-weighted vocabulary pooling -> text embedding.
  fusion : modality_mask selects concat or weighted gating (mask 3 only).
  score  : cosine similarity against a fixed reference vector plus bias.

Everything is float32 and uses only the explicit config and fixed weights, so
the same (pair_id, image_seed) always yields byte-identical arrays.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np

from .config import PipelineConfig
from .errors import MaskInvalidError
from .manifest import PairRecord, token_index
from .weights import FixedWeights, generate_image

IMAGE_STAGE = "image"
TEXT_STAGE = "text"
FUSION_STAGE = "fusion"
SCORE_STAGE = "score"


def load_pixels(record: PairRecord, images_dir: Path) -> np.ndarray:
    pixels = np.load(Path(images_dir) / f"{record.pair_id}.npy")
    expected = generate_image(record.image_seed, tuple(record.image_shape))
    if pixels.shape != tuple(record.image_shape) or not np.array_equal(pixels, expected):
        # The pixel file is part of the seed-derived input; guard against drift.
        from .errors import SeedShapeError

        raise SeedShapeError(
            "stored pixel array does not match the deterministic image_seed output",
            stage=IMAGE_STAGE,
            pair_id=record.pair_id,
        )
    return pixels


def image_encode(pixels: np.ndarray, config: PipelineConfig, weights: FixedWeights) -> np.ndarray:
    block = config.block_size
    height, width, _channels = pixels.shape
    h_blocks = height // block
    w_blocks = width // block
    # Reshape into (h_blocks, block, w_blocks, block, 3) then merge block axes.
    blocks = pixels.reshape(h_blocks, block, w_blocks, block, 3)
    blocks = blocks.transpose(0, 2, 1, 3, 4).reshape(h_blocks * w_blocks, block * block * 3)
    block_means = blocks.astype(np.float32).mean(axis=0)
    assert block_means.shape == (block * block * 3,)
    embedding = (block_means @ weights.image_proj).astype(np.float32)
    assert embedding.shape == (config.image_dim,)
    return np.ascontiguousarray(embedding, dtype=np.float32)


def text_encode(record: PairRecord, config: PipelineConfig, weights: FixedWeights) -> np.ndarray:
    if len(record.text_tokens) == 0:
        from .errors import TextEmptyError

        raise TextEmptyError(
            "text_tokens must not be empty", stage=TEXT_STAGE, pair_id=record.pair_id
        )
    counts: Dict[int, int] = {}
    for token in record.text_tokens:
        idx = token_index(token)
        counts[idx] = counts.get(idx, 0) + 1
    total = float(sum(counts.values()))
    embedding = np.zeros(config.text_dim, dtype=np.float32)
    for idx in sorted(counts):  # sorted accumulation order => reproducible sum
        weight = np.float32(counts[idx] / total)
        embedding = embedding + weight * weights.text_embed[idx]
    assert embedding.shape == (config.text_dim,)
    return np.ascontiguousarray(embedding, dtype=np.float32)


def fuse(
    record: PairRecord,
    image_emb: np.ndarray,
    text_emb: np.ndarray,
    config: PipelineConfig,
    weights: FixedWeights,
) -> np.ndarray:
    mask = record.modality_mask
    zero_image = np.zeros(config.image_dim, dtype=np.float32)
    zero_text = np.zeros(config.text_dim, dtype=np.float32)
    if mask == 1:
        fused = np.concatenate([image_emb, zero_text])
    elif mask == 2:
        fused = np.concatenate([zero_image, text_emb])
    elif mask == 3:
        concat = np.concatenate([image_emb, text_emb]).astype(np.float32)
        # Weighted gate: sigmoid of a fixed linear probe, blended with concat.
        logits = concat @ weights.gate
        logit = float(logits)
        if logit >= 0:
            gate_value = np.float32(1.0 / (1.0 + np.exp(-logit)))
        else:
            exp_logit = np.exp(logit)
            gate_value = np.float32(exp_logit / (1.0 + exp_logit))
        eps = np.float32(config.gate_eps)
        fused = (np.float32(1.0 - eps) * gate_value + eps) * concat
    else:
        raise MaskInvalidError(
            f"modality_mask {mask} is not one of (1, 2, 3)",
            stage=FUSION_STAGE,
            pair_id=record.pair_id,
        )
    assert fused.shape == (config.fusion_dim,)
    return np.ascontiguousarray(fused.astype(np.float32))


def score(
    fused: np.ndarray, config: PipelineConfig, weights: FixedWeights
) -> np.ndarray:
    fused = fused.astype(np.float32)
    norm_f = np.linalg.norm(fused).astype(np.float32)
    norm_r = np.linalg.norm(weights.score_ref).astype(np.float32)
    if norm_f == 0.0:
        similarity = np.float32(0.0)
    else:
        similarity = (np.dot(fused, weights.score_ref) / (norm_f * norm_r)).astype(np.float32)
    final = (similarity + np.float32(config.score_bias)).astype(np.float32)
    return np.array(final, dtype=np.float32)

