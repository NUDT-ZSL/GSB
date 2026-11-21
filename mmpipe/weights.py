"""Fixed, seed-derived constants shared by every process.

All matrices are built once from a master ``numpy.random.default_rng`` keyed on
the explicit config seed. They are plain float32 arrays, identical byte-for-byte
in every run and every worker, so embeddings depend only on the input pair and
the config — never on ambient state.
"""

from __future__ import annotations

import numpy as np

from .config import PipelineConfig


def expected_image_shape(seed: int, block_size: int) -> tuple:
    """Deterministic expected (H, W, 3) shape implied by an image seed."""
    rng = np.random.default_rng(seed)
    h_blocks = int(rng.integers(4, 9))
    w_blocks = int(rng.integers(4, 9))
    return (h_blocks * block_size, w_blocks * block_size, 3)


def generate_image(seed: int, shape: tuple) -> np.ndarray:
    """Generate the deterministic uint8 RGB pixel array for a seed/shape."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=shape, dtype=np.uint8)


class FixedWeights:
    """Immutable bundle of every learned-looking constant in the pipeline."""

    def __init__(self, config: PipelineConfig) -> None:
        rng = np.random.default_rng(config.master_seed)
        block_feats = config.block_size * config.block_size * 3
        self.image_proj = rng.standard_normal((block_feats, config.image_dim)).astype(np.float32)
        self.text_embed = rng.standard_normal((config.vocab_size, config.text_dim)).astype(np.float32)
        self.gate = rng.standard_normal(config.fusion_dim).astype(np.float32)
        self.score_ref = rng.standard_normal(config.fusion_dim).astype(np.float32)

        self.image_proj.setflags(write=False)
        self.text_embed.setflags(write=False)
        self.gate.setflags(write=False)
        self.score_ref.setflags(write=False)

        self.config = config

    def digest_material(self) -> bytes:
        parts = [
            self.image_proj.tobytes(),
            self.text_embed.tobytes(),
            self.gate.tobytes(),
            self.score_ref.tobytes(),
        ]
        return b"|".join(parts)

