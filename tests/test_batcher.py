import random

import pytest

from coord_workbench.batcher import (
    BatchFormer,
    merge_batches,
    merge_events_one_shot,
)
from coord_workbench.engine import form_batches
from coord_workbench.errors import BizError, Err
from coord_workbench.models import Event


def test_batched_equals_one_shot(samples) -> None:
    _, batches = form_batches(samples)
    assert merge_batches(batches, samples.domain_of) == merge_events_one_shot(
        samples.events, samples.domain_of
    )


def test_out_of_order_arrival_buffered_and_consistent(samples) -> None:
    arrival = list(samples.events)
    # deterministic shuffle that keeps the stream valid as an arrival order
    random.Random(7).shuffle(arrival)
    former, batches = form_batches(samples, arrival=arrival)
    assert former.buffer_status()["pending_seqs"] == []  # all drained
    assert merge_batches(batches, samples.domain_of) == merge_events_one_shot(
        samples.events, samples.domain_of
    )


def test_pending_buffer_stable_order(samples) -> None:
    former = BatchFormer(samples.domain_of)
    # deliver seq 3 and 2 before seq 1: both wait in the buffer
    by_seq = {e.seq: e for e in samples.events}
    former.arrive(by_seq[3])
    former.arrive(by_seq[2])
    assert former.buffer_status()["pending_seqs"] == [2, 3]
    former.arrive(by_seq[1])  # triggers drain of 2, 3
    assert former.buffer_status()["pending_seqs"] == []
    assert former.buffer_status()["expected_seq"] == 4


def test_same_node_term_once_per_batch(samples) -> None:
    _, batches = form_batches(samples)
    for batch in batches:
        seen = set()
        for ev in batch.events:
            key = (ev.node, ev.candidate_term)
            assert key not in seen, f"duplicate {key} in {batch.batch_id}"
            seen.add(key)


def test_clock_regress_beyond_tolerance() -> None:
    former = BatchFormer(lambda n: "d", clock_tolerance_ms=100)
    former.arrive(Event(seq=1, node="n", clock_ms=10_000, candidate_term=1, vote={}, revoked=False))
    with pytest.raises(BizError) as ei:
        former.arrive(Event(seq=2, node="n", clock_ms=9_000, candidate_term=1, vote={}, revoked=False))
    assert ei.value.code == Err.CLOCK_REGRESS
    assert ei.value.fields["watermark"] == 10_000


def test_clock_regress_within_tolerance_ok() -> None:
    former = BatchFormer(lambda n: "d", clock_tolerance_ms=250)
    former.arrive(Event(seq=1, node="n", clock_ms=10_000, candidate_term=1, vote={}, revoked=False))
    former.arrive(Event(seq=2, node="n", clock_ms=9_900, candidate_term=1, vote={}, revoked=False))
    assert former.clock_watermark == 10_000


def test_duplicate_delivery_dropped(samples) -> None:
    former = BatchFormer(samples.domain_of)
    first = samples.events[0]
    former.arrive(first)
    former.arrive(first)  # redelivery
    assert former.buffer_status()["duplicates_dropped"] == 1
