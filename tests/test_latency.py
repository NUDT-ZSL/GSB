import pytest

from hotcfg.errors import BizError, E_SAMPLE
from hotcfg.latency import (
    CLOCK_BASIS,
    MIN_SAMPLES,
    QUANT_ERROR_BOUND_MS,
    SCALE_MS,
    TRUTH_SOURCE,
    LatencyObserver,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def observer_with_durations(durations_ms):
    clock = FakeClock()
    obs = LatencyObserver(clock=clock)
    for d in durations_ms:
        start = clock.t * 1000.0
        clock.t += d / 1000.0
        obs.record("t1", 2, "query", start, clock.t * 1000.0)
    return obs


def test_fixed_point_stats():
    obs = observer_with_durations([1.0, 2.0, 3.0, 4.0, 100.0])
    stats = obs.stats(task_id="t1", version=2, kind="query")
    s = stats[("t1", 2, "query")]
    assert s["count"] == 5
    assert s["mean"] == int(round(22.0 * SCALE_MS))
    assert s["p95"] == 100 * SCALE_MS
    assert s["max"] == 100 * SCALE_MS
    assert s["scale"] == SCALE_MS
    assert s["quant_error_bound_ms"] == QUANT_ERROR_BOUND_MS == 0.5 / SCALE_MS
    assert s["clock_basis"] == CLOCK_BASIS
    assert s["truth_source"] == TRUTH_SOURCE


def test_insufficient_samples_raise_e_sample():
    obs = observer_with_durations([1.0, 2.0])
    with pytest.raises(BizError) as ei:
        obs.stats()
    assert ei.value.code == E_SAMPLE
    assert ei.value.details["min_samples"] == MIN_SAMPLES


def test_no_samples_raise_e_sample():
    obs = LatencyObserver()
    with pytest.raises(BizError) as ei:
        obs.stats(task_id="ghost")
    assert ei.value.code == E_SAMPLE
