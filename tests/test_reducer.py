"""Unit tests for the RingReducer phase state machine (no sockets)."""

import numpy as np
import pytest

from ddp_sim.errors import E_PHASE, SimError
from ddp_sim.model import local_gradient
from ddp_sim.worker import RingReducer


def simulate_ring(workers=4, dim=64, block=16, seed=5):
    """Drive all reducers in lockstep until done; returns reducers."""
    sizes = [block] * (dim // block)
    outboxes = {r: [] for r in range(workers)}
    reducers = []
    for r in range(workers):
        grad = local_gradient(seed, r, 0, dim)
        reducers.append(RingReducer(
            r, workers, sizes, grad,
            on_send=lambda h, p, r=r: outboxes[r].append((h, p))))
    for r in range(workers):
        for c in reducers[r].owned_chunks():
            reducers[r].start_chunk(c)
    # deliver messages around the ring until everything is done
    rounds = 0
    while not all(red.done for red in reducers):
        rounds += 1
        assert rounds < 100
        for r in range(workers):
            outbox = outboxes[r]
            outboxes[r] = []
            for header, payload in outbox:
                reducers[(r + 1) % workers].handle(header, payload)
    return reducers


def test_ring_allreduce_matches_reference_sum():
    workers, dim, seed = 4, 64, 5
    reducers = simulate_ring(workers, dim, 16, seed)
    grads = [local_gradient(seed, r, 0, dim) for r in range(workers)]
    reference = np.zeros(dim)
    n_chunks = dim // 16
    for c in range(n_chunks):
        start = c % workers
        acc = np.zeros(16)
        for i in range(workers):
            rank = (start + i) % workers
            acc = acc + grads[rank][c * 16:(c + 1) * 16]
        reference[c * 16:(c + 1) * 16] = acc
    for red in reducers:
        assert red.global_gradient().tobytes() == reference.tobytes()


def test_accumulation_order_is_ring_order():
    reducers = simulate_ring(4, 64, 16, 5)
    n_chunks = 4
    for c in range(n_chunks):
        start = c % 4
        expected = [(start + i) % 4 for i in range(4)]
        for red in reducers:
            if red.orders[c] is not None:
                assert red.orders[c] == expected


def test_gather_before_reduce_raises_e_phase():
    grad = local_gradient(5, 0, 0, 64)
    red = RingReducer(0, 4, [16] * 4, grad, on_send=lambda h, p: None)
    with pytest.raises(SimError) as err:
        red.handle({"type": "chunk", "phase": "gather", "chunk": 2,
                    "origin": 3}, b"\x00" * 16 * 8)
    assert err.value.code == E_PHASE


def test_duplicate_reduce_raises_e_phase():
    grad = local_gradient(5, 0, 0, 64)
    sent = []
    red = RingReducer(0, 4, [16] * 4, grad, on_send=lambda h, p: sent.append((h, p)))
    red.start_chunk(0)
    with pytest.raises(SimError) as err:
        red.handle({"type": "chunk", "phase": "reduce", "chunk": 0,
                    "contrib": 1, "order": [3]}, grad[:16].tobytes())
    assert err.value.code == E_PHASE


def test_inject_from_wrong_rank_raises_e_phase():
    grad = local_gradient(5, 0, 0, 64)
    red = RingReducer(0, 4, [16] * 4, grad, on_send=lambda h, p: None)
    with pytest.raises(SimError) as err:
        red.start_chunk(1)  # chunk 1 belongs to rank 1
    assert err.value.code == E_PHASE
