"""Independent reference bit-array implementation.

This module deliberately does **not** import :mod:`nqg.hashing` or
:mod:`nqg.bloom`.  It re-derives the documented position contract on its own
with a separate code path (its own hashlib call, its own modulo, its own
set-based bit array) so that an engine/filter bug cannot silently hide behind
a shared helper.

Storage model:

* ``set_bits``      - Python ``set`` of integer positions currently occupied;
* ``ref_counters``  - dict of per-position counters mirroring add/remove, so
                       the reference supports the same deletes as the kernel.
"""

from __future__ import annotations

import hashlib

from .schemas import ProjectSpec


class ReferenceCountingBitArray:
    """Stand-alone reimplementation of the counting Bloom semantics."""

    def __init__(self, spec: ProjectSpec) -> None:
        self.bit_array_size = spec.bit_array_size
        self.seed_tuple = tuple(spec.seeds)
        self.ref_counters: dict[int, int] = {}
        self.set_bits: set[int] = set()

    def _derive_index(self, seed: int, key: bytes) -> int:
        """Independent derivation: do not call the kernel's hashing module."""

        prefix = seed.to_bytes(4, byteorder="big")
        digest = hashlib.blake2b(prefix + key).digest()
        integer_value = int.from_bytes(digest, byteorder="big")
        return integer_value % self.bit_array_size

    def positions_for(self, key: bytes) -> list[int]:
        """All positions mapped by ``key``, independently computed."""

        return [self._derive_index(seed, key) for seed in self.seed_tuple]

    def add(self, key: bytes) -> list[int]:
        positions = self.positions_for(key)
        for index in positions:
            value = self.ref_counters.get(index, 0) + 1
            self.ref_counters[index] = value
            self.set_bits.add(index)
        return positions

    def remove(self, key: bytes) -> list[int]:
        positions = self.positions_for(key)
        for index in positions:
            value = self.ref_counters.get(index, 0)
            if value > 0:
                value -= 1
                self.ref_counters[index] = value
                if value == 0:
                    self.set_bits.discard(index)
        return positions

    def membership(self, key: bytes) -> bool:
        return all(index in self.set_bits for index in self.positions_for(key))

    def occupancy_count(self) -> int:
        return len(self.set_bits)

    def occupancy_ratio(self) -> float:
        return len(self.set_bits) / self.bit_array_size

    def position_states(self, key: bytes) -> dict[int, bool]:
        return {index: index in self.set_bits for index in self.positions_for(key)}


class ReferenceMismatch(RuntimeError):
    """Raised at the first query where kernel and reference disagree."""


class ReferenceAuditor:
    """Replays every engine operation on the independent bit array and audits.

    Because a rotated generation is rebuilt from the surviving key set, the
    auditor keeps one reference array per generation and rebuilds it the same
    way (bulk-adding the sorted surviving keys) rather than trusting the kernel.
    """

    def __init__(self, spec: ProjectSpec) -> None:
        self.spec = spec
        self.generation_arrays: dict[int, ReferenceCountingBitArray] = {}
        self.current_id: int | None = None

    def open_generation(self, generation_id: int) -> ReferenceCountingBitArray:
        arr = ReferenceCountingBitArray(self.spec)
        self.generation_arrays[generation_id] = arr
        self.current_id = generation_id
        return arr

    def rebuild_generation(
        self, generation_id: int, surviving_keys: list[bytes]
    ) -> ReferenceCountingBitArray:
        arr = self.open_generation(generation_id)
        for key in sorted(surviving_keys):
            arr.add(key)
        return arr

    def add(self, key: bytes) -> None:
        self.generation_arrays[self.current_id].add(key)

    def remove(self, key: bytes) -> None:
        self.generation_arrays[self.current_id].remove(key)

    def audit_query(
        self,
        query_seq: int,
        logical_time: int,
        generation_id: int,
        key: bytes,
        kernel_verdict: bool,
        kernel_set_count: int,
    ) -> None:
        """Raise :class:`ReferenceMismatch` at the first disagreement."""

        arr = self.generation_arrays[generation_id]
        ref_verdict = arr.membership(key)
        if ref_verdict != kernel_verdict:
            positions = arr.positions_for(key)
            states = arr.position_states(key)
            raise ReferenceMismatch(
                "first inconsistent query "
                f"seq={query_seq} tick={logical_time} key={key!r} "
                f"generation={generation_id} positions={positions} "
                f"kernel_verdict={kernel_verdict} reference_verdict={ref_verdict} "
                f"reference_position_states={states}"
            )
        if arr.occupancy_count() != kernel_set_count:
            positions = arr.positions_for(key)
            states = arr.position_states(key)
            raise ReferenceMismatch(
                "first occupancy mismatch "
                f"seq={query_seq} tick={logical_time} key={key!r} "
                f"generation={generation_id} positions={positions} "
                f"kernel_set_bits={kernel_set_count} "
                f"reference_set_bits={arr.occupancy_count()} "
                f"reference_position_states={states}"
            )
