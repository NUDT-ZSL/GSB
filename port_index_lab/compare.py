"""Order-sensitive result comparison and path diagnostics."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .bptree import BPlusTree
from .models import PortRecord, QuerySpec


def _counter_details(
    expected: Counter[int],
    actual: Counter[int],
) -> list[dict[str, Any]]:
    return [
        {
            "record_id": record_id,
            "expected_count": expected.get(record_id, 0),
            "actual_count": actual.get(record_id, 0),
        }
        for record_id in sorted(set(expected) | set(actual))
        if expected.get(record_id, 0) != actual.get(record_id, 0)
    ]


def compare_hits(
    *,
    index_name: str | None = None,
    query: QuerySpec,
    baseline: Iterable[PortRecord],
    index_scan: dict[str, Any],
) -> dict[str, Any]:
    baseline_list = list(baseline)
    baseline_ids = [record.write_seq for record in baseline_list]
    index_ids = list(index_scan["hit_record_ids"])
    baseline_counter = Counter(baseline_ids)
    index_counter = Counter(index_ids)
    missing_ids = sorted((baseline_counter - index_counter).elements())
    extra_ids = sorted((index_counter - baseline_counter).elements())
    duplicate_ids = sorted(
        record_id for record_id, count in index_counter.items() if count > 1
    )
    duplicate_baseline_ids = sorted(
        record_id for record_id, count in baseline_counter.items() if count > 1
    )

    first_divergence: dict[str, Any] | None = None
    for position in range(max(len(baseline_ids), len(index_ids))):
        baseline_id = baseline_ids[position] if position < len(baseline_ids) else None
        index_id = index_ids[position] if position < len(index_ids) else None
        if baseline_id != index_id:
            if baseline_id is None:
                kind = "extra_index_record"
            elif index_id is None:
                kind = "missing_index_record"
            else:
                kind = "order_divergence"
            first_divergence = {
                "position": position,
                "type": kind,
                "baseline_record_id": baseline_id,
                "index_record_id": index_id,
                "index_leaf_ids": index_scan["visited_leaf_ids"],
            }
            break

    sets_equal = baseline_counter == index_counter
    order_equal = baseline_ids == index_ids
    consistent = sets_equal and order_equal
    baseline_by_id = {record.write_seq: record for record in baseline_list}
    index_records_by_id = {
        record["write_seq"]: record for record in index_scan["hits"]
    }
    return {
        "index": index_name,
        "query_id": query.query_id,
        "query_kind": query.kind,
        "consistent": consistent,
        "set_consistent": sets_equal,
        "order_consistent": order_equal,
        "baseline_record_ids": baseline_ids,
        "index_record_ids": index_ids,
        "missing_records": [
            baseline_by_id[record_id].to_dict()
            for record_id in missing_ids
            if record_id in baseline_by_id
        ],
        "extra_records": [
            index_records_by_id[record_id]
            for record_id in extra_ids
            if record_id in index_records_by_id
        ],
        "duplicate_index_record_ids": duplicate_ids,
        "duplicate_baseline_record_ids": duplicate_baseline_ids,
        "count_differences": _counter_details(baseline_counter, index_counter),
        "first_divergence": first_divergence,
        "visited_leaf_ids": index_scan["visited_leaf_ids"],
    }


def compare_query_runs(
    *,
    index_name: str,
    query: QuerySpec,
    left_label: str,
    left_scan: dict[str, Any],
    right_label: str,
    right_scan: dict[str, Any],
) -> dict[str, Any]:
    left_ids = list(left_scan["hit_record_ids"])
    right_ids = list(right_scan["hit_record_ids"])
    first_divergence: dict[str, Any] | None = None
    for position in range(max(len(left_ids), len(right_ids))):
        left_id = left_ids[position] if position < len(left_ids) else None
        right_id = right_ids[position] if position < len(right_ids) else None
        if left_id != right_id:
            first_divergence = {
                "position": position,
                "left_record_id": left_id,
                "right_record_id": right_id,
                "left_visited_leaf_ids": left_scan["visited_leaf_ids"],
                "right_visited_leaf_ids": right_scan["visited_leaf_ids"],
            }
            break
    return {
        "index": index_name,
        "query_id": query.query_id,
        "query_kind": query.kind,
        "left_path": left_label,
        "right_path": right_label,
        "consistent": left_ids == right_ids,
        "left_record_ids": left_ids,
        "right_record_ids": right_ids,
        "first_divergence": first_divergence,
        "left_visited_leaf_ids": left_scan["visited_leaf_ids"],
        "right_visited_leaf_ids": right_scan["visited_leaf_ids"],
    }


def compare_tree_structures(
    left: BPlusTree,
    right: BPlusTree,
) -> dict[str, Any]:
    left_pages = {page["page_id"]: page for page in left.page_report()}
    right_pages = {page["page_id"]: page for page in right.page_report()}
    page_ids = sorted(set(left_pages) | set(right_pages))
    page_differences: list[dict[str, Any]] = []
    compared_fields = (
        "level",
        "parent_id",
        "keys",
        "record_ids",
        "children",
        "prev_leaf",
        "next_leaf",
    )
    for page_id in page_ids:
        left_page = left_pages.get(page_id)
        right_page = right_pages.get(page_id)
        if left_page is None or right_page is None:
            page_differences.append(
                {
                    "page_id": page_id,
                    "type": "missing_page",
                    "missing_on": "left" if left_page is None else "right",
                }
            )
            continue
        changed_fields = [
            field
            for field in compared_fields
            if left_page.get(field) != right_page.get(field)
        ]
        if changed_fields:
            page_differences.append(
                {
                    "page_id": page_id,
                    "type": "page_content_differs",
                    "changed_fields": changed_fields,
                }
            )
    left_record_ids = sorted(left.records)
    right_record_ids = sorted(right.records)
    record_id_differences = {
        "only_left": sorted(set(left_record_ids) - set(right_record_ids)),
        "only_right": sorted(set(right_record_ids) - set(left_record_ids)),
    }
    return {
        "identical": left.fingerprint() == right.fingerprint(),
        "fingerprints": {
            "left": left.fingerprint(),
            "right": right.fingerprint(),
        },
        "left_split_count": left.split_count,
        "right_split_count": right.split_count,
        "page_difference_count": len(page_differences),
        "page_differences": page_differences,
        "first_page_difference": page_differences[0] if page_differences else None,
        "record_id_differences": record_id_differences,
        "left_leaf_chain_connected": left.validate_structure()[
            "leaf_chain_connected"
        ],
        "right_leaf_chain_connected": right.validate_structure()[
            "leaf_chain_connected"
        ],
    }
