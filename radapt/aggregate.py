from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Iterable

import numpy as np

from .errors import ErrorCode, AggregateMismatchError
from .generator import BatchEnvelope
from .model import TableSpec, WindowSpec


def encode_jsonable(value: Any) -> Any:
    if isinstance(value, Fraction):
        return {"__frac__": True, "num": value.numerator, "den": value.denominator}
    if isinstance(value, dict):
        return {str(k): encode_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def decode_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("__frac__") is True:
            return Fraction(int(value["num"]), int(value["den"]))
        return {k: decode_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_jsonable(v) for v in value]
    return value


def dumps_json(value: Any) -> str:
    return json.dumps(encode_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_scalar(value: Any, col_type: str | None = None, decimal_scale: int = 2) -> Any:
    if isinstance(value, np.integer):
        value = int(value)
    elif isinstance(value, np.floating):
        value = float(value)
    elif isinstance(value, np.bool_):
        value = bool(value)
    if col_type == "decimal":
        return Fraction(int(value), 10**decimal_scale)
    if col_type in {"double", "float"}:
        return Fraction(Decimal(str(float(value))))
    if col_type in {"int", "bigint", "smallint", "timestamp"}:
        return int(value)
    if col_type == "boolean":
        return bool(value)
    return value


@dataclass
class MetricState:
    op: str
    count: int = 0
    sum: Fraction = Fraction(0)
    min: Any = None
    max: Any = None

    def add(self, value: Any, *, count_column: bool = False) -> None:
        self.count += 1
        if self.op == "count":
            return
        numeric = value if isinstance(value, Fraction) else Fraction(value)
        if self.op in {"sum", "avg"}:
            self.sum += numeric
        if self.op in {"min", "max"}:
            if self.min is None or value < self.min:
                self.min = value
            if self.max is None or value > self.max:
                self.max = value

    def result(self) -> Any:
        if self.op == "count":
            return self.count
        if self.op == "sum":
            return self.sum
        if self.op == "avg":
            return self.sum / self.count if self.count else None
        if self.op == "min":
            return self.min
        if self.op == "max":
            return self.max
        raise ValueError(f"unsupported op {self.op}")

    def to_dict(self) -> dict[str, Any]:
        return encode_jsonable(
            {"op": self.op, "count": self.count, "sum": self.sum, "min": self.min, "max": self.max}
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MetricState":
        data = decode_jsonable(raw)
        return cls(op=data["op"], count=data.get("count", 0), sum=data.get("sum", Fraction(0)), min=data.get("min"), max=data.get("max"))


@dataclass
class GroupState:
    key: tuple[Any, ...]
    metrics: dict[str, MetricState] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"key": list(self.key), "metrics": {alias: metric.to_dict() for alias, metric in self.metrics.items()}}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GroupState":
        return cls(
            key=tuple(decode_jsonable(raw["key"])),
            metrics={alias: MetricState.from_dict(item) for alias, item in raw["metrics"].items()},
        )


class WindowAggregate:
    def __init__(self, window: WindowSpec) -> None:
        self.window = window
        self.rows = 0
        self.groups: dict[str, GroupState] = {}

    def _key_for_row(self, columns: dict[str, np.ndarray], row_index: int, table: TableSpec) -> tuple[Any, ...]:
        key: list[Any] = []
        for name in self.window.group_by:
            col = table.column(name)
            key.append(normalize_scalar(columns[name][row_index], col.type, col.decimal_scale))
        return tuple(key)

    def _ensure_group(self, key: tuple[Any, ...]) -> GroupState:
        key_json = dumps_json(list(key))
        group = self.groups.get(key_json)
        if group is None:
            metrics = {agg.alias: MetricState(agg.op) for agg in self.window.aggregations}
            group = GroupState(key=key, metrics=metrics)
            self.groups[key_json] = group
        return group

    def apply_row(self, key: tuple[Any, ...], values: dict[str, Any]) -> None:
        self.rows += 1
        group = self._ensure_group(key)
        for agg in self.window.aggregations:
            metric = group.metrics[agg.alias]
            if agg.op == "count":
                metric.add(None)
            else:
                value = values[agg.column or ""]
                if value is not None:
                    metric.add(value)

    def add_batch(
        self,
        envelope: BatchEnvelope,
        table: TableSpec,
        *,
        operator: str | None = None,
        omit_first_row: bool = False,
    ) -> "ExecutionCounters":
        operator = operator or self.window.operator
        row_count = envelope.spec.row_count
        indices = list(range(row_count))
        sort_comparisons = 0
        if operator == "sort":
            indices.sort(key=lambda i: dumps_json(list(self._key_for_row(envelope.columns, i, table))))
            sort_comparisons = row_count * max(1, math.ceil(math.log2(row_count + 1)))
        hash_probes = 0
        aggregate_updates = 0
        started = 1 if omit_first_row else 0
        for row_index in indices[started:]:
            key = self._key_for_row(envelope.columns, row_index, table)
            hash_probes += 1
            values: dict[str, Any] = {}
            for agg in self.window.aggregations:
                if agg.column:
                    col = table.column(agg.column)
                    values[agg.column] = normalize_scalar(
                        envelope.columns[agg.column][row_index], col.type, col.decimal_scale
                    )
            before_groups = len(self.groups)
            self.apply_row(key, values)
            aggregate_updates += len(self.window.aggregations)
            if operator == "hash" and len(self.groups) == before_groups:
                hash_probes += 1
        return ExecutionCounters(
            rows_scanned=row_count - (1 if omit_first_row else 0),
            hash_probes=hash_probes,
            aggregate_updates=aggregate_updates,
            sort_comparisons_estimate=sort_comparisons,
            output_groups=len(self.groups),
        )

    def canonical(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key_json, group in self.groups.items():
            result[key_json] = {alias: metric.result() for alias, metric in group.metrics.items()}
            result[key_json]["__rows__"] = sum(metric.count for metric in group.metrics.values() if metric.op == "count") or None
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window.window_id,
            "group_by": list(self.window.group_by),
            "rows": self.rows,
            "groups": {key: group.to_dict() for key, group in self.groups.items()},
        }

    def dumps(self) -> str:
        return dumps_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any], window: WindowSpec) -> "WindowAggregate":
        data = decode_jsonable(raw if isinstance(raw, dict) else json.loads(raw))
        obj = cls(window)
        obj.rows = int(data["rows"])
        obj.groups = {key: GroupState.from_dict(group_raw) for key, group_raw in data.get("groups", {}).items()}
        return obj


@dataclass(frozen=True)
class ExecutionCounters:
    rows_scanned: int
    hash_probes: int
    aggregate_updates: int
    sort_comparisons_estimate: int
    output_groups: int


def logical_elapsed_ms(operator: str, row_count: int, groups: int) -> Fraction:
    row_count = max(0, row_count)
    groups = max(0, groups)
    if operator == "hash":
        work_units = row_count * 4 + groups * 2
    elif operator == "sort":
        comparisons = row_count * max(1, math.ceil(math.log2(row_count + 1))) if row_count else 0
        work_units = row_count * 2 + comparisons + groups * 2
    else:
        raise ValueError(f"unsupported operator {operator}")
    return Fraction(work_units, 100)


def estimate_logical_ms(operator: str, row_count: int, estimated_groups: int) -> Fraction:
    return logical_elapsed_ms(operator, row_count, estimated_groups)


def estimate_memory_bytes(table: TableSpec, row_count: int, estimated_groups: int, aggregations: int) -> int:
    widths = {
        "int": 4,
        "smallint": 2,
        "bigint": 8,
        "timestamp": 8,
        "decimal": 8,
        "double": 8,
        "float": 8,
        "boolean": 1,
        "string": 64,
    }
    column_bytes = sum(row_count * widths.get(col.type, 64) for col in table.columns)
    row_overhead = row_count * 32
    group_bytes = estimated_groups * (96 + 64 * max(1, len(table.columns)))
    agg_bytes = estimated_groups * aggregations * 48
    return column_bytes + row_overhead + group_bytes + agg_bytes


class ExactTruthPath:
    """Single-window, row-at-a-time exact aggregation reference."""

    def __init__(self, window: WindowSpec, table: TableSpec) -> None:
        self.window = window
        self.table = table
        self.aggregate = WindowAggregate(window)

    def consume_envelope(self, envelope: BatchEnvelope) -> None:
        for row_index in range(envelope.spec.row_count):
            key: list[Any] = []
            values: dict[str, Any] = {}
            for name in self.window.group_by:
                col = self.table.column(name)
                key.append(normalize_scalar(envelope.columns[name][row_index], col.type, col.decimal_scale))
            for agg in self.window.aggregations:
                if agg.column:
                    col = self.table.column(agg.column)
                    values[agg.column] = normalize_scalar(
                        envelope.columns[agg.column][row_index], col.type, col.decimal_scale
                    )
            self.aggregate.apply_row(tuple(key), values)

    def canonical(self) -> dict[str, dict[str, Any]]:
        return self.aggregate.canonical()


def canonical_window_result(aggregate: WindowAggregate) -> dict[str, Any]:
    return {
        "window_id": aggregate.window.window_id,
        "group_by": list(aggregate.window.group_by),
        "rows": aggregate.rows,
        "groups": [
            {"key": encode_jsonable(group.key), "metrics": encode_jsonable({alias: metric.result() for alias, metric in group.metrics.items()})}
            for group in sorted(aggregate.groups.values(), key=lambda group: dumps_json(list(group.key)))
        ],
    }


def find_first_divergence(
    window: WindowSpec,
    table: TableSpec,
    envelopes: Iterable[BatchEnvelope],
    *,
    stream_operator: str | None = None,
    omit_row_predicate: Callable[[int], bool] | None = None,
) -> dict[str, Any] | None:
    truth = ExactTruthPath(window, table)
    stream = WindowAggregate(window)
    for envelope in sorted(envelopes, key=lambda item: item.spec.seq):
        truth.consume_envelope(envelope)
        omit = bool(omit_row_predicate and omit_row_predicate(envelope.spec.seq))
        stream.add_batch(envelope, table, operator=stream_operator, omit_first_row=omit)
        expected = truth.canonical()
        actual = stream.canonical()
        if expected != actual:
            first_key = next((key for key in sorted(set(expected) | set(actual)) if expected.get(key) != actual.get(key)), None)
            return {
                "code": ErrorCode.AGGREGATE_MISMATCH,
                "window_id": window.window_id,
                "first_divergent_batch": envelope.spec.seq,
                "summary_digest": envelope.summary.digest,
                "batch_row_count": envelope.spec.row_count,
                "key": decode_jsonable(json.loads(first_key)) if first_key else None,
                "expected": expected.get(first_key) if first_key else None,
                "actual": actual.get(first_key) if first_key else None,
                "truth_rows": truth.aggregate.rows,
                "stream_rows": stream.rows,
                "truth_group_count": len(expected),
                "stream_group_count": len(actual),
                "intermediate_values": {
                    "batch_seqs_seen": [item.spec.seq for item in sorted(envelopes, key=lambda x: x.spec.seq) if item.spec.seq <= envelope.spec.seq],
                    "first_diff_key_json": first_key,
                },
            }
    return None


def assert_matches_truth(
    window: WindowSpec,
    table: TableSpec,
    envelopes: Iterable[BatchEnvelope],
    actual: WindowAggregate | dict[str, dict[str, Any]],
) -> None:
    envelopes = tuple(envelopes)
    truth = ExactTruthPath(window, table)
    for envelope in envelopes:
        truth.consume_envelope(envelope)
    expected = truth.canonical()
    actual_map = actual.canonical() if isinstance(actual, WindowAggregate) else actual
    if expected != actual_map:
        first_key = next((key for key in sorted(set(expected) | set(actual_map)) if expected.get(key) != actual_map.get(key)), None)
        first_seq = min((env.spec.seq for env in envelopes), default=None)
        for env in sorted(envelopes, key=lambda item: item.spec.seq):
            pass
        raise AggregateMismatchError(
            ErrorCode.AGGREGATE_MISMATCH,
            f"window {window.window_id!r} aggregate does not match exact truth",
            {
                "window_id": window.window_id,
                "first_divergent_batch": first_seq,
                "key": decode_jsonable(json.loads(first_key)) if first_key else None,
                "expected": expected.get(first_key) if first_key else None,
                "actual": actual_map.get(first_key) if first_key else None,
            },
        )
