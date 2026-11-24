"""Compare component output with deterministic truth via sync markers."""

from __future__ import annotations

from typing import Any


def collect_markers(value: Any, found: set[str] | None = None) -> set[str]:
    markers = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.endswith("marker") and isinstance(item, str):
                markers.add(item)
            collect_markers(item, markers)
    elif isinstance(value, list):
        for item in value:
            collect_markers(item, markers)
    return markers


def _check(
    name: str,
    ok: bool,
    detail_name: str | None = None,
    detail_value: Any = None,
    **details: Any,
) -> dict[str, Any]:
    result = {"name": name, "ok": ok}
    if detail_name is not None:
        result[detail_name] = detail_value
    result.update(details)
    return result


def verify_truth(
    truth: dict[str, Any],
    *,
    ledger_query: dict[str, Any],
    reentry_report: dict[str, Any],
    synthesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if isinstance(synthesis, dict) and isinstance(synthesis.get("objects"), list):
        synthesis = {item["object_key"]: item for item in synthesis["objects"] if "object_key" in item}
    objects = {
        item["object_key"]: item
        for item in ledger_query.get("objects", [])
        if isinstance(item, dict) and "object_key" in item
    }

    level_failures = []
    for object_key, expected_level in sorted(truth.get("expected_effective_levels", {}).items()):
        actual = objects.get(object_key, {}).get("effective_level")
        if actual != expected_level:
            level_failures.append({"object_key": object_key, "expected": expected_level, "actual": actual})
    checks.append(_check("effective_levels", not level_failures, "failures", level_failures))

    status_failures = []
    for object_key, expected_status in sorted(truth.get("expected_decision_status", {}).items()):
        actual = objects.get(object_key, {}).get("decision_status")
        if actual != expected_status:
            status_failures.append({"object_key": object_key, "expected": expected_status, "actual": actual})
    checks.append(_check("decision_status", not status_failures, "failures", status_failures))

    for set_name in ("expected_reentry_set", "expected_changed_object_set", "expected_migration_changed_set"):
        expected = truth.get(set_name)
        if expected is None:
            continue
        actual_name = {
            "expected_reentry_set": "reentry_set",
            "expected_changed_object_set": "changed_object_set",
            "expected_migration_changed_set": "migration_changed_set",
        }[set_name]
        actual = reentry_report.get(actual_name, [])
        checks.append(
            _check(
                actual_name,
                sorted(actual) == sorted(expected),
                expected=sorted(expected),
                actual=sorted(actual),
            )
        )

    marker_haystack = collect_markers(ledger_query) | collect_markers(reentry_report)
    if synthesis is not None:
        marker_haystack |= collect_markers(synthesis)

    missing_grants = [
        item["grant_marker"]
        for item in truth.get("raw_grants", [])
        if item.get("grant_marker") and item["grant_marker"] not in marker_haystack
    ]
    checks.append(_check("raw_grant_sync_markers", not missing_grants, "missing", sorted(missing_grants)))

    missing_revocations = [
        item["revocation_marker"]
        for item in truth.get("revocations", [])
        if item.get("revocation_marker") not in marker_haystack
    ]
    checks.append(_check("revocation_sync_markers", not missing_revocations, "missing", missing_revocations))

    missing_paths = [
        item["path_marker"]
        for item in truth.get("group_paths", [])
        if item.get("path_marker") not in marker_haystack
    ]
    checks.append(_check("group_path_sync_markers", not missing_paths, "missing", missing_paths))

    alternative_failures = []
    evidence_text_markers = collect_markers(reentry_report)
    for object_key, alternatives in truth.get("expected_alternatives", {}).items():
        for expected in alternatives:
            matching = [
                item
                for item in reentry_report.get("alternative_evidence", [])
                if item.get("object_key") == object_key
                and item.get("source_key") == expected["revoked_source_key"]
                and item.get("alternative", {}).get("source_key") == expected["alternative_source_key"]
            ]
            if not matching:
                alternative_failures.append({"object_key": object_key, **expected})
    checks.append(
        _check("alternative_source_evidence", not alternative_failures, "failures", alternative_failures)
    )

    cycle_failures = []
    if synthesis is not None:
        cycle_target = synthesis.get("obj:cycle-target", {})
        actual_cycles = cycle_target.get("group_cycles", [])
        for expected_cycle in truth.get("expected_group_cycles", []):
            if expected_cycle not in actual_cycles:
                cycle_failures.append({"expected": expected_cycle, "actual": actual_cycles})
    checks.append(_check("group_cycles", not cycle_failures, "failures", cycle_failures))

    ok = all(check["ok"] for check in checks)
    return {
        "report_type": "sensitivity-truth-verification",
        "ok": ok,
        "checks": checks,
        "marker_count": len(marker_haystack),
    }


def verify_rejection_codes(truth: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    actual_codes = {item.get("code") for item in issues}
    missing = [code for code in truth.get("expected_rejection_codes", []) if code not in actual_codes]
    return {
        "report_type": "sensitivity-rejection-verification",
        "ok": not missing,
        "missing_codes": missing,
        "actual_codes": sorted(code for code in actual_codes if code),
    }
