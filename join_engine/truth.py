from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

from .canonical import canonical_bytes, filter_passes, result_order_key
from .generator import generate_table_rows
from .models import QuerySpec, TableSpec
from .partitioning import partition_for_key
from .result_rows import left_only_row, matched_row, right_only_row


def compute_truth(
    *,
    left_table: TableSpec,
    right_table: TableSpec,
    left_rows: int,
    right_rows: int,
    seed: int,
    query: QuerySpec,
    partitions: int,
) -> dict[str, Any]:
    left = list(generate_table_rows(left_table, seed, rows_override=left_rows))
    right = list(generate_table_rows(right_table, seed, rows_override=right_rows))
    matched: list[dict[str, Any]] = []
    left_matched: set[int] = set()
    right_matched: set[int] = set()
    comparisons = 0
    for left_row in left:
        left_key = left_row["values"][query.left_key]
        for right_row in right:
            comparisons += 1
            right_key = right_row["values"][query.right_key]
            if left_key is None or right_key is None or left_key != right_key:
                continue
            if not filter_passes(
                query.query_id,
                int(left_row["row_id"]),
                int(right_row["row_id"]),
                query.filter_selectivity,
            ):
                continue
            partition = partition_for_key(left_key, partitions)
            matched.append(matched_row(left_row, right_row, partition))
            left_matched.add(int(left_row["row_id"]))
            right_matched.add(int(right_row["row_id"]))
    rows = list(matched)
    if query.join_type == "left":
        for left_row in left:
            if int(left_row["row_id"]) in left_matched:
                continue
            key = left_row["values"][query.left_key]
            rows.append(
                left_only_row(left_row, partition_for_key(key, partitions))
            )
    elif query.join_type == "right":
        for right_row in right:
            if int(right_row["row_id"]) in right_matched:
                continue
            key = right_row["values"][query.right_key]
            rows.append(
                right_only_row(right_row, partition_for_key(key, partitions))
            )
    rows.sort(key=result_order_key)
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_bytes(row))
        digest.update(b"\n")
    return {
        "rows": rows,
        "row_count": len(rows),
        "digest": digest.hexdigest(),
        "comparisons": comparisons,
        "left_rows": left_rows,
        "right_rows": right_rows,
        "source": {
            "kind": "fixed_order_nested_loop",
            "left_major": True,
            "residual_filter": "deterministic sha-based ON filter",
            "null_key_rule": "null never matches; outer preserved side emits with nulls",
            "duplicate_key_rule": "all filter-passing duplicate pairs are emitted",
        },
    }


def compare_truth(
    result_rows: Iterable[dict[str, Any]],
    truth: dict[str, Any],
    *,
    tolerance_relative: float = 0.0,
    tolerance_absolute: float = 0.0,
) -> dict[str, Any]:
    result_list = list(result_rows)
    truth_list = truth["rows"]
    mismatch_count = 0
    first_difference = None
    for index in range(max(len(result_list), len(truth_list))):
        result_row = result_list[index] if index < len(result_list) else None
        truth_row = truth_list[index] if index < len(truth_list) else None
        if _tolerant_equal(
            result_row,
            truth_row,
            relative=tolerance_relative,
            absolute=tolerance_absolute,
        ):
            continue
        mismatch_count += 1
        if first_difference is None:
            first_difference = {
                "row_offset": index,
                "result": result_row,
                "truth": truth_row,
                "result_source_partitions": (
                    result_row.get("source_partitions") if result_row else None
                ),
                "truth_source_partitions": (
                    truth_row.get("source_partitions") if truth_row else None
                ),
            }
    return {
        "ok": mismatch_count == 0,
        "mismatch_count": mismatch_count,
        "first_difference": first_difference,
        "result_rows": len(result_list),
        "truth_rows": len(truth_list),
        "truth_digest": truth["digest"],
        "truth_source": truth["source"],
    }


def _tolerant_equal(
    left: Any,
    right: Any,
    *,
    relative: float,
    absolute: float,
) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, float) or isinstance(right, float):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        return math.isclose(
            float(left),
            float(right),
            rel_tol=relative,
            abs_tol=absolute,
        )
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(
            _tolerant_equal(
                left[key], right[key], relative=relative, absolute=absolute
            )
            for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _tolerant_equal(a, b, relative=relative, absolute=absolute)
            for a, b in zip(left, right)
        )
    return left == right
