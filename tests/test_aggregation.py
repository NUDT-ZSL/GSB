from __future__ import annotations
from decimal import Decimal

import pytest

from slo_workbench import aggregation as agg
from slo_workbench.decimal_math import quantile_type7
from slo_workbench.errors import BusinessError
from slo_workbench.models import Event, parse_slo


def make_slo(**overrides):
    payload = {
        "name": "a",
        "service": "svc",
        "sli_type": "availability",
        "target": 90,
        "window_length_ms": 2000,
        "window_step_ms": 1000,
        "compliance_period_ms": 5000,
        "burn_alert_threshold": 2,
    }
    payload.update(overrides)
    return parse_slo(payload)


def event(seq, clock, success=True, valid=True, latency=10):
    return Event(
        seq=seq,
        service="svc",
        clock_ms=clock,
        success=success,
        latency_ms=Decimal(latency),
        valid=valid,
    )


def test_home_window_unique_and_deterministic():
    slo = make_slo()
    ownership = [agg.home_window_no(clock, slo.window_step_ms) for clock in range(0, 3000)]
    assert ownership[0] == 0
    assert ownership[999] == 0
    assert ownership[1000] == 1
    # Every clock maps to exactly one window.
    assert len(ownership) == len(set(range(0, 3000)))


def test_window_budget_and_burn_rate():
    slo = make_slo()
    events = [event(1, 10), event(2, 20, success=False), event(3, 30, success=False)]
    report = agg.compute_window(slo, 0, events)
    assert report["valid_requests"] == 3
    assert report["success_count"] == 1
    assert report["failure_count"] == 2
    # target 90% -> budget ratio 0.1; error ratio 2/3 -> burn 6.666...
    assert report["budget"]["total"]["value"] == "0.3"
    assert report["burn_rate"]["value"].startswith("6.666666666666666666666666667")
    assert report["burn_exceeded"] is True
    # Error bounds are recorded for every tracked operation.
    assert Decimal(report["burn_rate"]["error_bound"]) > 0


def test_repeated_computation_is_bit_identical():
    slo = make_slo()
    events = [event(i, i * 5, success=(i % 4 != 0)) for i in range(1, 40)]
    first = agg.compute_window(slo, 2, events)
    second = agg.compute_window(slo, 2, events)
    from slo_workbench.repository import payload_digest

    assert payload_digest(first) == payload_digest(second)


def test_empty_valid_requests_raises_defined_code():
    slo = make_slo()
    with pytest.raises(BusinessError) as exc:
        agg.compute_window(slo, 0, [event(1, 10, valid=False)])
    assert exc.value.code == "E_EMPTY_WINDOW"


def test_quantile_type7_linear_interpolation():
    values = [Decimal(v) for v in (10, 20, 30, 40)]
    # median index = (4-1)*0.5 = 1.5 -> 20 + 0.5*(30-20) = 25
    assert quantile_type7(values, Decimal(50)) == Decimal("25")
    assert quantile_type7(values, Decimal(0)) == Decimal("10")
    assert quantile_type7(values, Decimal(100)) == Decimal("40")


def test_latency_slo_quantile_and_good_count():
    slo = make_slo(
        sli_type="latency",
        latency_threshold_ms=25,
        latency_percentile=50,
    )
    events = [event(i, i, latency=lat) for i, lat in enumerate((5, 10, 20, 100), start=1)]
    report = agg.compute_window(slo, 0, events)
    assert report["latency"]["quantile_ms"] == "15.0"
    assert report["latency"]["quantile_met"] is True
    assert report["good_count"] == 3


def test_alert_state_machine_fires_and_releases():
    slo = make_slo(burn_consecutive_windows=2, release_consecutive_windows=2)
    previous = None
    verdicts = []
    for window_no, exceeded in enumerate((True, True, False, False), start=1):
        previous = agg.adjudicate_alert(slo, window_no, exceeded, previous)
        verdicts.append(previous)
    assert verdicts[0]["state"] == "ok"
    assert verdicts[1]["transition"] == "fire"
    assert verdicts[1]["state"] == "firing"
    assert verdicts[2]["state"] == "firing"
    assert verdicts[3]["transition"] == "release"
    assert verdicts[3]["state"] == "ok"


def test_compliance_rollup_is_ordered_and_stable():
    slo = make_slo(compliance_period_ms=3000)
    windows = [
        agg.compute_window(slo, i, [event(i * 10 + j, i * 1000 + j, success=(j != 0)) for j in range(1, 5)])
        for i in range(3)
    ]
    rollup = agg.aggregate_compliance(slo, windows)
    assert rollup["window_numbers"] == [0, 1, 2]
    again = agg.aggregate_compliance(slo, list(reversed(windows)))
    assert rollup == again
