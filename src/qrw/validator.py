from __future__ import annotations

from typing import Any

import numpy as np

from .canonical import canonical_json
from .errors import BusinessError, E_INCONSISTENT
from .expansion import expand_query
from .models import CompilerConfig


def _normalized(candidate: dict[str, Any]) -> str:
    return canonical_json(
        {
            "candidate": candidate["candidate"],
            "score_detail": candidate["score_detail"],
            "offset_map": candidate["offset_map"],
        }
    )


def _first_difference(
    *,
    query_id: str,
    left_dialect: str,
    right_dialect: str,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any]:
    limit = max(len(expected), len(actual))
    for index in range(limit):
        left = expected[index] if index < len(expected) else None
        right = actual[index] if index < len(actual) else None
        if left is not None and right is not None and left["candidate"] != right["candidate"]:
            offset = min(len(left["candidate"]), len(right["candidate"]))
            for position, (a, b) in enumerate(zip(left["candidate"], right["candidate"])):
                if a != b:
                    offset = position
                    break
            return {
                "query_id": query_id,
                "candidate_index": index,
                "dialect_pair": [left_dialect, right_dialect],
                "field": "candidate",
                "codepoint_offset": offset,
                "expected": left,
                "actual": right,
            }
        if left is not None and right is not None and left["offset_map"] != right["offset_map"]:
            return {
                "query_id": query_id,
                "candidate_index": index,
                "dialect_pair": [left_dialect, right_dialect],
                "field": "offset_map",
                "codepoint_offset": 0,
                "expected": left["offset_map"],
                "actual": right["offset_map"],
            }
        if left is not None and right is not None and left["score_detail"] != right["score_detail"]:
            return {
                "query_id": query_id,
                "candidate_index": index,
                "dialect_pair": [left_dialect, right_dialect],
                "field": "score_detail",
                "codepoint_offset": 0,
                "expected": left["score_detail"],
                "actual": right["score_detail"],
            }
        if (left is None) != (right is None):
            return {
                "query_id": query_id,
                "candidate_index": index,
                "dialect_pair": [left_dialect, right_dialect],
                "field": "candidate_count",
                "codepoint_offset": 0,
                "expected": left,
                "actual": right,
            }
    return {
        "query_id": query_id,
        "candidate_index": None,
        "dialect_pair": [left_dialect, right_dialect],
        "field": "none",
        "codepoint_offset": 0,
        "expected": None,
        "actual": None,
    }


def _score_check(
    compiled_by_dialect: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> tuple[bool, dict[str, Any] | None]:
    weights: list[float] = []
    slot_points: list[float] = []
    penalties: list[float] = []
    recorded: list[float] = []
    location: list[dict[str, Any]] = []
    for dialect in sorted(compiled_by_dialect):
        for query in queries:
            for candidate in expand_query(compiled_by_dialect[dialect], query, config):
                detail = candidate["score_detail"]
                weights.append(float(detail["weight"]))
                slot_points.append(float(detail["slot_points"]))
                penalties.append(float(detail["depth_penalty"]))
                recorded.append(float(detail["score"]))
                location.append(
                    {
                        "dialect": dialect,
                        "query_id": query["query_id"],
                        "candidate_id": candidate["candidate_id"],
                    }
                )
    if not weights:
        return True, None
    calculated = np.round(
        np.asarray(weights, dtype=np.float64)
        + np.asarray(slot_points, dtype=np.float64)
        - np.asarray(penalties, dtype=np.float64),
        6,
    )
    actual = np.asarray(recorded, dtype=np.float64)
    mismatches = np.flatnonzero(~np.isclose(calculated, actual, rtol=0.0, atol=1e-9))
    if mismatches.size:
        index = int(mismatches[0])
        return False, {
            "inconsistent_category": "score_detail",
            "first_difference_position": {
                **location[index],
                "field": "score",
                "expected": float(calculated[index]),
                "actual": float(actual[index]),
            },
        }
    return True, None


def _fill_check(compiled_by_dialect: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, Any] | None]:
    for dialect in sorted(compiled_by_dialect):
        compiled = compiled_by_dialect[dialect]
        rules = {rule["rule_id"]: rule for rule in compiled["rules"]}
        filled = {(fill["rule_id"], fill["field"]) for fill in compiled.get("fills", [])}
        for fill in compiled.get("fills", []):
            if fill["rule_id"] not in rules:
                return False, {
                    "inconsistent_category": "fill_traceability",
                    "first_difference_position": {
                        "dialect": dialect,
                        "rule_id": fill["rule_id"],
                        "field": fill["field"],
                        "reason": "fill references an unknown rule",
                    },
                }
            if not isinstance(fill.get("source_line"), int) or fill["source_line"] < 1:
                return False, {
                    "inconsistent_category": "fill_traceability",
                    "first_difference_position": {
                        "dialect": dialect,
                        "rule_id": fill["rule_id"],
                        "field": fill["field"],
                        "reason": "fill has no source line",
                    },
                }
            if not fill.get("origin"):
                return False, {
                    "inconsistent_category": "fill_traceability",
                    "first_difference_position": {
                        "dialect": dialect,
                        "rule_id": fill["rule_id"],
                        "field": fill["field"],
                        "reason": "fill has no origin",
                    },
                }
        for rule_id, rule in rules.items():
            for slot in rule["slots"]:
                if not slot["declared"]:
                    expected_fields = {
                        f"slots.{slot['name']}.type",
                        f"slots.{slot['name']}",
                    }
                    if not any((rule_id, field) in filled for field in expected_fields):
                        return False, {
                            "inconsistent_category": "fill_traceability",
                            "first_difference_position": {
                                "dialect": dialect,
                                "rule_id": rule_id,
                                "field": f"slots.{slot['name']}.type",
                                "reason": "inferred slot has no fill record",
                            },
                        }
    return True, None


def verify(
    compiled_by_dialect: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, Any]:
    try:
        score_ok, score_issue = _score_check(compiled_by_dialect, queries, config)
        if not score_ok:
            return _failure(score_issue)
        fill_ok, fill_issue = _fill_check(compiled_by_dialect)
        if not fill_ok:
            return _failure(fill_issue)
        dialects = sorted(compiled_by_dialect)
        expansions = {
            dialect: {
                query["query_id"]: expand_query(compiled_by_dialect[dialect], query, config)
                for query in queries
            }
            for dialect in dialects
        }
        for query in queries:
            query_id = query["query_id"]
            for left_index, left_dialect in enumerate(dialects):
                for right_dialect in dialects[left_index + 1 :]:
                    expected = expansions[left_dialect][query_id]
                    actual = expansions[right_dialect][query_id]
                    expected_set = sorted(_normalized(item) for item in expected)
                    actual_set = sorted(_normalized(item) for item in actual)
                    if expected_set != actual_set:
                        issue = _first_difference(
                            query_id=query_id,
                            left_dialect=left_dialect,
                            right_dialect=right_dialect,
                            expected=expected,
                            actual=actual,
                        )
                        return _failure(
                            {
                                "inconsistent_category": "candidate_set",
                                "first_difference_position": issue,
                            }
                        )
                    if [_normalized(item) for item in expected] != [
                        _normalized(item) for item in actual
                    ]:
                        issue = _first_difference(
                            query_id=query_id,
                            left_dialect=left_dialect,
                            right_dialect=right_dialect,
                            expected=expected,
                            actual=actual,
                        )
                        return _failure(
                            {
                                "inconsistent_category": "candidate_order",
                                "first_difference_position": issue,
                            }
                        )
        return {
            "ok": True,
            "code": None,
            "dialects": dialects,
            "queries_checked": len(queries),
            "checks": {
                "candidate_set_equal": True,
                "candidate_order_equal": True,
                "offset_map_equal": True,
                "score_recomputed": True,
                "fill_traceability": True,
            },
        }
    except BusinessError as exc:
        return {"ok": False, "code": exc.code, "error": exc.to_dict()}


def _failure(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "code": E_INCONSISTENT,
        "error": {
            "code": E_INCONSISTENT,
            "message": "dialect verification failed",
            "line": None,
            "field": issue["first_difference_position"].get("field"),
            "column": None,
            "details": issue,
        },
    }

