from __future__ import annotations

from copy import deepcopy

from conftest import (
    make_expected,
    make_instance,
    make_policy,
    make_service,
    reserve_ports,
    route,
)

from mesh_drift.canon import canonical_text
from mesh_drift.engine import (
    VERDICT_AUTO_CONVERGE,
    VERDICT_MANUAL_INTERVENTION,
    VERDICT_TOLERATE,
    analyze_instance,
    classify_instance,
)
from mesh_drift.validation import validate_expected


def _setup():
    routes = [
        route("r-stable", "/pay", "v1", 80),
        route("r-canary", "/pay", "v2", 20),
    ]
    port = reserve_ports(1)[0]
    instance = make_instance(port, instance_id="a", version=7, label="v1", routes=routes)
    expected = validate_expected(
        make_expected([make_service("checkout", routes, [instance])])
    )
    service = expected["services"][0]
    return expected, service, service["instances"][0]


def test_all_required_drift_types_and_stable_order():
    _expected, service, instance = _setup()
    report = {
        "service": "checkout",
        "instance_id": "a",
        "version": 6,
        "digest": "sha256:tampered",
        "logical_clock_ms": 900,
        "healthy": True,
        "rules": [
            route("r-stable", "/pay", "v1", 70),
            route("r-extra", "/new", "v9", 0),
        ],
        "missing_fields": [],
    }
    first = classify_instance(service, instance, report)
    second = deepcopy(first)
    assert canonical_text(first) == canonical_text(second)
    types = {item["drift_type"] for item in first}
    assert {
        "VERSION_BEHIND",
        "DIGEST_MISMATCH",
        "EXTRA_RULE",
        "MISSING_RULE",
        "ROUTE_WEIGHT_MISMATCH",
    }.issubset(types)
    keys = [(item["drift_type"], item["field_path"]) for item in first]
    assert keys == sorted(keys)
    for item in first:
        assert item["field_path"].startswith("$.")
        assert "basis" in item
        assert "expected" in item
        assert "actual" in item


def test_metadata_missing_is_classified():
    _, _, instance = _setup()
    routes = [
        route("r-stable", "/pay", "v1", 80),
        route("r-canary", "/pay", "v2", 20),
    ]
    service = make_service("checkout", routes, [instance])
    report = {
        "service": "checkout",
        "instance_id": "a",
        "version": 7,
        "digest": instance["digest"],
        "logical_clock_ms": 900,
        "healthy": None,
        "rules": routes,
        "missing_fields": ["healthy"],
    }
    drifts = classify_instance(service, instance, report)
    assert [item["drift_type"] for item in drifts] == ["REPORT_METADATA_MISSING"]


def test_decision_matrix_tolerance_escalation_and_health():
    _expected, service, instance = _setup()
    policy = make_policy(tolerance=5000, strict=True)
    report = {
        "service": "checkout",
        "instance_id": "a",
        "version": 6,
        "digest": instance["digest"],
        "logical_clock_ms": 900,
        "healthy": True,
        "rules": service["routes"],
        "missing_fields": [],
    }
    first = analyze_instance(service, instance, report, policy, 1000, {})
    assert first["aggregate_decision"]["verdict"] == VERDICT_TOLERATE
    prior = {
        "entry_id": "drift-prior",
        "first_seen_clock_ms": 1000,
        "occurrences": 1,
        "cooldown_until_clock_ms": 9000,
    }
    later = analyze_instance(
        service,
        instance,
        report,
        policy,
        7000,
        {"VERSION_BEHIND": prior},
    )
    assert later["aggregate_decision"]["verdict"] == VERDICT_AUTO_CONVERGE

    unhealthy_report = dict(report)
    unhealthy_report["healthy"] = False
    unhealthy = analyze_instance(service, instance, unhealthy_report, policy, 1000, {})
    assert unhealthy["aggregate_decision"]["verdict"] == VERDICT_MANUAL_INTERVENTION
    assert unhealthy["aggregate_decision"]["priority_basis"]["dominant_drifts"][0][
        "reason_code"
    ] == "UNHEALTHY_OR_HEALTH_UNKNOWN"

    tampered = dict(report)
    tampered["digest"] = "sha256:tampered"
    strict = analyze_instance(service, instance, tampered, policy, 1000, {})
    assert strict["aggregate_decision"]["verdict"] == VERDICT_MANUAL_INTERVENTION
    non_strict_policy = make_policy(tolerance=5000, strict=False)
    non_strict = analyze_instance(service, instance, tampered, non_strict_policy, 1000, {})
    assert non_strict["aggregate_decision"]["verdict"] == VERDICT_TOLERATE


def test_multiple_drifts_are_aggregated_not_dropped():
    _, service, instance = _setup()
    report = {
        "service": "checkout",
        "instance_id": "a",
        "version": 6,
        "digest": "sha256:tampered",
        "logical_clock_ms": 900,
        "healthy": False,
        "rules": [
            route("r-stable", "/pay", "v1", 70),
            route("r-canary", "/pay", "v2", 20),
            route("r-extra", "/new", "v9", 0),
        ],
        "missing_fields": [],
    }
    result = analyze_instance(service, instance, report, make_policy(), 1000, {})
    assert len(result["drifts"]) >= 4
    assert result["aggregate_decision"]["verdict"] == VERDICT_MANUAL_INTERVENTION
    assert len(result["aggregate_decision"]["priority_basis"]["dominant_drifts"]) >= 2
