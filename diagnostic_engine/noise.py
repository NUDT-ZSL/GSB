"""Deterministic NumPy noise generation."""
from __future__ import annotations

import hashlib

import numpy as np

from .models import ClipTask


def _clip_entropy(clip_id: str) -> int:
    digest = hashlib.sha256(clip_id.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) % (2**32)


def frame_noise(
    clip_id: str,
    noise_seed: int,
    frame_index: int,
    frame_shape: tuple[int, ...],
) -> np.ndarray:
    """Return the independently seeded noise excitation for one frame.

    Per-frame SeedSequence values make recovery independent of RNG call order.
    """
    seed_sequence = np.random.SeedSequence(
        [int(noise_seed) % (2**32), _clip_entropy(clip_id), int(frame_index)]
    )
    generator = np.random.default_rng(seed_sequence)
    return generator.integers(0, 256, size=tuple(frame_shape), dtype=np.uint8)


def noise_bank(task: ClipTask) -> np.ndarray:
    """Generate the full ``(total_frames, *frame_shape)`` deterministic bank."""
    bank = np.empty((task.total_frames, *task.frame_shape), dtype=np.uint8)
    for index in range(task.total_frames):
        bank[index] = frame_noise(
            task.clip_id, task.noise_seed, index, task.frame_shape
        )
    return bank


def initial_noise_frame(task: ClipTask) -> np.ndarray:
    return frame_noise(task.clip_id, task.noise_seed, 0, task.frame_shape)
