from __future__ import annotations

from typing import Any

from .canonical import canonical_json
from .errors import BusinessError, E_SEMANTIC_DRIFT
from .expansion import expand_query
from .models import CompilerConfig


def _walk_parts(sequence: dict[str, Any] | None):
    if sequence is None:
        return
    for part in sequence.get("parts", []):
        yield part
        if part["kind"] == "condition":
            yield from _walk_parts(part["then"])
            yield from _walk_parts(part["else"])


def _feature_counts(compiled: dict[str, Any]) -> dict[str, int]:
    counts = {"槽位": 0, "条件分支": 0, "引用": 0}
    for rule in compiled["rules"]:
        if rule["emit"]:
            counts["槽位"] += len(rule["slots"])
        for part in _walk_parts(rule["replacement"]):
            if part["kind"] == "condition":
                counts["条件分支"] += 1
            elif part["kind"] == "reference":
                counts["引用"] += 1
    return counts


def _slot_signature(compiled: dict[str, Any]) -> list[tuple[str, str]]:
    signature: list[tuple[str, str]] = []
    for rule in compiled["rules"]:
        if not rule["emit"]:
            continue
        signature.append(
            (
                rule["rule_id"],
                tuple(sorted((slot["name"], slot["type"]) for slot in rule["slots"])),
            )
        )
    return signature


def _normalized_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": candidate["candidate"],
        "score_detail": candidate["score_detail"],
        "offset_map": candidate["offset_map"],
    }


def _first_difference(
    *,
    query_id: str,
    base_dialect: str,
    other_dialect: str,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any]:
    limit = max(len(expected), len(actual))
    for index in range(limit):
        left = _normalized_candidate(expected[index]) if index < len(expected) else None
        right = _normalized_candidate(actual[index]) if index < len(actual) else None
        if left == right:
            continue
        field = "candidate"
        char_offset = 0
        if left is not None and right is not None:
            left_text = left["candidate"]
            right_text = right["candidate"]
            if left_text != right_text:
                for position, (left_char, right_char) in enumerate(zip(left_text, right_text)):
                    if left_char != right_char:
                        char_offset = position
                        break
                else:
                    char_offset = min(len(left_text), len(right_text))
            elif left["score_detail"] != right["score_detail"]:
                field = "score_detail"
            elif left["offset_map"] != right["offset_map"]:
                field = "offset_map"
        return {
            "query_id": query_id,
            "candidate_index": index,
            "field": field,
            "codepoint_offset": char_offset,
            "base_dialect": base_dialect,
            "other_dialect": other_dialect,
            "expected": left,
            "actual": right,
        }
    return {
        "query_id": query_id,
        "candidate_index": None,
        "field": "none",
        "codepoint_offset": 0,
        "base_dialect": base_dialect,
        "other_dialect": other_dialect,
        "expected": None,
        "actual": None,
    }


def adjudicate(
    compiled_by_dialect: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, Any]:
    dialects = sorted(compiled_by_dialect)
    expansions = {
        dialect: {
            query["query_id"]: expand_query(compiled_by_dialect[dialect], query, config)
            for query in queries
        }
        for dialect in dialects
    }
    counts = {dialect: _feature_counts(compiled_by_dialect[dialect]) for dialect in dialects}
    reference_dialect = dialects[-1]
    structural_categories: list[str] = []
    if "v1" in dialects:
        if counts[reference_dialect]["条件分支"] > counts["v1"]["条件分支"]:
            structural_categories.append("条件分支")
        if counts[reference_dialect]["引用"] > counts["v1"]["引用"]:
            structural_categories.append("引用")
        v1_slots = sorted(
            (name, slot_type)
            for _rule_id, slots in _slot_signature(compiled_by_dialect["v1"])
            for name, slot_type in slots
        )
        ref_slots = sorted(
            (name, slot_type)
            for _rule_id, slots in _slot_signature(compiled_by_dialect[reference_dialect])
            for name, slot_type in slots
        )
        if v1_slots != ref_slots:
            structural_categories.insert(0, "槽位")
    pair_results: list[dict[str, Any]] = []
    first_difference: dict[str, Any] | None = None
    consistent = True
    for query in queries:
        query_id = query["query_id"]
        for left_index, left_dialect in enumerate(dialects):
            for right_dialect in dialects[left_index + 1 :]:
                expected = expansions[left_dialect][query_id]
                actual = expansions[right_dialect][query_id]
                same_ordered = [
                    _normalized_candidate(item) for item in expected
                ] == [_normalized_candidate(item) for item in actual]
                same_set = sorted(
                    canonical_json(_normalized_candidate(item)) for item in expected
                ) == sorted(canonical_json(_normalized_candidate(item)) for item in actual)
                pair_results.append(
                    {
                        "query_id": query_id,
                        "dialects": [left_dialect, right_dialect],
                        "candidate_set_equal": same_set,
                        "candidate_order_equal": same_ordered,
                    }
                )
                if not same_ordered:
                    consistent = False
                    if first_difference is None:
                        first_difference = _first_difference(
                            query_id=query_id,
                            base_dialect=left_dialect,
                            other_dialect=right_dialect,
                            expected=expected,
                            actual=actual,
                        )
    fills = [
        {"dialect": dialect, **fill}
        for dialect in dialects
        for fill in compiled_by_dialect[dialect].get("fills", [])
    ]
    report: dict[str, Any] = {
        "status": "EQUIVALENT" if consistent and not structural_categories else "DRIFT",
        "ok": consistent and not structural_categories,
        "code": None if consistent and not structural_categories else E_SEMANTIC_DRIFT,
        "dialects": dialects,
        "queries_checked": len(queries),
        "feature_counts": counts,
        "unrecoverable_categories": structural_categories,
        "candidate_sets_equal": consistent,
        "candidate_order_equal": consistent,
        "first_difference": first_difference,
        "pair_results": pair_results,
        "default_fill_sources": fills,
        "expansion": expansions,
    }
    if report["code"]:
        categories = structural_categories or ["槽位"]
        report["error"] = {
            "code": E_SEMANTIC_DRIFT,
            "message": "template semantics cannot be reproduced across all dialects",
            "details": {
                "categories": categories,
                "first_difference": first_difference,
            },
        }
    return report


def adjudicate_or_raise(*args: Any, **kwargs: Any) -> dict[str, Any]:
    report = adjudicate(*args, **kwargs)
    if report.get("code"):
        raise BusinessError(
            report["code"],
            report["error"]["message"],
            details=report["error"]["details"],
        )
    return report

