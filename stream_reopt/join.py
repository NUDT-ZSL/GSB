from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import JoinPredicate, Shape, TableSpec
from .precision import row_sort_key, value_sort_key


@dataclass
class RuntimeData:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def append_batch(self, table: str, columns: dict[str, Any], row_count: int) -> None:
        target = self.tables.setdefault(table, [])
        names = sorted(columns)
        for index in range(row_count):
            target.append({f"{table}.{name}": columns[name][index] for name in names})

    def row_count(self, table: str) -> int:
        return len(self.tables.get(table, []))

    def counts(self, table_names: Iterable[str]) -> dict[str, int]:
        return {name: self.row_count(name) for name in table_names}

    def clone(self) -> "RuntimeData":
        return RuntimeData(tables={name: list(rows) for name, rows in self.tables.items()})


@dataclass(frozen=True)
class Condition:
    left_key: str
    right_key: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.left_key, self.right_key)


def canonicalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=row_sort_key)


def _encode_value(value: Any) -> bytes:
    if isinstance(value, bool):
        return b"b" + (b"1" if value else b"0")
    if isinstance(value, int):
        return b"i" + struct.pack(">q", value)
    if isinstance(value, float):
        return b"f" + struct.pack(">d", value)
    encoded = str(value).encode("utf-8")
    return b"s" + struct.pack(">I", len(encoded)) + encoded


def rows_digest(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"SRJ1")
    digest.update(struct.pack(">Q", len(rows)))
    for row in rows:
        digest.update(struct.pack(">I", len(row)))
        for name in sorted(row):
            name_bytes = name.encode("utf-8")
            digest.update(struct.pack(">I", len(name_bytes)))
            digest.update(name_bytes)
            digest.update(_encode_value(row[name]))
    return digest.hexdigest()


def _conditions_for(
    table: str,
    joined_tables: set[str],
    predicates: tuple[JoinPredicate, ...],
) -> tuple[Condition, ...]:
    conditions: list[Condition] = []
    for predicate in predicates:
        if predicate.left_table == table and predicate.right_table in joined_tables:
            conditions.append(
                Condition(
                    left_key=f"{predicate.right_table}.{predicate.right_column}",
                    right_key=f"{predicate.left_table}.{predicate.left_column}",
                )
            )
        elif predicate.right_table == table and predicate.left_table in joined_tables:
            conditions.append(
                Condition(
                    left_key=f"{predicate.left_table}.{predicate.left_column}",
                    right_key=f"{predicate.right_table}.{predicate.right_column}",
                )
            )
    return tuple(conditions)


def shape_connected(shape: Shape, predicates: tuple[JoinPredicate, ...]) -> bool:
    joined = {shape.join_order[0]}
    for table in shape.join_order[1:]:
        if not _conditions_for(table, joined, predicates):
            return False
        joined.add(table)
    return True


def _matches(left_row: dict[str, Any], right_row: dict[str, Any], conditions: tuple[Condition, ...]) -> bool:
    return all(left_row[condition.left_key] == right_row[condition.right_key] for condition in conditions)


def _merge(left_row: dict[str, Any], right_row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left_row)
    merged.update(right_row)
    return merged


def _hash_join(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    conditions: tuple[Condition, ...],
) -> list[dict[str, Any]]:
    first = conditions[0]
    index: dict[Any, list[dict[str, Any]]] = {}
    for row in right_rows:
        index.setdefault(row[first.right_key], []).append(row)
    result: list[dict[str, Any]] = []
    for left_row in left_rows:
        for right_row in index.get(left_row[first.left_key], ()):
            if _matches(left_row, right_row, conditions):
                result.append(_merge(left_row, right_row))
    return result


def _nested_loop_join(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    conditions: tuple[Condition, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for left_row in left_rows:
        for right_row in right_rows:
            if _matches(left_row, right_row, conditions):
                result.append(_merge(left_row, right_row))
    return result


def _sort_merge_join(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    conditions: tuple[Condition, ...],
) -> list[dict[str, Any]]:
    first = conditions[0]
    sorted_left = sorted(left_rows, key=lambda row: value_sort_key(row[first.left_key]))
    sorted_right = sorted(right_rows, key=lambda row: value_sort_key(row[first.right_key]))
    result: list[dict[str, Any]] = []
    left_index = 0
    right_index = 0
    while left_index < len(sorted_left) and right_index < len(sorted_right):
        left_key = value_sort_key(sorted_left[left_index][first.left_key])
        right_key = value_sort_key(sorted_right[right_index][first.right_key])
        if left_key < right_key:
            left_index += 1
            continue
        if right_key < left_key:
            right_index += 1
            continue
        left_end = left_index + 1
        while left_end < len(sorted_left) and value_sort_key(
            sorted_left[left_end][first.left_key]
        ) == left_key:
            left_end += 1
        right_end = right_index + 1
        while right_end < len(sorted_right) and value_sort_key(
            sorted_right[right_end][first.right_key]
        ) == right_key:
            right_end += 1
        for left_row in sorted_left[left_index:left_end]:
            for right_row in sorted_right[right_index:right_end]:
                if _matches(left_row, right_row, conditions):
                    result.append(_merge(left_row, right_row))
        left_index = left_end
        right_index = right_end
    return result


def _join_by_algorithm(
    algorithm: str,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    conditions: tuple[Condition, ...],
) -> list[dict[str, Any]]:
    if algorithm == "hash":
        return _hash_join(left_rows, right_rows, conditions)
    if algorithm == "nested_loop":
        return _nested_loop_join(left_rows, right_rows, conditions)
    if algorithm == "sort_merge":
        return _sort_merge_join(left_rows, right_rows, conditions)
    raise ValueError(f"unknown algorithm: {algorithm}")


def execute_shape(
    data: RuntimeData,
    shape: Shape,
    predicates: tuple[JoinPredicate, ...],
    canonical: bool = True,
) -> list[dict[str, Any]]:
    first_table = shape.join_order[0]
    rows = [dict(row) for row in data.tables.get(first_table, [])]
    joined_tables = {first_table}
    for index, table in enumerate(shape.join_order[1:], start=1):
        conditions = _conditions_for(table, joined_tables, predicates)
        if not conditions:
            raise ValueError(f"shape is not connected at table {table}")
        right_rows = [dict(row) for row in data.tables.get(table, [])]
        rows = _join_by_algorithm(shape.algorithms[index - 1], rows, right_rows, conditions)
        joined_tables.add(table)
        if not rows:
            break
    return canonicalize_rows(rows) if canonical else rows


def table_key_stats(
    rows: list[dict[str, Any]],
    key: str,
) -> tuple[dict[Any, int], int, float]:
    counts: dict[Any, int] = {}
    for row in rows:
        value = row[key]
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return counts, 0, 0.0
    maximum = max(counts.values())
    return counts, len(counts), maximum / len(rows)


def pair_join_count(
    left_rows: list[dict[str, Any]],
    left_key: str,
    right_rows: list[dict[str, Any]],
    right_key: str,
) -> int:
    left_counts, _, _ = table_key_stats(left_rows, left_key)
    right_counts, _, _ = table_key_stats(right_rows, right_key)
    if len(left_counts) > len(right_counts):
        left_counts, right_counts = right_counts, left_counts
    total = 0
    for key, count in left_counts.items():
        total += count * right_counts.get(key, 0)
    return total


def log2_cost(value: int) -> float:
    return value * math.log2(value + 1)
