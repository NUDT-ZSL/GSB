"""Audio chunk hashing and PCM helpers."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np


def pcm_bytes(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes(order="C")


def audio_chunks(samples: np.ndarray, chunk_samples: int) -> list[dict[str, Any]]:
    raw = pcm_bytes(samples)
    bytes_per_sample = 2
    chunks: list[dict[str, Any]] = []
    for index, sample_start in enumerate(range(0, len(samples), chunk_samples)):
        byte_start = sample_start * bytes_per_sample
        byte_end = min(len(raw), byte_start + chunk_samples * bytes_per_sample)
        blob = raw[byte_start:byte_end]
        chunks.append(
            {
                "index": index,
                "sample_start": sample_start,
                "sample_count": len(blob) // bytes_per_sample,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    return chunks


def chunk_hashes(samples: np.ndarray, chunk_samples: int) -> list[str]:
    return [chunk["sha256"] for chunk in audio_chunks(samples, chunk_samples)]
