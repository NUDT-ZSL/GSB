"""Injection-parameter-driven consistency observations.

Only the description set (records, injection object, sync marker) and the
configured resource limits are used here.  Generator-private intermediate
state is deliberately not part of the dataset contract.
"""

from __future__ import annotations

from typing import Any

from .model import compute_sync_marker, normalize_status
from .paths import PathError, canonical_path


def _field_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {field["path"]: field for field in result.get("fields", [])}


def _code_present(result: dict[str, Any], code: str) -> bool:
    return code in result.get("codes", [])


def _canonical_or_none(value: object) -> str | None:
    try:
        return canonical_path(value)
    except PathError:
        return None


def build_observations(
    dataset: dict[str, Any],
    result: dict[str, Any],
    *,
    field_count: int,
    max_fields: int,
    timeout_ms: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    records = dataset.get("records")
    injection = dataset.get("injection", {})
    if not isinstance(records, list):
        records = []
    if not isinstance(injection, dict):
        injection = {}

    supplied_marker = dataset.get("sync_marker")
    if isinstance(supplied_marker, str):
        actual_marker = compute_sync_marker(records, injection)
        checks.append(
            {
                "name": "sync_marker",
                "expected": supplied_marker,
                "actual": actual_marker,
                "consistent": supplied_marker == actual_marker,
            }
        )

    fields = _field_map(result)
    violations = result.get("violations", [])

    force_cycle_length = injection.get("force_cycle_length")
    if isinstance(force_cycle_length, int) and not isinstance(
        force_cycle_length, bool
    ):
        actual_lengths = sorted(
            {cycle["length"] for cycle in result.get("cycles", [])}
        )
        if force_cycle_length == 0:
            consistent = not actual_lengths
        else:
            consistent = force_cycle_length in actual_lengths
        checks.append(
            {
                "name": "force_cycle_length",
                "expected": force_cycle_length,
                "actual": actual_lengths,
                "consistent": consistent,
            }
        )

    force_downgrade_at = _canonical_or_none(injection.get("force_downgrade_at"))
    if isinstance(injection.get("force_downgrade_at"), str):
        actual = sorted(
            violation["field"]
            for violation in violations
            if violation["code"] == "E_DOWNGRADE"
        )
        checks.append(
            {
                "name": "force_downgrade_at",
                "expected": force_downgrade_at,
                "actual": actual,
                "consistent": bool(force_downgrade_at and force_downgrade_at in actual),
            }
        )

    force_leak_from = _canonical_or_none(injection.get("force_leak_from"))
    if isinstance(injection.get("force_leak_from"), str):
        actual_origins = sorted(
            {
                source["path"]
                for violation in violations
                if violation["code"] == "E_LEAK"
                for source in violation.get("sources", [])
            }
        )
        checks.append(
            {
                "name": "force_leak_from",
                "expected": force_leak_from,
                "actual": actual_origins,
                "consistent": bool(force_leak_from and force_leak_from in actual_origins),
            }
        )

    forge_version_mix_at = _canonical_or_none(
        injection.get("forge_version_mix_at")
    )
    if isinstance(injection.get("forge_version_mix_at"), str):
        actual = sorted(
            violation["field"]
            for violation in violations
            if violation["code"] == "E_VERSION_CONFLICT"
        )
        checks.append(
            {
                "name": "forge_version_mix_at",
                "expected": forge_version_mix_at,
                "actual": actual,
                "consistent": bool(
                    forge_version_mix_at and forge_version_mix_at in actual
                ),
            }
        )

    deep_chain = injection.get("deep_chain")
    if isinstance(deep_chain, dict):
        paths = [
            _canonical_or_none(path)
            for path in deep_chain.get("paths", [])
            if isinstance(path, str)
        ]
        expected_status = normalize_status(deep_chain.get("expect_effective"))
        actual_status = None
        all_present = bool(paths) and all(path in fields for path in paths if path)
        if all_present and paths[-1]:
            actual_status = fields[paths[-1]]["effective_status"]
        consistent = bool(
            all_present
            and expected_status is not None
            and actual_status == expected_status
        )
        checks.append(
            {
                "name": "deep_chain",
                "expected": {
                    "paths": paths,
                    "effective_status": expected_status,
                },
                "actual": {
                    "all_present": all_present,
                    "effective_status": actual_status,
                },
                "consistent": consistent,
            }
        )

    if injection.get("expect_clean") is True:
        actual_codes = result.get("codes", [])
        checks.append(
            {
                "name": "expect_clean",
                "expected": [],
                "actual": actual_codes,
                "consistent": not actual_codes,
            }
        )

    if injection.get("expect_no_cycle") is True:
        actual_cycles = result.get("cycles", [])
        checks.append(
            {
                "name": "expect_no_cycle",
                "expected": [],
                "actual": actual_cycles,
                "consistent": not actual_cycles,
            }
        )

    delay_ms = injection.get("simulate_delay_ms", 0)
    if not isinstance(delay_ms, int) or isinstance(delay_ms, bool):
        delay_ms = 0
    expected_timeout = delay_ms >= timeout_ms
    checks.append(
        {
            "name": "simulated_timeout",
            "expected": expected_timeout,
            "actual": _code_present(result, "E_TIMEOUT"),
            "consistent": expected_timeout == _code_present(result, "E_TIMEOUT"),
        }
    )

    expected_fields = field_count > max_fields
    checks.append(
        {
            "name": "field_limit",
            "expected": expected_fields,
            "actual": _code_present(result, "E_FIELDS"),
            "consistent": expected_fields == _code_present(result, "E_FIELDS"),
        }
    )

    consistent = sum(1 for check in checks if check["consistent"])
    inconsistent = len(checks) - consistent
    return {
        "checked": len(checks),
        "consistent": consistent,
        "inconsistent": inconsistent,
        "checks": checks,
    }
