"""Deterministic hashing helpers.

Only :mod:`hashlib` (blake2b) is used so the digest of any logical object is
bit-for-bit stable across runs, machines and operating systems.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Tuple


def digest_parts(parts: Iterable[Tuple[str, object]]) -> str:
    """Hash an ordered sequence of ``(label, value)`` pairs into a hex digest."""
    hasher = hashlib.blake2b(digest_size=32)
    for label, value in parts:
        hasher.update(b"f:")
        hasher.update(label.encode("utf-8"))
        hasher.update(b"=")
        hasher.update(repr(value).encode("utf-8"))
        hasher.update(b"|")
    return hasher.hexdigest()


def digest_bytes(payload: bytes) -> str:
    """Hash a raw byte payload."""
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def combine_digests(label: str, digests: Iterable[str]) -> str:
    """Order-sensitive combination of child digests."""
    return digest_parts([(label, tuple(digests))])
