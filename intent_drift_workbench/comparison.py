"""Structured field-by-field comparisons used by acceptance checks."""

from __future__ import annotations

from typing import Any, Iterable


TURN_FIELDS = (
    "term_counts",
    "category_vector",
    "hit_terms",
    "raw_terms",
    "empty_terms",
    "content_fingerprint",
    "cache_key",
    "normalized_text",
)
WINDOW_FIELDS = (
    "status",
    "reason",
    "covered_turn_numbers",
    "missing_turn_numbers",
    "category_counts",
    "term_counts",
    "referenced_terms",
    "distribution",
    "distribution_empty",
    "drift_score",
    "alert",
    "boundary_equal_threshold",
    "baseline_status",
    "components",
    "contributions",
    "contribution_sum",
    "rounding_residual",
    "exception_type",
)


def _mismatch(scope: str, field: str, left: Any, right: Any, **identifiers: Any) -> dict[str, Any]:
    payload = {"scope": scope, "field": field, "incremental": left, "full": right}
    payload.update(identifiers)
    return payload


def _turn_key(item: dict[str, Any]) -> tuple[str, int]:
    return str(item["session_id"]), int(item["turn_no"])


def _window_key(item: dict[str, Any]) -> tuple[str, int]:
    return str(item["session_id"]), int(item["start_turn"])


def compare_results(
    incremental: dict[str, Any],
    full: dict[str, Any],
    *,
    limit: int = 200,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    left_turns = {_turn_key(item): item for item in incremental.get("turns", [])}
    right_turns = {_turn_key(item): item for item in full.get("turns", [])}
    for key in sorted(set(left_turns) | set(right_turns)):
        if key not in left_turns:
            mismatches.append(
                _mismatch("turn", "__presence__", None, "present", session_id=key[0], turn_no=key[1])
            )
            continue
        if key not in right_turns:
            mismatches.append(
                _mismatch("turn", "__presence__", "present", None, session_id=key[0], turn_no=key[1])
            )
            continue
        for field in TURN_FIELDS:
            if left_turns[key].get(field) != right_turns[key].get(field):
                mismatches.append(
                    _mismatch(
                        "turn",
                        field,
                        left_turns[key].get(field),
                        right_turns[key].get(field),
                        session_id=key[0],
                        turn_no=key[1],
                    )
                )

    left_windows = {_window_key(item): item for item in incremental.get("windows", [])}
    right_windows = {_window_key(item): item for item in full.get("windows", [])}
    for key in sorted(set(left_windows) | set(right_windows)):
        if key not in left_windows:
            mismatches.append(
                _mismatch(
                    "window", "__presence__", None, "present", session_id=key[0], start_turn=key[1]
                )
            )
            continue
        if key not in right_windows:
            mismatches.append(
                _mismatch(
                    "window", "__presence__", "present", None, session_id=key[0], start_turn=key[1]
                )
            )
            continue
        for field in WINDOW_FIELDS:
            if left_windows[key].get(field) != right_windows[key].get(field):
                mismatches.append(
                    _mismatch(
                        "window",
                        field,
                        left_windows[key].get(field),
                        right_windows[key].get(field),
                        session_id=key[0],
                        start_turn=key[1],
                    )
                )

    incremental_failures = incremental.get("failures", [])
    full_failures = full.get("failures", [])
    if incremental_failures != full_failures:
        for index in range(max(len(incremental_failures), len(full_failures))):
            left = incremental_failures[index] if index < len(incremental_failures) else None
            right = full_failures[index] if index < len(full_failures) else None
            if left == right:
                continue
            identifiers = left or right or {}
            mismatches.append(
                _mismatch(
                    "failure",
                    f"failures[{index}]",
                    left,
                    right,
                    session_id=identifiers.get("session_id"),
                    turn_no=identifiers.get("turn_no"),
                    start_turn=identifiers.get("window_start"),
                    business_code=identifiers.get("business_code"),
                )
            )
    if incremental.get("counts") != full.get("counts"):
        mismatches.append(
            _mismatch("counts", "counts", incremental.get("counts"), full.get("counts"))
        )
    return {
        "equal": not mismatches,
        "checked": {
            "turn_fields": list(TURN_FIELDS),
            "window_fields": list(WINDOW_FIELDS),
            "failure_list": True,
        },
        "mismatch_count": len(mismatches),
        "mismatches_truncated": len(mismatches) > limit,
        "mismatches": mismatches[:limit],
    }


def compare_serial_parallel(serial: dict[str, Any], parallel: dict[str, Any], *, limit: int = 200) -> dict[str, Any]:
    result = compare_results(serial, parallel, limit=limit)
    if serial.get("invalidation_history") != parallel.get("invalidation_history"):
        result["mismatches"].append(
            _mismatch(
                "invalidation",
                "invalidation_history",
                serial.get("invalidation_history"),
                parallel.get("invalidation_history"),
            )
        )
    if serial.get("boundary_alerts") != parallel.get("boundary_alerts"):
        result["mismatches"].append(
            _mismatch(
                "boundary_alerts",
                "boundary_alerts",
                serial.get("boundary_alerts"),
                parallel.get("boundary_alerts"),
            )
        )
    result["equal"] = result["equal"] and not (
        serial.get("invalidation_history") != parallel.get("invalidation_history")
        or serial.get("boundary_alerts") != parallel.get("boundary_alerts")
    )
    result["mismatch_count"] = len(result["mismatches"])
    result["mismatches_truncated"] = result["mismatch_count"] > limit
    result["mismatches"] = result["mismatches"][:limit]
    result["execution"] = {
        "serial_concurrency": serial.get("execution", {}).get("max_workers"),
        "parallel_concurrency": parallel.get("execution", {}).get("max_workers"),
        "stable_order": "session_id then numeric turn/start; ties never resolved by completion order",
    }
    return result


def any_mismatches(items: Iterable[dict[str, Any]]) -> bool:
    return any(item.get("mismatch_count", 0) > 0 for item in items)
