from __future__ import annotations

from typing import Any, Dict

import pytest

from sre_pipeline.errors import (
    E_BAD_EVENT,
    E_BAD_POLICY,
    E_BAD_TOPOLOGY,
    PipelineError,
)
from sre_pipeline.model import load_bundle_from_objects

from .helpers import base_config, clone, event, run_cli, write_sample


def _load(config: Dict[str, Any], events=None, anomalies=None):
    return load_bundle_from_objects(
        config,
        {"events": events or [event(1, "svc-a", 0)]},
        {"anomalies": anomalies or []},
    )


def test_valid_bundle_and_stable_hashes():
    config = base_config()
    events = [event(1, "svc-a", 0), event(2, "svc-b", 10, False)]
    first = _load(config, events)
    second = _load(clone(config), clone(events))
    assert first.config_sha256 == second.config_sha256
    assert first.events_sha256 == second.events_sha256
    assert first.anomalies_sha256 == second.anomalies_sha256


@pytest.mark.parametrize(
    "mutator,expected",
    [
        (
            lambda c: c["topology"]["services"].append(
                clone(c["topology"]["services"][0])
            ),
            E_BAD_TOPOLOGY,
        ),
        (
            lambda c: c["topology"]["services"][1]["upstream"].append("missing"),
            E_BAD_TOPOLOGY,
        ),
        (
            lambda c: c["topology"]["services"][0]["upstream"].append("svc-b"),
            E_BAD_TOPOLOGY,
        ),
        (
            lambda c: c["rate_limit"]["services"]["svc-a"].update(capacity=0),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["rate_limit"]["services"]["svc-a"].update(refill_per_sec=0),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["circuit_breaker"].update(step_ms=1001),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["circuit_breaker"].update(failure_rate_threshold=0),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["circuit_breaker"].update(failure_rate_threshold=1),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["degradation"]["levels"][1].update(ratio=0.25),
            E_BAD_POLICY,
        ),
        (
            lambda c: c["degradation"]["levels"][-1].update(ratio=0.999),
            E_BAD_POLICY,
        ),
    ],
)
def test_invalid_config_codes(mutator, expected):
    config = base_config()
    mutator(config)
    with pytest.raises(PipelineError) as caught:
        _load(config)
    assert caught.value.code == expected
    assert caught.value.details.get("rule")


@pytest.mark.parametrize(
    "events,expected",
    [
        ([event(1, "svc-a", 0), event(1, "svc-b", 1)], E_BAD_EVENT),
        ([event(1, "missing", 0)], E_BAD_EVENT),
        ([event(1, "svc-a", -1)], E_BAD_EVENT),
    ],
)
def test_invalid_event_codes(events, expected):
    with pytest.raises(PipelineError) as caught:
        _load(base_config(), events)
    assert caught.value.code == expected


def test_invalid_load_does_not_create_database(tmp_path):
    config = base_config()
    config["rate_limit"]["global"]["capacity"] = 0
    paths = write_sample(tmp_path / "sample", config, [event(1, "svc-a", 0)])
    db_path = tmp_path / "state.sqlite"
    result = run_cli(
        "load",
        "--db",
        str(db_path),
        "--config",
        str(paths["config"]),
        "--events",
        str(paths["events"]),
        "--anomalies",
        str(paths["anomalies"]),
    )
    assert result.returncode == 2
    assert E_BAD_POLICY in result.stderr
    assert not db_path.exists()


def test_duplicate_anomaly_id_is_rejected():
    anomalies = [
        {
            "anomaly_id": "dup",
            "event_seq": 1,
            "service": "svc-a",
            "stage": "parse",
            "kind": "policy_version_conflict",
        },
        {
            "anomaly_id": "dup",
            "event_seq": 1,
            "service": "svc-a",
            "stage": "rate_limit",
            "kind": "clock_rollback_over_tolerance",
        },
    ]
    with pytest.raises(PipelineError) as caught:
        _load(base_config(), [event(1, "svc-a", 0)], anomalies)
    assert caught.value.code == "E_BAD_ANOMALY"

