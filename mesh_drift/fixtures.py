"""Offline report-fixture parsing for deterministic analysis."""

from __future__ import annotations

from typing import Any

from .engine import analyze_observations
from .errors import E_BAD_FIXTURE, DriftError
from .validation import get_service, parse_report


def reports_from_fixture(
    fixture: Any,
    service: dict[str, Any],
    *,
    raise_on_error: bool = True,
    ignore_unknown: bool = False,
) -> dict[str, dict[str, Any]]:
    if not isinstance(fixture, dict):
        raise DriftError(E_BAD_FIXTURE, "report fixture must be an object", path="$")
    raw_reports = fixture.get("reports", fixture)
    if isinstance(raw_reports, list):
        entries: dict[str, Any] = {}
        for index, item in enumerate(raw_reports):
            if not isinstance(item, dict):
                raise DriftError(
                    E_BAD_FIXTURE, "fixture report entry must be an object", path=f"$.reports[{index}]"
                )
            instance_id = item.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                raise DriftError(
                    E_BAD_FIXTURE,
                    "fixture report entry requires instance_id",
                    path=f"$.reports[{index}].instance_id",
                )
            if "response" in item:
                entries[instance_id] = item["response"]
            elif "raw_response" in item:
                entries[instance_id] = item["raw_response"]
            else:
                raise DriftError(
                    E_BAD_FIXTURE,
                    "fixture report entry requires response",
                    path=f"$.reports[{index}]",
                )
        raw_reports = entries
    if not isinstance(raw_reports, dict):
        raise DriftError(E_BAD_FIXTURE, "reports must be an object or array", path="$.reports")

    expected_instances = {item["instance_id"]: item for item in service["instances"]}
    observations: dict[str, dict[str, Any]] = {}
    for instance_id in sorted(raw_reports):
        expected_instance = expected_instances.get(instance_id)
        if expected_instance is None:
            if ignore_unknown:
                continue
            if raise_on_error:
                raise DriftError(
                    E_BAD_FIXTURE,
                    "fixture report references an instance outside the selected service",
                    path=f"$.reports.{instance_id}",
                )
            continue
        report = parse_report(raw_reports[instance_id], service, expected_instance)
        observations[instance_id] = {
            "instance_id": instance_id,
            "outcome": "PULLED_OK",
            "error_code": None,
            "http_status": 200,
            "raw_body": None,
            "diagnostic": {"source": "offline_fixture"},
            "report": report,
        }
    return observations


def analyze_fixture(
    expected: dict[str, Any],
    policy: dict[str, Any],
    fixture: Any,
    service_name: str,
    logical_clock_ms: int,
) -> dict[str, Any]:
    service = get_service(expected, service_name)
    observations = reports_from_fixture(fixture, service, ignore_unknown=True)
    return analyze_observations(
        expected,
        service,
        policy,
        observations,
        logical_clock_ms,
        {},
    )

