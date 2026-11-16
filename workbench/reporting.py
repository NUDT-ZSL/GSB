"""最终 JSON 报告与一致性比对摘要。"""

from __future__ import annotations

from typing import Any

from .store import dump_json


def compare_canonical_results(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> dict[str, Any]:
    """逐项比较查询结果（文档 ID 顺序、命中词项、位置摘要与失败清单）。"""
    mismatches: list[dict[str, Any]] = []
    if len(left) != len(right):
        mismatches.append({
            "field": "query_count",
            "left": len(left),
            "right": len(right),
        })
    for index, (left_item, right_item) in enumerate(zip(left, right)):
        if left_item != right_item:
            differing = sorted(
                key
                for key in set(left_item) | set(right_item)
                if left_item.get(key) != right_item.get(key)
            )
            mismatches.append({
                "query_index": index,
                "query_id": left_item.get("query_id"),
                "differing_fields": differing,
            })
    return {
        "equal": not mismatches,
        "compared_queries": min(len(left), len(right)),
        "mismatches": mismatches,
    }


def write_report(path: str, report: dict[str, Any]) -> None:
    dump_json(path, report)
