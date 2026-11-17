from __future__ import annotations

from typing import Any


def _bbox_tuple(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return tuple(int(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def load_truth_pages(truth: Any) -> dict[str, dict[str, Any]]:
    pages = truth.get("pages", []) if isinstance(truth, dict) else []
    result: dict[str, dict[str, Any]] = {}
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("page"), str):
                result[page["page"]] = page
    return result


def validate_page_truth(
    *,
    page: str,
    blocks: list[dict[str, Any]],
    reading_order: list[dict[str, Any]],
    truth_page: dict[str, Any],
    rule: str,
) -> dict[str, Any]:
    truth_blocks = truth_page.get("blocks", [])
    if not isinstance(truth_blocks, list):
        truth_blocks = []
    truth_by_bbox: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for block in truth_blocks:
        if not isinstance(block, dict):
            continue
        bbox = _bbox_tuple(block.get("bbox"))
        if bbox is not None:
            truth_by_bbox[bbox] = block
    actual_by_bbox: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for block in blocks:
        bbox = _bbox_tuple(block.get("bbox"))
        if bbox is not None:
            actual_by_bbox[bbox] = block

    missing_bboxes = sorted(set(truth_by_bbox) - set(actual_by_bbox))
    extra_bboxes = sorted(set(actual_by_bbox) - set(truth_by_bbox))
    role_mismatches: list[dict[str, Any]] = []
    group_mismatches: list[dict[str, Any]] = []
    for bbox in sorted(set(truth_by_bbox) & set(actual_by_bbox)):
        expected = truth_by_bbox[bbox]
        actual = actual_by_bbox[bbox]
        if expected.get("role") != actual.get("role"):
            role_mismatches.append(
                {
                    "bbox": list(bbox),
                    "expected": expected.get("role"),
                    "actual": actual.get("role"),
                }
            )
        expected_group = expected.get("column_group")
        actual_group = actual.get("column_group")
        if expected_group != actual_group:
            group_mismatches.append(
                {
                    "bbox": list(bbox),
                    "expected": expected_group,
                    "actual": actual_group,
                }
            )

    order_mismatches: list[dict[str, Any]] = []
    expected_pairs: list[tuple[int, tuple[int, int, int, int]]] = []
    for bbox, block in truth_by_bbox.items():
        by_rule = block.get("reading_index_by_rule", {})
        expected_index = None
        if isinstance(by_rule, dict):
            expected_index = by_rule.get(rule)
        if expected_index is None:
            expected_index = block.get("reading_index")
        if isinstance(expected_index, int):
            expected_pairs.append((expected_index, bbox))
    expected_pairs.sort(key=lambda item: (item[0], item[1]))
    actual_pairs = []
    for entry in reading_order:
        bbox = _bbox_tuple(entry.get("bbox"))
        index = entry.get("reading_index")
        if bbox is not None and isinstance(index, int):
            actual_pairs.append((index, bbox))
    actual_pairs.sort(key=lambda item: (item[0], item[1]))
    for position, (expected_pair, actual_pair) in enumerate(
        zip(expected_pairs, actual_pairs), start=1
    ):
        if expected_pair[1] != actual_pair[1]:
            order_mismatches.append(
                {
                    "position": position,
                    "expected_bbox": list(expected_pair[1]),
                    "actual_bbox": list(actual_pair[1]),
                    "expected_index": expected_pair[0],
                    "actual_index": actual_pair[0],
                }
            )
    if len(expected_pairs) != len(actual_pairs):
        order_mismatches.append(
            {
                "position": min(len(expected_pairs), len(actual_pairs)) + 1,
                "reason": "reading_order_length_mismatch",
                "expected_length": len(expected_pairs),
                "actual_length": len(actual_pairs),
            }
        )
    passed = not (
        missing_bboxes
        or extra_bboxes
        or role_mismatches
        or group_mismatches
        or order_mismatches
    )
    return {
        "enabled": True,
        "page": page,
        "rule": rule,
        "expected_blocks": len(truth_blocks),
        "actual_blocks": len(blocks),
        "matched_blocks": len(set(truth_by_bbox) & set(actual_by_bbox)),
        "missing_bboxes": [list(item) for item in missing_bboxes],
        "extra_bboxes": [list(item) for item in extra_bboxes],
        "role_mismatches": role_mismatches,
        "group_mismatches": group_mismatches,
        "order_mismatches": order_mismatches,
        "passed": passed,
    }
