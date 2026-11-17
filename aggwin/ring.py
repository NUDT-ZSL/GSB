"""Fixed-capacity circular ring buffer for sliding window frame state.

The ring stores admitted rows for one window partition.  It enforces two
constraints:

* timestamp interval ``[current_ts + lower, current_ts + upper]``
  (both bounds inclusive; the interval closure rule is recorded explicitly);
* a hard maximum row count.

Entries are kept in arrival order; out-of-order arrivals are admitted when
they fall inside the watermark lag allowance and trigger an explicit local
recomputation of the running sum.  Eviction by timestamp (sliding out) never
alters the contribution of rows that remain in the frame.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class RingEntry:
    row_id: int
    ts: int
    value: int


class RingBuffer:
    """Deterministic circular buffer with O(1) amortized sliding."""

    def __init__(self, max_rows: int):
        self.max_rows = max_rows
        self._storage: list[RingEntry | None] = [None] * max_rows
        self._head = 0          # index of the oldest entry
        self._size = 0
        self.value_sum = 0

    def __len__(self) -> int:
        return self._size

    def entries(self) -> list[RingEntry]:
        """Entries in arrival order (oldest -> newest)."""
        result = []
        for offset in range(self._size):
            result.append(self._storage[(self._head + offset) % self.max_rows])
        return result

    def _enqueue(self, entry: RingEntry) -> None:
        if self._size == self.max_rows:
            self._evict_oldest()
        slot = (self._head + self._size) % self.max_rows
        self._storage[slot] = entry
        self._size += 1
        self.value_sum += entry.value

    def _evict_oldest(self) -> RingEntry:
        oldest = self._storage[self._head]
        self._storage[self._head] = None
        self._head = (self._head + 1) % self.max_rows
        self._size -= 1
        self.value_sum -= oldest.value
        return oldest

    def expire_before(self, lower_ts: int) -> list[RingEntry]:
        """Slide out entries whose timestamp is strictly below lower_ts."""
        removed: list[RingEntry] = []
        while self._size and self._storage[self._head].ts < lower_ts:
            removed.append(self._evict_oldest())
        return removed

    def admit(self, entry: RingEntry, watermark: int, allowed_lag: int) -> bool:
        """Admission rule for an arriving (possibly late) row.

        A row is admitted iff ``entry.ts >= watermark - allowed_lag``.  Rows
        older than the lag allowance are permanently rejected for this window.
        """
        return entry.ts >= watermark - allowed_lag

    def push(self, entry: RingEntry) -> None:
        self._enqueue(entry)

    def recompute_sum(self) -> int:
        """Local recomputation after an out-of-order insertion."""
        total = 0
        for stored in self.entries():
            total += stored.value
        self.value_sum = total
        return total

    def rank_of(self, row_id: int) -> int:
        """1-based rank of ``row_id`` when the frame is sorted by (ts, row_id)."""
        ordered = sorted(self.entries(), key=lambda item: (item.ts, item.row_id))
        for position, entry in enumerate(ordered, start=1):
            if entry.row_id == row_id:
                return position
        raise KeyError(row_id)

    def to_state(self) -> dict:
        return {
            "max_rows": self.max_rows,
            "head": 0,
            "size": self._size,
            "value_sum": self.value_sum,
            "entries": [asdict(entry) for entry in self.entries()],
            "interval_closure": "inclusive_both_bounds",
        }

    @classmethod
    def from_state(cls, state: dict) -> "RingBuffer":
        ring = cls(int(state["max_rows"]))
        for raw_entry in state["entries"]:
            ring.push(RingEntry(**raw_entry))
        ring.recompute_sum()
        ring._head = 0
        if ring.value_sum != int(state["value_sum"]):
            raise ValueError("ring buffer sum does not match its entries")
        return ring
