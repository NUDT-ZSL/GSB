from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from flowguard.cli import main
from flowguard.errors import ErrorCode, FlowGuardError
from flowguard.models import parse_sample

from .conftest import event, events_doc, simple_policy, simple_topology, write_json


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("duplicate_service", ErrorCode.TOPOLOGY_DUPLICATE_SERVICE),
        ("open_dependency", ErrorCode.TOPOLOGY_UNKNOWN_DEPENDENCY),
        ("seq_order", ErrorCode.EVENT_SEQ_NOT_INCREASING),
        ("negative_clock", ErrorCode.EVENT_CLOCK_INVALID),
        ("bad_result", ErrorCode.EVENT_RESULT_INVALID),
        ("negative_latency", ErrorCode.EVENT_LATENCY_INVALID),
        ("zero_qps", ErrorCode.POLICY_QPS_INVALID),
        ("zero_capacity", ErrorCode.POLICY_BUCKET_INVALID),
        ("step_over_length", ErrorCode.POLICY_WINDOW_INVALID),
        ("bad_ratio", ErrorCode.POLICY_RATIO_INVALID),
    ],
)
def test_invalid_load_writes_no_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_code: str,
) -> None:
    topology = simple_topology("svc-a")
    policy = simple_policy()
    events = events_doc([event(1)])
    if case == "duplicate_service":
        topology["services"].append(copy.deepcopy(topology["services"][0]))
    elif case == "open_dependency":
        topology["dependencies"] = [{"from": "svc-a", "to": "missing"}]
    elif case == "seq_order":
        events = events_doc([event(1), event(1, clock_ms=1)])
    elif case == "negative_clock":
        events = events_doc([event(1, clock_ms=-1)])
    elif case == "bad_result":
        events = events_doc([event(1, result="unknown")])
    elif case == "negative_latency":
        events = events_doc([event(1, latency_ms=-1)])
    elif case == "zero_qps":
        policy["default"]["qps_limit"] = 0
    elif case == "zero_capacity":
        policy["default"]["token_bucket_capacity"] = 0
    elif case == "step_over_length":
        policy["default"]["window_step_ms"] = 200
    elif case == "bad_ratio":
        policy["default"]["degrade_allow_ratio"] = 1.5

    topology_path = write_json(tmp_path / "topology.json", topology)
    events_path = write_json(tmp_path / "events.json", events)
    policy_path = write_json(tmp_path / "policy.json", policy)
    db_path = tmp_path / "state.db"

    exit_code = main(
        [
            "load",
            "--db",
            str(db_path),
            "--topology",
            str(topology_path),
            "--events",
            str(events_path),
            "--policy",
            str(policy_path),
        ]
    )
    assert exit_code == 2
    assert not db_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == expected_code


def test_same_sample_load_is_idempotent_and_different_sample_is_rejected(
    tmp_path: Path,
) -> None:
    from flowguard import Adjudicator

    topology = simple_topology()
    policy = simple_policy()
    first = parse_sample(topology, events_doc([event(1)]), policy)
    adjudicator = Adjudicator(tmp_path / "state.db")
    first_result = adjudicator.load_sample(first)
    assert first_result["loaded_events"] == 1
    second_result = adjudicator.load_sample(first)
    assert second_result["idempotent"] is True
    assert second_result["loaded_events"] == 0

    changed = parse_sample(
        topology,
        events_doc([event(1, latency_ms=11.0)]),
        policy,
    )
    with pytest.raises(FlowGuardError) as exc_info:
        adjudicator.load_sample(changed)
    assert exc_info.value.code == ErrorCode.SAMPLE_DIGEST_MISMATCH
    counts = adjudicator.store.counts()["events"]
    assert counts == {"total": 1, "processed": 0, "pending": 1}


def test_ratio_and_window_validation_precedes_state_creation(tmp_path: Path) -> None:
    topology = simple_topology()
    policy = simple_policy()
    policy["default"]["half_open_successes_required"] = 3
    with pytest.raises(FlowGuardError) as exc_info:
        parse_sample(topology, events_doc([event(1)]), policy)
    assert exc_info.value.code == ErrorCode.POLICY_INVALID
