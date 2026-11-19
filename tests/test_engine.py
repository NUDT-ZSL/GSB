from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flowguard import Adjudicator
from flowguard.errors import ErrorCode, FlowGuardError
from flowguard.models import Event
from flowguard.stats import layered_quantile
from flowguard.util import canonical_json

from .conftest import event, events_doc, fixed_sample, make_sample, simple_policy


def test_fixed_sample_covers_rate_breaker_degrade_and_both_modes(
    fixed_db,
) -> None:
    db, adjudicator, report = fixed_db
    assert len(report["decision_digest"]) == 64
    assert report["decisions"] == {"allow": 24, "degrade_allow": 2, "reject": 2}
    assert report["modes"] == {"exact": 24, "layered": 4}

    gateway_reject = adjudicator.explain(7)["decision"]
    assert gateway_reject["final"]["decision"] == "reject"
    assert gateway_reject["final"]["source"] == "rate_limit"
    assert gateway_reject["rate_limit"]["cost_units"] == 3_000_000
    assert gateway_reject["rate_limit"]["tokens_before_units"] < 3_000_000

    exact = adjudicator.explain(13)["decision"]
    layered = adjudicator.explain(14)["decision"]
    restore = adjudicator.explain(18)["decision"]
    assert exact["stats_mode"] == "exact"
    assert layered["stats_mode"] == "layered"
    assert layered["mode_switch"]["trigger"] == "window_count_exceeds_retention_limit"
    assert layered["window_evidence"]["error_upper_bound"] > 0
    assert restore["stats_mode"] == "exact"
    assert (
        restore["mode_switch"]["trigger"]
        == "window_count_at_or_below_restore_threshold"
    )

    opened = adjudicator.explain(22)["decision"]
    held = adjudicator.explain(23)["decision"]
    probe = adjudicator.explain(24)["decision"]
    closed = adjudicator.explain(25)["decision"]
    degraded = adjudicator.explain(26)["decision"]
    recovered = adjudicator.explain(27)["decision"]
    normal = adjudicator.explain(28)["decision"]
    assert opened["breaker"]["transition"] == "closed_to_open"
    assert opened["degrade"]["active_after"] is True
    assert held["final"]["decision"] == "reject"
    assert held["final"]["source"] == "breaker"
    assert probe["breaker"]["state_at_gate"] == "half_open"
    assert probe["final"]["source"] == "breaker_half_open"
    assert closed["breaker"]["transition"] == "half_open_to_closed"
    assert closed["breaker"]["budget_recycled"] is True
    assert closed["rate_limit"]["tokens_after"] == 5.0
    assert degraded["final"]["decision"] == "degrade_allow"
    assert degraded["degrade"]["probation"] is True
    assert recovered["degrade"]["active_after"] is False
    assert normal["degrade"]["active_before"] is False

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM window_events").fetchone()[0] == 26
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 28
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM window_events WHERE seq IN (24, 25)"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_two_runs_are_bitwise_deterministic(tmp_path: Path) -> None:
    digests = []
    explains = []
    for index in range(2):
        adjudicator = Adjudicator(tmp_path / f"run-{index}.db")
        adjudicator.load_sample(fixed_sample(), reset=True)
        report = adjudicator.run()
        digests.append(report["decision_digest"])
        explains.append(adjudicator.explain(27)["decision"])
    assert digests[0] == digests[1]
    assert explains[0] == explains[1]


def test_clock_rollback_exceeding_tolerance_preserves_state(tmp_path: Path) -> None:
    adjudicator = Adjudicator(tmp_path / "clock.db")
    sample = make_sample(events=events_doc([event(1, clock_ms=100)]))
    adjudicator.load_sample(sample, reset=True)
    adjudicator.run()
    before = adjudicator.status()

    with pytest.raises(FlowGuardError) as exc_info:
        adjudicator.process_event(
            Event(2, "svc-a", 94, "success", 10.0, 1.0)
        )
    assert exc_info.value.code == ErrorCode.CLOCK_ROLLBACK
    after = adjudicator.status()
    assert before["services"] == after["services"]
    assert after["counts"]["events"] == {
        "total": 1,
        "processed": 1,
        "pending": 0,
    }

    within_tolerance = adjudicator.process_event(
        Event(2, "svc-a", 98, "success", 10.0, 1.0)
    )
    assert within_tolerance["effective_clock_ms"] == 100
    assert within_tolerance["window_id"] == 5


def test_token_bucket_capacity_and_cost_decision(tmp_path: Path) -> None:
    policy = simple_policy(
        qps_limit=1,
        token_bucket_capacity=2,
        clock_tolerance_ms=5,
    )
    sample = make_sample(
        events=events_doc(
            [
                event(1, clock_ms=0),
                event(2, clock_ms=0),
                event(3, clock_ms=0),
            ]
        ),
        policy=policy,
    )
    adjudicator = Adjudicator(tmp_path / "rate.db")
    adjudicator.load_sample(sample, reset=True)
    report = adjudicator.run()
    assert report["decisions"] == {"allow": 2, "reject": 1}
    assert adjudicator.explain(1)["decision"]["final"]["decision"] == "allow"
    assert adjudicator.explain(2)["decision"]["final"]["decision"] == "allow"
    rejected = adjudicator.explain(3)["decision"]
    assert rejected["final"]["decision"] == "reject"
    assert rejected["final"]["source"] == "rate_limit"
    assert rejected["rate_limit"]["tokens_after_units"] == 0


def test_mode_switch_failure_is_business_error_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = simple_policy(
        retention_limit=1,
        mode_restore_ratio=0.5,
        qps_limit=1000,
        token_bucket_capacity=20,
    )
    sample = make_sample(
        events=events_doc(
            [
                event(1, clock_ms=0),
                event(2, clock_ms=1),
            ]
        ),
        policy=policy,
    )
    adjudicator = Adjudicator(tmp_path / "mode.db")
    adjudicator.load_sample(sample, reset=True)
    first = adjudicator.process_event(Event(1, "svc-a", 0, "success", 10.0, 1.0))
    assert first["stats_mode"] == "exact"

    def fail(*args, **kwargs):
        raise RuntimeError("forced summary failure")

    monkeypatch.setattr("flowguard.stats.layered_quantile", fail)
    with pytest.raises(FlowGuardError) as exc_info:
        adjudicator.process_event(Event(2, "svc-a", 1, "success", 10.0, 1.0))
    error = exc_info.value
    assert error.code == ErrorCode.MODE_SWITCH_FAILED
    assert error.fields["service"] == "svc-a"
    assert error.fields["window_id"] == 0
    assert "forced summary failure" in error.fields["reason"]
    status = adjudicator.status()
    assert status["counts"]["events"] == {
        "total": 2,
        "processed": 1,
        "pending": 1,
    }
    assert status["services"][0]["statistics"]["mode"] == "exact"


def test_zero_valid_requests_has_distinct_error() -> None:
    with pytest.raises(FlowGuardError) as exc_info:
        layered_quantile({}, 0.95, 16)
    assert exc_info.value.code == ErrorCode.ZERO_VALID_REQUESTS
    assert "window" in exc_info.value.message or "summary" in exc_info.value.message


@pytest.mark.parametrize(
    ("bad_event", "expected_code"),
    [
        (Event(1, "svc-a", 0, "bogus", 10.0, 1.0), ErrorCode.EVENT_RESULT_INVALID),
        (Event(1, "svc-a", 0, "success", -1.0, 1.0), ErrorCode.EVENT_LATENCY_INVALID),
        (Event(1, "missing", 0, "success", 1.0, 1.0), ErrorCode.EVENT_FIELD_INVALID),
    ],
)
def test_direct_api_event_validation_does_not_write(
    tmp_path: Path,
    bad_event: Event,
    expected_code: str,
) -> None:
    adjudicator = Adjudicator(tmp_path / "direct.db")
    adjudicator.load_sample(make_sample(), reset=True)
    with pytest.raises(FlowGuardError) as exc_info:
        adjudicator.process_event(bad_event)
    assert exc_info.value.code == expected_code
    assert adjudicator.store.counts()["events"] == {
        "total": 1,
        "processed": 0,
        "pending": 1,
    }


def test_duplicate_event_returns_bitwise_identical_decision(tmp_path: Path) -> None:
    adjudicator = Adjudicator(tmp_path / "duplicate.db")
    adjudicator.load_sample(make_sample(), reset=True)
    first = adjudicator.process_event(Event(1, "svc-a", 0, "success", 10.0, 1.0))
    second = adjudicator.process_event(Event(1, "svc-a", 0, "success", 10.0, 1.0))
    assert canonical_json(first) == canonical_json(second)
    assert adjudicator.store.counts()["events"] == {
        "total": 1,
        "processed": 1,
        "pending": 0,
    }
