"""Counting Bloom filter kernel used by the negative-query gateway.

The backing store is a per-bit counter array (``bytearray``).  Each insertion
increments every mapped counter, each deletion decrements it, and a position
counts as "set" while its counter is non-zero.  Keeping the counters (instead
of a plain bit array) lets the engine explain residual risk with the concrete
counter values behind every reported candidate key.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import bit_positions
from .schemas import ProjectSpec


@dataclass
class Generation:
    """One Bloom generation and its lifecycle bookkeeping."""

    generation_id: int
    spec: ProjectSpec
    counters: bytearray
    inserted_count: int
    activated_at: int
    deactivated_at: int | None = None
    migrated_keys: int = 0

    @property
    def bit_array_size(self) -> int:
        return self.spec.bit_array_size

    def set_bit_count(self) -> int:
        """Number of array positions whose counter is non-zero."""

        return sum(1 for counter in self.counters if counter > 0)

    def occupancy_ratio(self) -> float:
        """Occupancy = set-bit count / bit-array size."""

        return self.set_bit_count() / self.bit_array_size


class CountingBloom:
    """Mutable counting Bloom filter bound to one project spec."""

    def __init__(self, spec: ProjectSpec) -> None:
        self.spec = spec

    def new_generation(self, generation_id: int, activated_at: int) -> Generation:
        return Generation(
            generation_id=generation_id,
            spec=self.spec,
            counters=bytearray(self.spec.bit_array_size),
            inserted_count=0,
            activated_at=activated_at,
        )

    def positions(self, key: bytes) -> tuple[int, ...]:
        return bit_positions(self.spec.seeds, key, self.spec.bit_array_size)

    def add(self, generation: Generation, key: bytes) -> tuple[int, ...]:
        """Set every mapped position (increment counters); return positions."""

        positions = self.positions(key)
        for index in positions:
            generation.counters[index] = min(255, generation.counters[index] + 1)
        generation.inserted_count += 1
        return positions

    def set_positions(self, generation: Generation, key: bytes) -> tuple[int, ...]:
        """Rebuild helper: set bits for a migrated key without counting an insert."""

        positions = self.positions(key)
        for index in positions:
            generation.counters[index] = min(255, generation.counters[index] + 1)
        return positions

    def remove(self, generation: Generation, key: bytes) -> tuple[int, ...]:
        """Release every mapped position (decrement counters); return positions."""

        positions = self.positions(key)
        for index in positions:
            if generation.counters[index] > 0:
                generation.counters[index] -= 1
        return positions

    def contains(self, generation: Generation, key: bytes) -> bool:
        """Filter verdict: present only if every mapped counter is non-zero."""

        return all(generation.counters[index] > 0 for index in self.positions(key))

    def counter_snapshot(self, generation: Generation, key: bytes) -> dict[int, int]:
        """Residual evidence: counter value at each position mapped by a key."""

        return {
            index: generation.counters[index]
            for index in self.positions(key)
        }
