"""Deterministic drift classification and adjudication."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canon import canonical_text

VERDICT_TOLERATE = "TOLERATE"
VERDICT_AUTO_CONVERGE = "AUTO_CONVERGE"
VERDICT_MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
VERDICT_CONVERGED = "CONVERGED"

VERDICT_PRIORITY = {
    VERDICT_CONVERGED: 0,
    VERDICT_TOLERATE: 1,
    VERDICT_AUTO_CONVERGE: 2,
    VERDICT_MANUAL_INTERVENTION: 3,
}

STRUCTURAL_DRIFT_TYPES = {
    "REPORT_METADATA_MISSING",
    "EXTRA_RULE",
    "MISSING_RULE",
    "ROUTE_CONTENT_MISMATCH",
}


def drift_prior_key(drift_type: str, field_path: str) -> str:
    """Internal identity for persisted dedup state.

    The business key is instance + drift type. A field path is included so
    that two distinct differences of the same type in one report do not
    overwrite each other in the ledger continuation context.
    """

    return f"{drift_type}\x1f{field_path}"


def _drift(
    drift_type: str,
    field_path: str,
    expected: Any,
    actual: Any,
    basis: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "drift_type": drift_type,
        "field_path": field_path,
        "expected": expected,
        "actual": actual,
        "basis": basis,
        "evidence": evidence or {},
    }


def classify_instance(
    service: dict[str, Any],
    expected_instance: dict[str, Any],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare one report with its expected instance and service routes."""

    instance_id = expected_instance["instance_id"]
    drifts: list[dict[str, Any]] = []
    missing = set(report["missing_fields"])
    expected_meta = {
        "service": service["service"],
        "instance_id": instance_id,
        "version": expected_instance["version"],
        "digest": expected_instance["digest"],
        "logical_clock_ms": None,
        "healthy": None,
        "rules": service["routes"],
    }
    for field in sorted(missing):
        drifts.append(
            _drift(
                "REPORT_METADATA_MISSING",
                f"$.{field}",
                expected_meta.get(field),
                None,
                f"report omitted required metadata field {field}",
                {"metadata_field": field, "instance_id": instance_id},
            )
        )

    actual_version = report.get("version")
    expected_version = expected_instance["version"]
    if actual_version is not None:
        if actual_version < expected_version:
            drifts.append(
                _drift(
                    "VERSION_BEHIND",
                    "$.version",
                    expected_version,
                    actual_version,
                    f"actual version {actual_version} is older than expected version {expected_version}",
                    {
                        "lag": expected_version - actual_version,
                        "instance_id": instance_id,
                    },
                )
            )
        elif actual_version > expected_version:
            drifts.append(
                _drift(
                    "VERSION_AHEAD",
                    "$.version",
                    expected_version,
                    actual_version,
                    f"actual version {actual_version} is newer than expected version {expected_version}",
                    {
                        "lead": actual_version - expected_version,
                        "instance_id": instance_id,
                    },
                )
            )

    expected_digest = expected_instance["digest"]
    actual_digest = report.get("digest")
    if actual_digest is not None and actual_digest != expected_digest:
        drifts.append(
            _drift(
                "DIGEST_MISMATCH",
                "$.digest",
                expected_digest,
                actual_digest,
                "reported rule-content digest differs from the expected snapshot digest",
                {
                    "expected_digest": expected_digest,
                    "actual_digest": actual_digest,
                    "instance_id": instance_id,
                },
            )
        )

    actual_rules = report.get("rules")
    if actual_rules is not None:
        expected_rules = {rule["rule_id"]: rule for rule in service["routes"]}
        applied_rules = {rule["rule_id"]: rule for rule in actual_rules}
        expected_ids = set(expected_rules)
        actual_ids = set(applied_rules)
        for rule_id in sorted(actual_ids - expected_ids):
            drifts.append(
                _drift(
                    "EXTRA_RULE",
                    f"$.rules[{rule_id}]",
                    None,
                    applied_rules[rule_id],
                    "instance reports an active rule that is not declared for the service",
                    {"rule_id": rule_id, "instance_id": instance_id},
                )
            )
        for rule_id in sorted(expected_ids - actual_ids):
            drifts.append(
                _drift(
                    "MISSING_RULE",
                    f"$.rules[{rule_id}]",
                    expected_rules[rule_id],
                    None,
                    "instance does not report a rule declared for the service",
                    {"rule_id": rule_id, "instance_id": instance_id},
                )
            )
        for rule_id in sorted(expected_ids & actual_ids):
            expected_rule = expected_rules[rule_id]
            actual_rule = applied_rules[rule_id]
            if actual_rule["weight"] != expected_rule["weight"]:
                drifts.append(
                    _drift(
                        "ROUTE_WEIGHT_MISMATCH",
                        f"$.rules[{rule_id}].weight",
                        expected_rule["weight"],
                        actual_rule["weight"],
                        "effective route weight differs from the declared split",
                        {
                            "rule_id": rule_id,
                            "rule_path": expected_rule["path"],
                            "instance_id": instance_id,
                        },
                    )
                )
            for content_field in ("path", "version_label"):
                if actual_rule[content_field] != expected_rule[content_field]:
                    drifts.append(
                        _drift(
                            "ROUTE_CONTENT_MISMATCH",
                            f"$.rules[{rule_id}].{content_field}",
                            expected_rule[content_field],
                            actual_rule[content_field],
                            f"effective rule {content_field} differs from the declaration",
                            {
                                "rule_id": rule_id,
                                "content_field": content_field,
                                "instance_id": instance_id,
                            },
                        )
                    )
    drifts.sort(
        key=lambda item: (
            item["drift_type"],
            item["field_path"],
            canonical_text(item["expected"]),
            canonical_text(item["actual"]),
        )
    )
    return drifts


def _prior_first_seen(prior_entry: dict[str, Any] | None, current_clock_ms: int) -> int:
    if prior_entry is None:
        return current_clock_ms
    return int(prior_entry["first_seen_clock_ms"])


def adjudicate_drift(
    drift: dict[str, Any],
    report: dict[str, Any],
    policy: dict[str, Any],
    current_clock_ms: int,
    prior_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply the persisted-first-seen tolerant window and escalation matrix."""

    first_seen = _prior_first_seen(prior_entry, current_clock_ms)
    age_ms = max(0, current_clock_ms - first_seen)
    observations = {
        "instance_id": report.get("instance_id"),
        "healthy": report.get("healthy"),
        "strict_digest": policy["strict_digest"],
        "tolerance_window_ms": policy["tolerance_window_ms"],
        "current_clock_ms": current_clock_ms,
        "first_seen_clock_ms": first_seen,
        "drift_age_ms": age_ms,
        "prior_entry_id": None if prior_entry is None else prior_entry["entry_id"],
        "prior_occurrences": 0 if prior_entry is None else prior_entry["occurrences"],
    }
    drift_type = drift["drift_type"]

    if report.get("healthy") is not True:
        return {
            "verdict": VERDICT_MANUAL_INTERVENTION,
            "reason_code": "UNHEALTHY_OR_HEALTH_UNKNOWN",
            "reason": "the instance did not self-declare healthy, so autonomous remediation is unsafe",
            "observations": observations,
        }

    if drift_type in STRUCTURAL_DRIFT_TYPES:
        return {
            "verdict": VERDICT_MANUAL_INTERVENTION,
            "reason_code": "STRUCTURAL_CONFIGURATION_DRIFT",
            "reason": "the drift changes configuration structure and requires operator review",
            "observations": observations,
        }

    if drift_type == "DIGEST_MISMATCH" and policy["strict_digest"]:
        return {
            "verdict": VERDICT_MANUAL_INTERVENTION,
            "reason_code": "STRICT_DIGEST_MISMATCH",
            "reason": "strict digest comparison treats a content-summary mismatch as an integrity incident",
            "observations": observations,
        }

    if drift_type == "VERSION_AHEAD":
        return {
            "verdict": VERDICT_MANUAL_INTERVENTION,
            "reason_code": "UNEXPECTED_VERSION_AHEAD",
            "reason": "the instance reports a version newer than the declaration and cannot be safely converged",
            "observations": observations,
        }

    if age_ms <= policy["tolerance_window_ms"]:
        return {
            "verdict": VERDICT_TOLERATE,
            "reason_code": "WITHIN_TOLERANCE_WINDOW",
            "reason": "drift age is within the logical tolerance window",
            "observations": observations,
        }

    reason_code = (
        "TOLERANCE_WINDOW_EXPIRED"
        if drift_type != "DIGEST_MISMATCH"
        else "NON_STRICT_DIGEST_TOLERANCE_EXPIRED"
    )
    return {
        "verdict": VERDICT_AUTO_CONVERGE,
        "reason_code": reason_code,
        "reason": "the persisted drift has outlived the tolerance window and is eligible for automatic convergence",
        "observations": observations,
    }


def aggregate_decisions(
    instance_id: str,
    drifts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not drifts:
        return {
            "instance_id": instance_id,
            "verdict": VERDICT_CONVERGED,
            "priority": VERDICT_PRIORITY[VERDICT_CONVERGED],
            "priority_basis": "no drift observed in this round",
            "drift_types": [],
        }
    max_priority = max(VERDICT_PRIORITY[item["decision"]["verdict"]] for item in drifts)
    verdict = next(
        name for name, priority in VERDICT_PRIORITY.items() if priority == max_priority
    )
    dominant = [
        {
            "drift_type": item["drift_type"],
            "field_path": item["field_path"],
            "reason_code": item["decision"]["reason_code"],
        }
        for item in sorted(drifts, key=lambda value: (value["drift_type"], value["field_path"]))
        if VERDICT_PRIORITY[item["decision"]["verdict"]] == max_priority
    ]
    return {
        "instance_id": instance_id,
        "verdict": verdict,
        "priority": max_priority,
        "priority_basis": {
            "rule": "MANUAL_INTERVENTION > AUTO_CONVERGE > TOLERATE > CONVERGED",
            "dominant_drifts": dominant,
        },
        "drift_types": sorted({item["drift_type"] for item in drifts}),
    }


def analyze_instance(
    service: dict[str, Any],
    expected_instance: dict[str, Any],
    report: dict[str, Any],
    policy: dict[str, Any],
    current_clock_ms: int,
    prior_entries: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify and adjudicate all drifts for one instance.

    ``prior_entries`` maps drift_type to the latest persisted ledger entry and
    is the only source for tolerance-window age. Nothing is retained in memory
    between rounds.
    """

    prior_entries = prior_entries or {}
    reusable_entries = {
        drift_type: entry
        for drift_type, entry in prior_entries.items()
        if current_clock_ms <= int(entry["cooldown_until_clock_ms"])
    }
    drifts = classify_instance(service, expected_instance, report)
    for drift in drifts:
        prior = reusable_entries.get(
            drift_prior_key(drift["drift_type"], drift["field_path"])
        )
        if prior is None:
            prior = reusable_entries.get(drift["drift_type"])
        drift["decision"] = adjudicate_drift(
            drift,
            report,
            policy,
            current_clock_ms,
            prior,
        )
        drift["drift_key"] = "|".join(
            (
                service["service"],
                expected_instance["instance_id"],
                drift["drift_type"],
                drift["field_path"],
            )
        )
    aggregate = aggregate_decisions(expected_instance["instance_id"], drifts)
    return {
        "instance_id": expected_instance["instance_id"],
        "report_summary": {
            "version": report.get("version"),
            "digest": report.get("digest"),
            "logical_clock_ms": report.get("logical_clock_ms"),
            "healthy": report.get("healthy"),
            "missing_fields": list(report.get("missing_fields", [])),
            "rule_count": None if report.get("rules") is None else len(report["rules"]),
        },
        "drifts": drifts,
        "aggregate_decision": aggregate,
        "drift_types": [item["drift_type"] for item in drifts],
    }


def analyze_observations(
    expected: dict[str, Any],
    service: dict[str, Any],
    policy: dict[str, Any],
    observations: dict[str, dict[str, Any]],
    current_clock_ms: int,
    ledger_context: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    ledger_context = ledger_context or {}
    instances: list[dict[str, Any]] = []
    drift_type_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {
        VERDICT_CONVERGED: 0,
        VERDICT_TOLERATE: 0,
        VERDICT_AUTO_CONVERGE: 0,
        VERDICT_MANUAL_INTERVENTION: 0,
    }
    for expected_instance in service["instances"]:
        instance_id = expected_instance["instance_id"]
        observation = observations.get(instance_id)
        if observation is None or observation.get("outcome") != "PULLED_OK":
            continue
        analysis = analyze_instance(
            service,
            expected_instance,
            observation["report"],
            policy,
            current_clock_ms,
            ledger_context.get(instance_id, {}),
        )
        instances.append(analysis)
        verdict_counts[analysis["aggregate_decision"]["verdict"]] += 1
        for drift_type in analysis["drift_types"]:
            drift_type_counts[drift_type] = drift_type_counts.get(drift_type, 0) + 1
    instances.sort(key=lambda item: item["instance_id"])
    return {
        "schema_version": 1,
        "service": service["service"],
        "logical_clock_ms": current_clock_ms,
        "expected_snapshot_id": expected["snapshot_id"],
        "policy_id": policy["policy_id"],
        "instances": instances,
        "summary": {
            "drift_type_counts": dict(sorted(drift_type_counts.items())),
            "verdict_counts": verdict_counts,
            "instances_with_drift": sum(1 for item in instances if item["drifts"]),
        },
    }


def analysis_without_decisions(
    expected: dict[str, Any],
    service: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Offline structural analysis used by validation-focused callers/tests."""

    results = []
    for expected_instance in service["instances"]:
        instance_id = expected_instance["instance_id"]
        report = reports.get(instance_id)
        if report is None:
            continue
        results.append(
            {
                "instance_id": instance_id,
                "drifts": classify_instance(service, expected_instance, report),
            }
        )
    results.sort(key=lambda item: item["instance_id"])
    return deepcopy(
        {
            "schema_version": 1,
            "service": service["service"],
            "instances": results,
        }
    )
