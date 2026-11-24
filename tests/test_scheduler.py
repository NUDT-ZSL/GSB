from __future__ import annotations

import pytest

from formbench.scheduler import (
    ManualClock,
    MemoryTracker,
    StopController,
    WorkItem,
    execute_plan,
    plan_rounds,
)


def _items(count=8, size=10, cost=3.0):
    return [WorkItem(index=index, buffer_bytes=size, logic_cost=cost) for index in range(count)]


def test_plan_is_reproducible_for_each_concurrency():
    items = _items()
    plans = {}
    for concurrency in (1, 2, 3, 4, 8):
        first = plan_rounds(
            items,
            concurrency=concurrency,
            memory_budget_bytes=25,
            round_logic_budget=10,
        )
        second = plan_rounds(
            items,
            concurrency=concurrency,
            memory_budget_bytes=25,
            round_logic_budget=10,
        )
        assert first.admission_order == second.admission_order
        assert [r.indices for r in first.rounds] == [r.indices for r in second.rounds]
        plans[concurrency] = first

    assert plans[1].round_count == 8
    assert all(len(round_plan.indices) <= 2 for round_plan in plans[2].rounds)
    for concurrency, plan in plans.items():
        for round_plan in plan.rounds:
            assert len(round_plan.indices) <= concurrency
            assert round_plan.projected_bytes <= 25 or round_plan.indices
            assert round_plan.projected_logic <= 10 or round_plan.close_reason == (
                "block_exceeded_logic_budget"
            )


def test_execute_releases_buffers_after_block_error():
    items = _items(count=5, size=10, cost=1)
    plan = plan_rounds(
        items, concurrency=2, memory_budget_bytes=100, round_logic_budget=100
    )
    tracker = MemoryTracker(100)
    started = []

    def worker(item):
        started.append(item.index)
        if item.index == 1:
            raise RuntimeError("injected block failure")
        return item.index * 2

    with pytest.raises(RuntimeError, match="injected block failure"):
        execute_plan(
            plan,
            {item.index: item for item in items},
            concurrency=2,
            tracker=tracker,
            clock=ManualClock(),
            stop=StopController(),
            worker=worker,
        )

    assert tracker.used_bytes == 0
    assert tracker.peak_bytes <= 100


def test_stop_controller_prevents_next_round():
    items = _items(count=4, size=1, cost=1)
    plan = plan_rounds(
        items, concurrency=1, memory_budget_bytes=10, round_logic_budget=10
    )
    stop = StopController()

    def worker(item):
        if item.index == 0:
            stop.request_stop("test")
        return True

    from formbench.errors import InterruptedBySignal

    with pytest.raises(InterruptedBySignal):
        execute_plan(
            plan,
            {item.index: item for item in items},
            concurrency=1,
            tracker=MemoryTracker(10),
            clock=ManualClock(),
            stop=stop,
            worker=worker,
        )


def test_oversized_buffer_is_a_business_budget_error():
    from formbench.errors import MemoryBudgetError

    with pytest.raises(MemoryBudgetError):
        plan_rounds(
            [WorkItem(0, 100, 1)],
            concurrency=1,
            memory_budget_bytes=10,
            round_logic_budget=10,
        )
