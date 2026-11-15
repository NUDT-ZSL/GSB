"""Small, version-stable deterministic PRNG.

The solver never uses Python's global random state, NumPy's global state, the
clock, or process hashes to make decisions.  SplitMix64 is used here because
its output is fully specified and easy to reproduce across processes.
"""

from __future__ import annotations

import hashlib
from typing import Sequence, TypeVar

_MASK64 = (1 << 64) - 1
_T = TypeVar("_T")


class DeterministicRNG:
    def __init__(self, seed: int) -> None:
        self._state = int(seed) & _MASK64

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return (z ^ (z >> 31)) & _MASK64

    def randbelow(self, limit: int) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        # Rejection sampling avoids a modulo bias while remaining stable.
        cutoff = ((1 << 64) // limit) * limit
        while True:
            value = self.next_u64()
            if value < cutoff:
                return value % limit

    def choice(self, values: Sequence[_T]) -> _T:
        if not values:
            raise ValueError("cannot choose from an empty sequence")
        return values[self.randbelow(len(values))]

    def shuffle(self, values: list[_T]) -> None:
        for index in range(len(values) - 1, 0, -1):
            other = self.randbelow(index + 1)
            values[index], values[other] = values[other], values[index]

    def rotated(self, values: Sequence[_T]) -> list[_T]:
        if not values:
            return []
        offset = self.randbelow(len(values))
        return list(values[offset:]) + list(values[:offset])


def derive_seed(
    seed: int,
    spec_digest: str,
    constraint_index: int,
    constraint_id: str,
    attempt: int,
    purpose: str,
) -> int:
    material = (
        f"{seed}\0{spec_digest}\0{constraint_index}\0{constraint_id}\0"
        f"{attempt}\0{purpose}"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
