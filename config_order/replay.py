"""Snapshot replay comparison with source-level E_REPLAY evidence."""

from __future__ import annotations

from typing import Any

from .canonical import canonical_dumps
from .errors import E_REPLAY, Issue


def _index(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for environment in result.get("environments", []):
        env_id = environment.get("environment_id")
        for key_report in environment.get("keys", []):
            indexed[(env_id, key_report.get("key"))] = key_report
    return indexed


def _first_source(key_report: dict[str, Any] | None) -> str | None:
    if not key_report:
        return None
    decision = key_report.get("decision", {})
    if decision.get("selected_source") is not None:
        return decision["selected_source"]
    involved = decision.get("involved_sources") or []
    if involved:
        return involved[0]
    steps = key_report.get("chain", {}).get("steps", [])
    if steps:
        return steps[0].get("source")
    return None


def compare_results(
    expected: dict[str, Any], actual: dict[str, Any]
) -> list[Issue]:
    expected_index = _index(expected)
    actual_index = _index(actual)
    issues: list[Issue] = []
    for environment_id, key in sorted(set(expected_index) | set(actual_index)):
        expected_key = expected_index.get((environment_id, key))
        actual_key = actual_index.get((environment_id, key))
        field_path = f"environments.{environment_id}.keys.{key}"
        if expected_key is None or actual_key is None:
            issues.append(
                Issue(
                    code=E_REPLAY,
                    field_path=field_path,
                    message="key exists in only one side of the replay comparison",
                    environment_id=environment_id,
                    config_key=key,
                    source_id=_first_source(expected_key or actual_key),
                    detail={
                        "expected_present": expected_key is not None,
                        "actual_present": actual_key is not None,
                    },
                )
            )
            continue

        expected_chain = expected_key.get("chain", {})
        actual_chain = actual_key.get("chain", {})
        expected_decision = expected_key.get("decision", {})
        actual_decision = actual_key.get("decision", {})
        comparisons = {
            "status": (
                expected_key.get("status"),
                actual_key.get("status"),
            ),
            "code": (expected_key.get("code"), actual_key.get("code")),
            "chain_digest": (
                expected_chain.get("digest"),
                actual_chain.get("digest"),
            ),
            "selected_source": (
                expected_decision.get("selected_source"),
                actual_decision.get("selected_source"),
            ),
            "effective_value": (
                expected_decision.get("effective_value"),
                actual_decision.get("effective_value"),
            ),
            "coverage": (
                expected_decision.get("coverage"),
                actual_decision.get("coverage"),
            ),
            "trace": (
                expected_decision.get("trace"),
                actual_decision.get("trace"),
            ),
        }
        mismatches = [
            name
            for name in sorted(comparisons)
            if canonical_dumps(comparisons[name][0])
            != canonical_dumps(comparisons[name][1])
        ]
        if mismatches:
            source = (
                expected_decision.get("selected_source")
                or actual_decision.get("selected_source")
                or _first_source(expected_key)
                or _first_source(actual_key)
            )
            issues.append(
                Issue(
                    code=E_REPLAY,
                    field_path=field_path,
                    message="snapshot replay produced a different result",
                    environment_id=environment_id,
                    config_key=key,
                    source_id=source,
                    detail={
                        "mismatched_fields": mismatches,
                        "expected": {
                            name: comparisons[name][0] for name in mismatches
                        },
                        "actual": {
                            name: comparisons[name][1] for name in mismatches
                        },
                    },
                )
            )
    return issues

