from __future__ import annotations

import hashlib
from typing import Iterable

import numpy as np

from .canonical import sha256_value


def _derive_entropy(seed: int, seq: int, delta: int) -> int:
    material = f"card-replay-rng:v1|{seed}|{seq}|{delta}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def derive_rng_stream(seed: int, seq: int, delta: int, size: int) -> list[int]:
    """Derive a random vector using only (seed, seq, delta, size).

    NumPy's PCG64 bit stream is deterministic for a fixed integer seed. The
    entropy itself is SHA-256-derived so the mapping from the three business
    inputs is stable and independent of process state or worker scheduling.
    """

    if size < 1:
        raise ValueError("rng stream size must be positive")
    generator = np.random.Generator(np.random.PCG64(_derive_entropy(seed, seq, delta)))
    values = generator.integers(0, 2**64, size=size, dtype=np.uint64)
    return [int(value) for value in values]


def rng_stream_hash(values: Iterable[int]) -> str:
    return sha256_value(list(values))
