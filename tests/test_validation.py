from __future__ import annotations

from copy import deepcopy

import pytest
from conftest import (
    make_expected,
    make_instance,
    make_policy,
    make_service,
    reserve_ports,
    route,
)

from mesh_drift.cli import run as cli_run
from mesh_drift.errors import (
    E_BAD_EXPECTED,
    E_BAD_POLICY,
    E_BAD_REPORT,
    DriftError,
)
from mesh_drift.validation import parse_report, validate_expected, validate_policy


def _valid_fixture():
    routes = [
        route("r1", "/pay", "v1", 70),
        route("r2", "/pay", "v2", 30),
    ]
    port = reserve_ports(1)[0]
    instance = make_instance(port, instance_id="a", version=3, label="v1", routes=routes)
    return make_expected([make_service("checkout", routes, [instance])])


def test_expected_rejects_duplicate_service_and_weights():
    snapshot = _valid_fixture()
    duplicate = deepcopy(snapshot)
    duplicate["services"].append(deepcopy(duplicate["services"][0]))
    with pytest.raises(DriftError) as caught:
        validate_expected(duplicate)
    assert caught.value.code == E_BAD_EXPECTED

    bad_weight = deepcopy(snapshot)
    bad_weight["services"][0]["routes"][0]["weight"] = 60
    with pytest.raises(DriftError) as caught:
        validate_expected(bad_weight)
    assert caught.value.code == E_BAD_EXPECTED
    assert caught.value.details["weight_sum"] == 90


def test_expected_rejects_negative_version_and_unclosed_label():
    snapshot = _valid_fixture()
    snapshot["services"][0]["instances"][0]["version"] = -1
    with pytest.raises(DriftError) as caught:
        validate_expected(snapshot)
    assert caught.value.code == E_BAD_EXPECTED

    snapshot = _valid_fixture()
    snapshot["services"][0]["instances"][0]["version_label"] = "v9"
    with pytest.raises(DriftError) as caught:
        validate_expected(snapshot)
    assert caught.value.code == E_BAD_EXPECTED


def test_report_missing_metadata_is_drift_input_not_hard_error():
    snapshot = validate_expected(_valid_fixture())
    service = snapshot["services"][0]
    instance = service["instances"][0]
    report = parse_report(
        {"instance_id": "a", "version": 3, "rules": service["routes"]},
        service,
        instance,
    )
    assert "digest" in report["missing_fields"]
    assert "healthy" in report["missing_fields"]


def test_report_negative_clock_is_bad_report():
    snapshot = validate_expected(_valid_fixture())
    service = snapshot["services"][0]
    instance = service["instances"][0]
    with pytest.raises(DriftError) as caught:
        parse_report(
            {
                "instance_id": "a",
                "logical_clock_ms": -1,
                "version": 3,
                "digest": "x",
                "healthy": True,
                "rules": service["routes"],
            },
            service,
            instance,
        )
    assert caught.value.code == E_BAD_REPORT


def test_policy_positive_constraints():
    policy = make_policy()
    policy["max_concurrency"] = 0
    with pytest.raises(DriftError) as caught:
        validate_policy(policy)
    assert caught.value.code == E_BAD_POLICY


def test_invalid_input_creates_no_database(tmp_path):
    snapshot = _valid_fixture()
    snapshot["services"][0]["routes"][0]["weight"] = 1
    expected_path = tmp_path / "expected.json"
    policy_path = tmp_path / "policy.json"
    db_path = tmp_path / "should-not-exist.db"
    import json

    expected_path.write_text(json.dumps(snapshot), encoding="utf-8")
    policy_path.write_text(json.dumps(make_policy()), encoding="utf-8")
    code = cli_run(
        [
            "reconcile",
            "--db",
            str(db_path),
            "--expected",
            str(expected_path),
            "--policy",
            str(policy_path),
            "--service",
            "checkout",
            "--clock",
            "1000",
        ]
    )
    assert code == 2
    assert not db_path.exists()

