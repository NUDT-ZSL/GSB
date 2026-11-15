"""Deterministic float32 <-> 16-bit PCM conversion and content hashing.

All processing happens on float32 waveforms in [-1.0, 1.0]. Every state-tree
node is *quantized* to 16-bit PCM before it is cached or exported, which makes
the byte output reproducible across runs, platforms and Python/NumPy
versions (the rounding rule is implemented explicitly instead of relying on
NumPy's banker's-rounding cast behaviour).
"""

from __future__ import annotations

import hashlib

import numpy as np

PCM_SCALE = 32768.0
PCM_MAX = 32767
PCM_MIN = -32768


def float_to_pcm16(samples: np.ndarray) -> np.ndarray:
    """Quantize float samples to little-endian int16 deterministically.

    Round half away from zero, then hard-clip to the int16 range. ``+0.5`` and
    ``-0.5`` therefore always round to ``+1`` / ``-1`` regardless of platform.
    """

    x = np.asarray(samples, dtype=np.float64)
    scaled = x * PCM_SCALE
    rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    clipped = np.clip(rounded, PCM_MIN, PCM_MAX)
    return clipped.astype("<i2")


def pcm16_to_float(pcm: np.ndarray) -> np.ndarray:
    """Convert int16 PCM back to float32 (division by 32768.0)."""

    return np.asarray(pcm, dtype="<i2").astype(np.float32) / np.float32(PCM_SCALE)


def pcm_bytes(pcm: np.ndarray) -> bytes:
    """Canonical little-endian int16 byte representation."""

    return np.asarray(pcm, dtype="<i2").tobytes()


def hash_pcm(pcm: np.ndarray) -> str:
    """SHA-256 content hash of the canonical PCM byte representation."""

    return hashlib.sha256(pcm_bytes(pcm)).hexdigest()
