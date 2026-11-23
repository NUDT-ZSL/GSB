from __future__ import annotations

import copy

import pytest

from sre_pipeline.engine import apply_event, initial_state, replay_all
from sre_pipeline.errors import (
    E_CLOCK_ROLLBACK,
    E_DEGRADATION_ILLEGAL_REGRESSION,
    E_HALF_OPEN_PROBE_OVERFLOW,
    E_POLICY_VERSION_CONFLICT,
    E_WINDOW_DIGEST_MISMATCH,
    PipelineError,
)
from sre_pipeline.model import load_bundle_from_objects, sha256_json

from .helpers import base_config, clone, event


def _bundle(config=None, events=None, anomalies=None):
    return load_bundle_from_objects(
        config or base_config(),
        {"events": events or [event(1, "svc-a", 0)]},
        {"anomalies": anomalies or []},
    )


def test_replay_is_bit_for_bit_deterministic():
    events = [
        event(1, "svc-a", 0),
        event(2, "svc-a", 50),
        event(3, "svc-a", 100, False),
        event(4, "svc-b", 100, False),
        event(5, "svc-b", 1000),
        event(6, "svc-b", 1500),
        event(7, "svc-b", 1600),
    ]
    bundle = _bundle(events=events)
    first = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    second = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert first["state_sha256"] == second["state_sha256"]
    assert first["records"] == second["records"]
    assert first["final_state"] == second["final_state"]


def test_two_level_rate_limit_and_wait():
    config = base_config()
    config["rate_limit"]["services"]["svc-a"].update(
        capacity=1,
        refill_per_sec=1,
        burst_depth=1,
    )
    events = [
        event(1, "svc-a", 0),
        event(2, "svc-a", 0),
        event(3, "svc-a", 1000),
    ]
    result = replay_all(*(lambda b: (b.policy, b.events, b.anomalies))(_bundle(config, events)))
    assert result["records"][0]["decision"] == "ALLOW"
    assert result["records"][1]["decision"] == "REJECT"
    assert result["records"][1]["error_code"] == "E_RATE_LIMITED"
    assert result["records"][1]["rate_limit"]["wait_ms"] == 1000
    assert result["records"][2]["decision"] == "ALLOW"


def test_global_bucket_blocks_even_when_service_has_tokens():
    config = base_config()
    config["rate_limit"]["global"].update(
        capacity=1,
        refill_per_sec=1,
        burst_depth=1,
    )
    events = [event(1, "svc-a", 0), event(2, "svc-b", 0)]
    result = replay_all(*(lambda b: (b.policy, b.events, b.anomalies))(_bundle(config, events)))
    second = result["records"][1]
    assert second["decision"] == "REJECT"
    assert "global" in second["rate_limit"]["deficiencies"]
    assert second["rate_limit"]["wait_ms"] == 1000


def test_breaker_waits_for_complete_window_then_opens():
    events = [
        event(1, "svc-a", 0, True),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 200, False),
        event(4, "svc-a", 300, True),
        event(5, "svc-a", 1000, True),
    ]
    bundle = _bundle(events=events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert result["records"][3]["circuit"]["after"] == "closed"
    assert result["records"][3]["window"]["finalized"] is False
    opened = result["records"][4]
    assert opened["decision"] == "REJECT"
    assert opened["circuit"]["before"] == "open"
    assert opened["error_code"] == "E_CIRCUIT_OPEN"
    assert opened["completed_windows"][0]["failure_rate_bps"] == 5000


def test_step_checkpoint_delays_window_evaluation():
    config = base_config()
    config["circuit_breaker"]["step_ms"] = 300
    events = [
        event(1, "svc-a", 0, False),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 1100, True),
        event(4, "svc-a", 1200, True),
    ]
    bundle = _bundle(config, events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert result["records"][2]["circuit"]["after"] == "closed"
    assert result["records"][2]["completed_windows"] == []
    assert result["records"][3]["circuit"]["before"] == "open"
    assert result["records"][3]["decision"] == "REJECT"


def test_half_open_successes_close_and_probe_failure_reopens():
    events = [
        event(1, "svc-a", 0, False),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 1000, True),
        event(4, "svc-a", 1500, True),
        event(5, "svc-a", 1600, True),
    ]
    bundle = _bundle(events=events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert result["records"][2]["decision"] == "REJECT"
    assert result["records"][3]["circuit"]["before"] == "half_open"
    assert result["records"][4]["circuit"]["after"] == "closed"

    failed_probe_events = [
        event(1, "svc-a", 0, False),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 1000, True),
        event(4, "svc-a", 1500, False),
        event(5, "svc-a", 1900, True),
        event(6, "svc-a", 2000, True),
    ]
    bundle = _bundle(events=failed_probe_events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert result["records"][3]["circuit"]["after"] == "open"
    assert result["records"][3]["circuit"]["state_detail"]["open_until_ms"] == 2000
    assert result["records"][4]["decision"] == "REJECT"
    assert result["records"][5]["circuit"]["before"] == "half_open"


def test_degradation_rejection_does_not_enter_window_or_error_budget():
    config = base_config()
    config["degradation"]["recovery_successes_per_step"] = 99
    events = [
        event(1, "svc-a", 0, False),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 1000, True),
        event(4, "svc-a", 1500, True),
        event(5, "svc-a", 1600, True),
    ]
    # Find a sequence number that maps outside the L1 0.5 admission slot.
    rejected_seq = next(
        seq
        for seq in range(6, 40)
        if (seq * 2_654_435_761) % 10_000 >= 5_000
    )
    for seq in range(6, rejected_seq + 1):
        events.append(event(seq, "svc-a", 1700 + (seq - 6) * 10, False))
    bundle = _bundle(config, events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    target = result["records"][rejected_seq - 1]
    previous = result["records"][rejected_seq - 2]
    assert target["degradation"]["before"] == "L1"
    assert target["decision"] == "REJECT"
    assert target["error_code"] == "E_DEGRADATION_LIMITED"
    assert target["outcome_recorded"] is False
    assert target["window"]["failure"] == previous["window"]["failure"]
    assert (
        target["global_error_budget"]["failure_count"]
        == previous["global_error_budget"]["failure_count"]
    )


def test_global_error_budget_degrades_and_failure_holds_stable_tier():
    config = base_config()
    config["global_error_budget"] = {"max_failures": 2, "window_ms": 150}
    config["degradation"]["recovery_successes_per_step"] = 2
    config["degradation"]["levels"][0]["ratio"] = 0.4
    config["degradation"]["levels"][1]["ratio"] = 0.8
    events = [
        event(1, "svc-a", 0, False),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 200),
        event(4, "svc-a", 300, False),
        event(5, "svc-a", 400),
    ]
    bundle = _bundle(config, events)
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    assert result["records"][1]["global_error_budget"]["after_exhausted"] is True
    assert result["records"][1]["degradation"]["after"] == "L1"
    assert result["records"][2]["global_error_budget"]["before_exhausted"] is False
    assert result["records"][2]["degradation"]["action"]["direction"] == "wait"
    assert result["records"][3]["degradation"]["action"]["direction"] == "hold"
    assert result["records"][3]["degradation"]["after"] == "L1"


@pytest.mark.parametrize(
    "stage,kind,code",
    [
        ("parse", "policy_version_conflict", E_POLICY_VERSION_CONFLICT),
        ("rate_limit", "clock_rollback_over_tolerance", E_CLOCK_ROLLBACK),
        ("circuit", "window_digest_mismatch", E_WINDOW_DIGEST_MISMATCH),
        ("circuit", "half_open_probe_overflow", E_HALF_OPEN_PROBE_OVERFLOW),
        (
            "degradation",
            "illegal_degradation_regression",
            E_DEGRADATION_ILLEGAL_REGRESSION,
        ),
    ],
)
def test_anomaly_rolls_back_and_isolates_other_services(stage, kind, code):
    config = base_config()
    events = [
        event(1, "svc-a", 0),
        event(2, "svc-b", 0),
        event(3, "svc-a", 100),
    ]
    anomalies = [
        {
            "anomaly_id": "a1",
            "event_seq": 2,
            "service": "svc-b",
            "stage": stage,
            "kind": kind,
            "details": {},
        }
    ]
    bundle = _bundle(config, events, anomalies)
    state = initial_state(bundle.policy)
    state, first = apply_event(bundle.policy, state, bundle.events[0])
    before = copy.deepcopy(state)
    with pytest.raises(PipelineError) as caught:
        apply_event(
            bundle.policy,
            state,
            bundle.events[1],
            bundle.anomalies_for(2),
        )
    assert caught.value.code == code
    assert state == before
    assert caught.value.details["event_seq"] == 2
    assert caught.value.details["service"] == "svc-b"
    assert "window_id" in caught.value.details
    new_state, _ = apply_event(bundle.policy, state, bundle.events[2])
    assert sha256_json(new_state["services"]["svc-b"]) == sha256_json(
        before["services"]["svc-b"]
    )


def test_real_clock_rollback_is_rejected_without_state_change():
    bundle = _bundle(events=[event(1, "svc-a", 100), event(2, "svc-a", 50)])
    state, _ = apply_event(bundle.policy, initial_state(bundle.policy), bundle.events[0])
    before = copy.deepcopy(state)
    with pytest.raises(PipelineError) as caught:
        apply_event(bundle.policy, state, bundle.events[1])
    assert caught.value.code == E_CLOCK_ROLLBACK
    assert caught.value.details["rollback_ms"] == 50
    assert state == before

