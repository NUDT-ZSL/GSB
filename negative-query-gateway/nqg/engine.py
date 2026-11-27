"""Negative-query acceleration engine.

Pipeline for each read query (logical-time ordered):

1. the counting Bloom filter returns a verdict;
2. a current-generation tombstone short-circuits any deleted key to
   "definitely absent" even if residual bits remain set;
3. an absent verdict is intercepted at the gateway (no backend load);
4. a present verdict is released to the backend shard index, which is the
   authority on existence; a released-but-absent key is a measured false
   positive.

Generations rotate when the current generation reaches its declared insertion
limit: the successor is rebuilt from the authoritative surviving key set and
swapped in atomically, after which the old generation never judges again.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bloom import CountingBloom, Generation
from .reference_bloom import ReferenceAuditor
from .schemas import Event, ProjectSpec


@dataclass
class RotationRecord:
    triggered_at: int
    old_generation_id: int
    new_generation_id: int
    migrated_keys: int
    old_generation_deactivated_at: int


@dataclass
class QueryRecord:
    query_seq: int
    logical_time: int
    generation_id: int
    key: bytes
    filter_verdict: str  # "absent" | "present"
    decision: str  # "intercepted" | "released"
    backend_exists: bool | None
    false_positive: bool
    via_tombstone: bool
    positions: tuple[int, ...]


@dataclass
class DeleteRecord:
    logical_time: int
    generation_id: int
    key: bytes
    positions: tuple[int, ...]
    counters_after: dict[int, int]


@dataclass
class EngineResult:
    spec: ProjectSpec
    generations: list[Generation]
    rotations: list[RotationRecord]
    queries: list[QueryRecord]
    deletes: list[DeleteRecord]
    backend_final_size: int


class BackendShardIndex:
    """Authoritative in-memory shard index (stand-in for real storage)."""

    def __init__(self) -> None:
        self._objects: set[bytes] = set()
        self.lookups = 0

    def insert(self, key: bytes) -> None:
        self._objects.add(key)

    def delete(self, key: bytes) -> None:
        self._objects.discard(key)

    def exists(self, key: bytes) -> bool:
        self.lookups += 1
        return key in self._objects

    def snapshot(self) -> set[bytes]:
        return set(self._objects)


class GatewayEngine:
    """Runs the full event stream and records every auditable decision."""

    def __init__(self, spec: ProjectSpec, auditor: ReferenceAuditor | None = None) -> None:
        self.spec = spec
        self.bloom = CountingBloom(spec)
        self.backend = BackendShardIndex()
        self.auditor = auditor

        first_generation = self.bloom.new_generation(generation_id=1, activated_at=0)
        self.current: Generation = first_generation
        self.generations: list[Generation] = [first_generation]
        if self.auditor is not None:
            self.auditor.open_generation(1)

        # Persistent generational markers: key -> generation in which it was
        # deleted.  They survive rotation so a deleted key can never read as
        # present again unless it is explicitly re-inserted.
        self.tombstones: dict[bytes, int] = {}
        self.rotations: list[RotationRecord] = []
        self.queries: list[QueryRecord] = []
        self.delete_records: list[DeleteRecord] = []
        self._query_seq = 0

    def _rotate(self, logical_time: int) -> None:
        """Rebuild a successor from surviving keys and atomically switch."""

        old = self.current
        surviving = sorted(self.backend.snapshot())
        new_id = old.generation_id + 1
        successor = self.bloom.new_generation(new_id, activated_at=logical_time)
        for key in surviving:
            self.bloom.set_positions(successor, key)
        successor.migrated_keys = len(surviving)

        # Atomic switch: single pointer assignment flips the active generation.
        old.deactivated_at = logical_time
        self.current = successor
        self.generations.append(successor)
        self.rotations.append(
            RotationRecord(
                triggered_at=logical_time,
                old_generation_id=old.generation_id,
                new_generation_id=new_id,
                migrated_keys=len(surviving),
                old_generation_deactivated_at=logical_time,
            )
        )
        if self.auditor is not None:
            self.auditor.rebuild_generation(new_id, surviving)

    def _handle_insert(self, event: Event) -> None:
        if self.current.inserted_count >= self.spec.max_inserts_per_generation:
            self._rotate(event.logical_time)
        self.bloom.add(self.current, event.key)
        self.backend.insert(event.key)
        self.tombstones.pop(event.key, None)
        if self.auditor is not None:
            self.auditor.add(event.key)

    def _handle_delete(self, event: Event) -> None:
        positions = self.bloom.positions(event.key)
        self.bloom.remove(self.current, event.key)
        self.backend.delete(event.key)
        # Generational marker: guarantees this key can never read as present.
        self.tombstones[event.key] = self.current.generation_id
        counters_after = self.bloom.counter_snapshot(self.current, event.key)
        self.delete_records.append(
            DeleteRecord(
                logical_time=event.logical_time,
                generation_id=self.current.generation_id,
                key=event.key,
                positions=positions,
                counters_after=counters_after,
            )
        )
        if self.auditor is not None:
            self.auditor.remove(event.key)

    def _handle_query(self, event: Event) -> QueryRecord:
        self._query_seq += 1
        positions = self.bloom.positions(event.key)
        raw_filter_present = self.bloom.contains(self.current, event.key)
        via_tombstone = event.key in self.tombstones
        filter_present = raw_filter_present and not via_tombstone
        filter_verdict = "present" if filter_present else "absent"

        if self.auditor is not None:
            self.auditor.audit_query(
                query_seq=self._query_seq,
                logical_time=event.logical_time,
                generation_id=self.current.generation_id,
                key=event.key,
                # The reference audits the raw bit-array verdict; the tombstone
                # is an engine-layer marker outside the bit array.
                kernel_verdict=raw_filter_present,
                kernel_set_count=self.current.set_bit_count(),
            )

        if not filter_present:
            record = QueryRecord(
                query_seq=self._query_seq,
                logical_time=event.logical_time,
                generation_id=self.current.generation_id,
                key=event.key,
                filter_verdict=filter_verdict,
                decision="intercepted",
                backend_exists=None,
                false_positive=False,
                via_tombstone=via_tombstone,
                positions=positions,
            )
        else:
            exists = self.backend.exists(event.key)
            record = QueryRecord(
                query_seq=self._query_seq,
                logical_time=event.logical_time,
                generation_id=self.current.generation_id,
                key=event.key,
                filter_verdict=filter_verdict,
                decision="released",
                backend_exists=exists,
                false_positive=not exists,
                via_tombstone=False,
                positions=positions,
            )
        self.queries.append(record)
        return record

    def run(self, events: list[Event]) -> EngineResult:
        for event in events:
            if event.op == "insert":
                self._handle_insert(event)
            elif event.op == "delete":
                self._handle_delete(event)
            else:
                self._handle_query(event)
        return EngineResult(
            spec=self.spec,
            generations=self.generations,
            rotations=self.rotations,
            queries=self.queries,
            deletes=self.delete_records,
            backend_final_size=len(self.backend.snapshot()),
        )
