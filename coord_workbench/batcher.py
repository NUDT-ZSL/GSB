"""Logical-clock-driven election event batching & deterministic merge (req 3).

* Out-of-order arrivals land in a pending buffer and are drained in a
  stable (seq, clock_ms) order.
* Committed events are grouped into topology batches keyed by
  (domain, candidate_term); a node appears at most once per batch for
  the same candidate term -- a repeat closes the current batch and
  opens the next one.
* A logical-clock regression beyond the tolerance raises a business
  error.
* ``merge_events_one_shot`` is the reference replay; the batched merge
  must produce an item-for-item identical topology state.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from .errors import BizError, Err
from .models import Batch, Event

CLOCK_TOLERANCE_MS = 250
PER_EVENT_COST_MS = 10


class BatchFormer:
    """Turns an event arrival stream into committed topology batches."""

    def __init__(self, domain_of: Callable[[str], str], clock_tolerance_ms: int = CLOCK_TOLERANCE_MS) -> None:
        self.domain_of = domain_of
        self.tolerance = clock_tolerance_ms
        self.expected_seq = 1
        self.clock_watermark = 0
        self.pending: List[Event] = []  # out-of-order buffer
        self.duplicates = 0
        self._open: Dict[Any, Batch] = {}
        self._batch_counter = 0
        self.closed: List[Batch] = []

    def arrive(self, event: Event) -> None:
        """Feed one event in arrival order."""
        if event.seq < self.expected_seq:
            self.duplicates += 1
            return
        if event.seq > self.expected_seq:
            self.pending.append(event)
            self.pending.sort(key=lambda e: (e.seq, e.clock_ms))  # stable order
            return
        self._commit(event)

    def _commit(self, event: Event) -> None:
        if event.clock_ms < self.clock_watermark - self.tolerance:
            raise BizError(
                Err.CLOCK_REGRESS,
                "logical clock regression beyond tolerance",
                seq=event.seq,
                clock_ms=event.clock_ms,
                watermark=self.clock_watermark,
                tolerance=self.tolerance,
            )
        self.clock_watermark = max(self.clock_watermark, event.clock_ms)
        self.expected_seq = event.seq + 1
        self._append_to_batch(event)
        # drain pending buffer while the head is the next expected seq
        while self.pending and self.pending[0].seq == self.expected_seq:
            nxt = self.pending.pop(0)
            self._commit(nxt)

    def _append_to_batch(self, event: Event) -> None:
        domain = self.domain_of(event.node)
        key = (domain, event.candidate_term)
        batch = self._open.get(key)
        if batch is not None and any(e.node == event.node for e in batch.events):
            self._close(key)
            batch = None
        if batch is None:
            self._batch_counter += 1
            batch = Batch(
                batch_id=f"batch-{self._batch_counter:04d}",
                domain=domain,
                topo_epoch=event.candidate_term,
            )
            self._open[key] = batch
        batch.events.append(event)
        batch.est_cost_ms += PER_EVENT_COST_MS

    def _close(self, key: Any) -> None:
        batch = self._open.pop(key)
        self.closed.append(batch)

    def flush(self) -> List[Batch]:
        all_batches = self.closed + list(self._open.values())
        self._open.clear()
        self.closed = sorted(all_batches, key=lambda b: b.batch_id)
        return list(self.closed)

    def buffer_status(self) -> Dict[str, Any]:
        return {
            "expected_seq": self.expected_seq,
            "clock_watermark": self.clock_watermark,
            "pending_seqs": [e.seq for e in self.pending],
            "duplicates_dropped": self.duplicates,
        }


def empty_topology_state() -> Dict[str, Any]:
    return {}


def apply_event(state: Dict[str, Any], event: Event, domain_of: Callable[[str], str]) -> None:
    """Apply one committed event to the topology state (deterministic)."""
    domain = domain_of(event.node)
    dom = state.setdefault(domain, {})
    term = dom.setdefault(str(event.candidate_term), {"votes": {}, "revoked": [], "last_clock": 0})
    if event.revoked:
        if event.node not in term["revoked"]:
            term["revoked"].append(event.node)
            term["revoked"].sort()
        term["votes"].pop(event.node, None)
    else:
        term["votes"][event.node] = {"for": event.vote.get("for"), "weight": event.vote.get("weight", 0)}
    term["last_clock"] = max(term["last_clock"], event.clock_ms)


def merge_events_one_shot(events: List[Event], domain_of: Callable[[str], str]) -> Dict[str, Any]:
    """Reference one-shot replay of the full event stream."""
    state = empty_topology_state()
    for event in sorted(events, key=lambda e: e.seq):
        apply_event(state, event, domain_of)
    return state


def merge_batches(batches: List[Batch], domain_of: Callable[[str], str]) -> Dict[str, Any]:
    """Batched merge; must equal :func:`merge_events_one_shot`."""
    state = empty_topology_state()
    for batch in batches:
        for event in sorted(batch.events, key=lambda e: e.seq):
            apply_event(state, event, domain_of)
    return state
