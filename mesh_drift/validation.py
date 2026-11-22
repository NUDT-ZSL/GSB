"""Validation and normalization for expected, report, and policy snapshots."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canon import canonical_text, json_digest, sort_unique
from .errors import (
    E_BAD_EXPECTED,
    E_BAD_POLICY,
    E_BAD_REPORT,
    E_REPORT_IDENTITY,
    DriftError,
)

TOTAL_ROUTE_WEIGHT = 100

REPORT_METADATA_FIELDS = (
    "service",
    "instance_id",
    "version",
    "digest",
    "logical_clock_ms",
    "healthy",
    "rules",
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bad_expected(message: str, path: str, **details: Any) -> DriftError:
    return DriftError(E_BAD_EXPECTED, message, path=path, details=details)


def _bad_report(message: str, path: str, **details: Any) -> DriftError:
    return DriftError(E_BAD_REPORT, message, path=path, details=details)


def _require_object(value: Any, code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DriftError(code, "expected a JSON object", path=path)
    return value


def _require_nonempty_string(value: Any, code: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriftError(code, "expected a non-empty string", path=path)
    return value


def _require_nonnegative_int(value: Any, code: str, path: str) -> int:
    if not _is_int(value) or value < 0:
        raise DriftError(code, "expected a non-negative integer", path=path)
    return value


def _require_positive_int(value: Any, code: str, path: str) -> int:
    if not _is_int(value) or value <= 0:
        raise DriftError(code, "expected a positive integer", path=path)
    return value


def _validate_rule(rule: Any, path: str) -> dict[str, Any]:
    rule = _require_object(rule, E_BAD_EXPECTED, path)
    rule_id = _require_nonempty_string(rule.get("rule_id"), E_BAD_EXPECTED, f"{path}.rule_id")
    route_path = _require_nonempty_string(rule.get("path"), E_BAD_EXPECTED, f"{path}.path")
    version_label = _require_nonempty_string(
        rule.get("version_label"), E_BAD_EXPECTED, f"{path}.version_label"
    )
    if "weight" not in rule:
        raise _bad_expected("route weight is required", f"{path}.weight")
    weight = _require_nonnegative_int(rule.get("weight"), E_BAD_EXPECTED, f"{path}.weight")
    return {
        "rule_id": rule_id,
        "path": route_path,
        "version_label": version_label,
        "weight": weight,
    }


def validate_expected(snapshot: Any) -> dict[str, Any]:
    """Validate and normalize an expected snapshot without touching persistence."""

    if not isinstance(snapshot, dict):
        raise _bad_expected("expected snapshot must be a JSON object", "$")
    if snapshot.get("schema_version") != 1:
        raise _bad_expected("unsupported schema_version", "$.schema_version")
    snapshot_id = _require_nonempty_string(
        snapshot.get("snapshot_id"), E_BAD_EXPECTED, "$.snapshot_id"
    )
    logical_clock_ms = _require_nonnegative_int(
        snapshot.get("logical_clock_ms"), E_BAD_EXPECTED, "$.logical_clock_ms"
    )
    services_value = snapshot.get("services")
    if not isinstance(services_value, list) or not services_value:
        raise _bad_expected("services must be a non-empty array", "$.services")

    services: list[dict[str, Any]] = []
    service_names: set[str] = set()
    all_instance_ids: set[str] = set()
    all_rule_ids: set[str] = set()

    for service_index, raw_service in enumerate(services_value):
        service_path = f"$.services[{service_index}]"
        raw_service = _require_object(raw_service, E_BAD_EXPECTED, service_path)
        service_name = _require_nonempty_string(
            raw_service.get("service"), E_BAD_EXPECTED, f"{service_path}.service"
        )
        if service_name in service_names:
            raise _bad_expected(
                "duplicate service name", f"{service_path}.service", service=service_name
            )
        service_names.add(service_name)

        raw_rules = raw_service.get("routes")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise _bad_expected(
                "routes must be a non-empty array", f"{service_path}.routes"
            )
        rules = [
            _validate_rule(rule, f"{service_path}.routes[{rule_index}]")
            for rule_index, rule in enumerate(raw_rules)
        ]
        route_ids: set[str] = set()
        path_weights: dict[str, int] = {}
        labels: set[str] = set()
        for rule in rules:
            rule_id = rule["rule_id"]
            if rule_id in route_ids:
                raise _bad_expected(
                    "duplicate rule_id in service",
                    f"{service_path}.routes",
                    rule_id=rule_id,
                )
            if rule_id in all_rule_ids:
                raise _bad_expected(
                    "duplicate rule_id across services",
                    f"{service_path}.routes",
                    rule_id=rule_id,
                )
            route_ids.add(rule_id)
            all_rule_ids.add(rule_id)
            labels.add(rule["version_label"])
            path_weights[rule["path"]] = path_weights.get(rule["path"], 0) + rule["weight"]

        for route_path, weight_sum in sorted(path_weights.items()):
            if weight_sum != TOTAL_ROUTE_WEIGHT:
                raise _bad_expected(
                    "route weights must sum to the closed total",
                    f"{service_path}.routes",
                    route_path=route_path,
                    weight_sum=weight_sum,
                    expected_sum=TOTAL_ROUTE_WEIGHT,
                )

        raw_instances = raw_service.get("instances")
        if not isinstance(raw_instances, list) or not raw_instances:
            raise _bad_expected(
                "instances must be a non-empty array", f"{service_path}.instances"
            )
        instances: list[dict[str, Any]] = []
        for instance_index, raw_instance in enumerate(raw_instances):
            instance_path = f"{service_path}.instances[{instance_index}]"
            raw_instance = _require_object(raw_instance, E_BAD_EXPECTED, instance_path)
            instance_id = _require_nonempty_string(
                raw_instance.get("instance_id"),
                E_BAD_EXPECTED,
                f"{instance_path}.instance_id",
            )
            if instance_id in all_instance_ids:
                raise _bad_expected(
                    "duplicate instance_id",
                    f"{instance_path}.instance_id",
                    instance_id=instance_id,
                )
            all_instance_ids.add(instance_id)
            endpoint = _require_nonempty_string(
                raw_instance.get("endpoint"), E_BAD_EXPECTED, f"{instance_path}.endpoint"
            )
            version = _require_nonnegative_int(
                raw_instance.get("version"), E_BAD_EXPECTED, f"{instance_path}.version"
            )
            digest = _require_nonempty_string(
                raw_instance.get("digest"), E_BAD_EXPECTED, f"{instance_path}.digest"
            )
            version_label = _require_nonempty_string(
                raw_instance.get("version_label"),
                E_BAD_EXPECTED,
                f"{instance_path}.version_label",
            )
            if version_label not in labels:
                raise _bad_expected(
                    "instance version_label is not closed over declared routes",
                    f"{instance_path}.version_label",
                    version_label=version_label,
                    declared_labels=sort_unique(list(labels)),
                )
            pull_cost = raw_instance.get("pull_cost_logic_ms", 100)
            pull_cost = _require_nonnegative_int(
                pull_cost, E_BAD_EXPECTED, f"{instance_path}.pull_cost_logic_ms"
            )
            instances.append(
                {
                    "instance_id": instance_id,
                    "endpoint": endpoint,
                    "version": version,
                    "digest": digest,
                    "version_label": version_label,
                    "pull_cost_logic_ms": pull_cost,
                }
            )
        instances.sort(key=lambda item: item["instance_id"])
        rules.sort(key=lambda item: (item["rule_id"], item["path"], item["version_label"]))
        services.append(
            {
                "service": service_name,
                "routes": rules,
                "instances": instances,
            }
        )

    services.sort(key=lambda item: item["service"])
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "logical_clock_ms": logical_clock_ms,
        "services": services,
    }


def validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise DriftError(E_BAD_POLICY, "policy must be a JSON object", path="$")
    if policy.get("schema_version") != 1:
        raise DriftError(
            E_BAD_POLICY, "unsupported schema_version", path="$.schema_version"
        )
    policy_id = policy.get("policy_id", "policy")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise DriftError(
            E_BAD_POLICY,
            "policy_id must be a non-empty string",
            path="$.policy_id",
        )
    tolerance_window_ms = _require_positive_int(
        policy.get("tolerance_window_ms"),
        E_BAD_POLICY,
        "$.tolerance_window_ms",
    )
    max_concurrency = _require_positive_int(
        policy.get("max_concurrency"), E_BAD_POLICY, "$.max_concurrency"
    )
    round_budget_ms = _require_positive_int(
        policy.get("round_budget_ms"), E_BAD_POLICY, "$.round_budget_ms"
    )
    dedup_cooldown_ms = _require_nonnegative_int(
        policy.get("dedup_cooldown_ms"), E_BAD_POLICY, "$.dedup_cooldown_ms"
    )
    strict_digest = policy.get("strict_digest")
    if not isinstance(strict_digest, bool):
        raise DriftError(
            E_BAD_POLICY, "strict_digest must be a boolean", path="$.strict_digest"
        )
    instance_timeout_ms = _require_positive_int(
        policy.get("instance_timeout_ms", 1000),
        E_BAD_POLICY,
        "$.instance_timeout_ms",
    )
    max_report_bytes = _require_positive_int(
        policy.get("max_report_bytes", 65536),
        E_BAD_POLICY,
        "$.max_report_bytes",
    )
    return {
        "schema_version": 1,
        "policy_id": policy_id,
        "tolerance_window_ms": tolerance_window_ms,
        "strict_digest": strict_digest,
        "max_concurrency": max_concurrency,
        "round_budget_ms": round_budget_ms,
        "dedup_cooldown_ms": dedup_cooldown_ms,
        "instance_timeout_ms": instance_timeout_ms,
        "max_report_bytes": max_report_bytes,
    }


def get_service(expected: dict[str, Any], service_name: str) -> dict[str, Any]:
    for service in expected["services"]:
        if service["service"] == service_name:
            return service
    raise DriftError(
        E_BAD_EXPECTED,
        "requested service is not declared",
        path="$.services",
        details={"service": service_name},
    )


def parse_report(
    raw_report: Any,
    expected_service: dict[str, Any],
    expected_instance: dict[str, Any],
    *,
    raise_identity_error: bool = False,
) -> dict[str, Any]:
    """Validate typed report fields while allowing metadata omissions.

    Missing metadata is a drift observation, not a malformed response. Wrong
    field types, duplicate rules, or a mismatched identity are hard report
    errors and therefore cannot be converted into ledger drifts.
    """

    if not isinstance(raw_report, dict):
        raise _bad_report("report must be a JSON object", "$")

    normalized: dict[str, Any] = {
        "service": None,
        "instance_id": None,
        "version": None,
        "digest": None,
        "logical_clock_ms": None,
        "healthy": None,
        "rules": None,
        "missing_fields": [],
    }

    if raw_report.get("service") is not None:
        value = _require_nonempty_string(
            raw_report.get("service"), E_BAD_REPORT, "$.service"
        )
        if value != expected_service["service"]:
            error = DriftError(
                E_REPORT_IDENTITY,
                "report service does not match endpoint context",
                path="$.service",
                details={
                    "expected": expected_service["service"],
                    "actual": value,
                    "instance_id": expected_instance["instance_id"],
                },
            )
            if raise_identity_error:
                raise error
            raise error
        normalized["service"] = value

    if raw_report.get("instance_id") is not None:
        value = _require_nonempty_string(
            raw_report.get("instance_id"), E_BAD_REPORT, "$.instance_id"
        )
        if value != expected_instance["instance_id"]:
            raise DriftError(
                E_REPORT_IDENTITY,
                "report instance_id does not match endpoint context",
                path="$.instance_id",
                details={
                    "expected": expected_instance["instance_id"],
                    "actual": value,
                },
            )
        normalized["instance_id"] = value

    if raw_report.get("version") is not None:
        normalized["version"] = _require_nonnegative_int(
            raw_report.get("version"), E_BAD_REPORT, "$.version"
        )
    if raw_report.get("digest") is not None:
        normalized["digest"] = _require_nonempty_string(
            raw_report.get("digest"), E_BAD_REPORT, "$.digest"
        )
    if raw_report.get("logical_clock_ms") is not None:
        normalized["logical_clock_ms"] = _require_nonnegative_int(
            raw_report.get("logical_clock_ms"),
            E_BAD_REPORT,
            "$.logical_clock_ms",
        )
    if raw_report.get("healthy") is not None:
        if not isinstance(raw_report.get("healthy"), bool):
            raise _bad_report("healthy must be a boolean", "$.healthy")
        normalized["healthy"] = raw_report.get("healthy")

    if raw_report.get("rules") is not None:
        raw_rules = raw_report.get("rules")
        if not isinstance(raw_rules, list):
            raise _bad_report("rules must be an array", "$.rules")
        rules: list[dict[str, Any]] = []
        seen_rule_ids: set[str] = set()
        for index, raw_rule in enumerate(raw_rules):
            path = f"$.rules[{index}]"
            if not isinstance(raw_rule, dict):
                raise _bad_report("rule must be an object", path)
            rule_id = _require_nonempty_string(
                raw_rule.get("rule_id"), E_BAD_REPORT, f"{path}.rule_id"
            )
            if rule_id in seen_rule_ids:
                raise _bad_report("duplicate rule_id in report", f"{path}.rule_id")
            seen_rule_ids.add(rule_id)
            rule_path = _require_nonempty_string(
                raw_rule.get("path"), E_BAD_REPORT, f"{path}.path"
            )
            version_label = _require_nonempty_string(
                raw_rule.get("version_label"),
                E_BAD_REPORT,
                f"{path}.version_label",
            )
            weight = _require_nonnegative_int(
                raw_rule.get("weight"), E_BAD_REPORT, f"{path}.weight"
            )
            rules.append(
                {
                    "rule_id": rule_id,
                    "path": rule_path,
                    "version_label": version_label,
                    "weight": weight,
                }
            )
        normalized["rules"] = sorted(
            rules, key=lambda item: (item["rule_id"], item["path"])
        )

    missing = [field for field in REPORT_METADATA_FIELDS if normalized.get(field) is None]
    normalized["missing_fields"] = missing
    return normalized


def prepare_analysis(
    expected_snapshot: Any, policy_snapshot: Any, service_name: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate first, then choose a service. No persistence is involved."""

    expected = validate_expected(expected_snapshot)
    policy = validate_policy(policy_snapshot)
    service = get_service(expected, service_name)
    return expected, policy, deepcopy(service)


def expected_service_digest(service: dict[str, Any]) -> str:
    return json_digest(service)


def stable_text(value: Any) -> str:
    return canonical_text(value, indent=2)
