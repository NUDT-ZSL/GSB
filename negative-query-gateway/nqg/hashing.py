"""Canonical seeded bit-position derivation for the Bloom kernel.

The contract shared by every schema version is:

    digest_i = BLAKE2b( seed_i (4-byte big endian) || byte_key )
    index_i  = int.from_bytes(digest_i, big endian) mod bit_array_size

All three schemas normalize to the same byte keys and the same integer seed
vector, so a given key always maps to the same ordered bit positions regardless
of which historical schema registered it.

Only the standard library (``hashlib``) is used.
"""

from __future__ import annotations

import hashlib


def seeded_digest(seed: int, key: bytes) -> bytes:
    """Return the raw keyed digest for one (seed, key) pair."""

    return hashlib.blake2b(seed.to_bytes(4, "big") + key).digest()


def bit_index(seed: int, key: bytes, bit_array_size: int) -> int:
    """Derive one bit index for ``key`` under ``seed``."""

    return int.from_bytes(seeded_digest(seed, key), "big") % bit_array_size


def bit_positions(seeds: tuple[int, ...], key: bytes, bit_array_size: int) -> tuple[int, ...]:
    """Ordered bit positions touched by every hash function for ``key``."""

    return tuple(bit_index(seed, key, bit_array_size) for seed in seeds)
