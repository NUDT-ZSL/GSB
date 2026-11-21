from __future__ import annotations

import pytest

from capbench.scenario import GradientSpec
from capbench.workload import (
    build_window_requests,
    count_window_requests,
    p95_latency,
)


def test_request_schedule_is_byte_deterministic(demo_scenario) -> None:
    gradient = demo_scenario.plan_map["alpha"].gradients[-1]
    first = build_window_requests(gradient, 0, demo_scenario.clock, 100)
    second = build_window_requests(gradient, 0, demo_scenario.clock, 100)
    assert first == second
    assert len(first) == count_window_requests(gradient, 0) == 200
    assert first[0].sequence == 100
    assert first[-1].sequence == 299
    assert all(
        0 <= item.started_ms - gradient.logical_start_ms < 1000
        for item in first
    )
    assert all(
        (item.planned_ms - gradient.logical_start_ms) // 1000 == 0
        for item in first
    )


def test_all_three_window_layers_are_disjoint(demo_scenario) -> None:
    gradient = demo_scenario.plan_map["alpha"].gradients[0]
    windows = [
        build_window_requests(
            gradient,
            index,
            demo_scenario.clock,
            1 + index * 50,
        )
        for index in range(gradient.window_count)
    ]
    for index, requests in enumerate(windows):
        assert len(requests) == 50
        assert all(
            index * 1000 <= request.planned_ms < (index + 1) * 1000
            for request in requests
        )
    all_sequences = [request.sequence for requests in windows for request in requests]
    assert len(all_sequences) == len(set(all_sequences))


def test_p95_linear_interpolation_rule() -> None:
    assert p95_latency([4, 1, 3, 2]) == pytest.approx(3.85)
    assert p95_latency([7]) == 7
    assert p95_latency([]) is None
    assert p95_latency([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]) == pytest.approx(19.05)


def test_clock_steps_are_injected() -> None:
    from capbench.clock import ClockSegment, ClockSpec

    gradient = GradientSpec(
        index=1,
        concurrency=2,
        duration_ms=100,
        pace_ms=10,
        window_ms=100,
        request_timeout_ms=100,
        logical_start_ms=0,
    )
    clock = ClockSpec((ClockSegment(0, 100, 7),))
    requests = build_window_requests(gradient, 0, clock, 1)
    assert requests
    ticks = {0, 7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98}
    assert {request.started_ms for request in requests} <= ticks

