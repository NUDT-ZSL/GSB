"""Seed derivation isolated from Python's randomized hash()."""

from __future__ import annotations

import hashlib

import numpy as np


def derived_seed(*parts: object) -> int:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:16], "little")


def rng_for(*parts: object) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(derived_seed(*parts)))

