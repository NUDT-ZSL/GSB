"""Validated immutable domain models for the adaptive join runtime."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .canonical import digest_value, load_json
from .errors import BusinessError, ErrorCode

_INTEGER_TYPES = {"int", "integer", "int32", "int64", "bigint", "long"}
_FLOAT_TYPES = {"float", "float32", "float64", "double", "real"}
_STRING_TYPES = {"str", "string", "text", "varchar", "char"}
_NUMERIC_TYPES = _INTEGER_TYPES | _FLOAT_TYPES
_ALL_TYPES = _NUMERIC_TYPES | _STRING_TYPES
_ALGORITHMS = {"hash": "hash", "sort_merge": "sort_merge", "merge": "sort_merge", "nested_loop": "nested_loop"}
_KEY_SEMANTICS = {
    "primary_key",
    "foreign_key",
    "join_key",
    "attribute",
    "measure",
    "dimension",
    "none",
}


def _fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, details=details)


def _path(parent: str, key: str | int) -> str:
    return f"{parent}.{key}" if parent else str(key)


def _require(mapping: Mapping[str, Any], key: str, code: str, parent: str) -> Any:
    if key not in mapping:
        _fail(code, f"missing required field {_path(parent, key)}", field=_path(parent, key))
    return mapping[key]


def _as_str(value: Any, code: str, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(code, f"{path} must be a non-empty string", field=path)
    return value.strip()


def _as_int(value: Any, code: str, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code, f"{path} must be an integer", field=path, actual_type=type(value).__name__)
    if minimum is not None and value < minimum:
        _fail(code, f"{path} must be >= {minimum}", field=path, value=value)
    return value


def _as_number(
    value: Any,
    code: str,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_minimum: bool = False,
    strict_maximum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code, f"{path} must be numeric", field=path, actual_type=type(value).__name__)
    number = float(value)
    if not math.isfinite(number):
        _fail(code, f"{path} must be finite", field=path, value=value)
    if minimum is not None:
        if strict_minimum and number <= minimum:
            _fail(code, f"{path} must be > {minimum}", field=path, value=number)
        if not strict_minimum and number < minimum:
            _fail(code, f"{path} must be >= {minimum}", field=path, value=number)
    if maximum is not None:
        if strict_maximum and number >= maximum:
            _fail(code, f"{path} must be < {maximum}", field=path, value=number)
        if not strict_maximum and number > maximum:
            _fail(code, f"{path} must be <= {maximum}", field=path, value=number)
    return number


def _iter_mapping(value: Any, code: str, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code, f"{path} must be an object", field=path, actual_type=type(value).__name__)
    return value


@dataclass(frozen=True)
class SkewSpec:
    ratio: float
    hot_fraction: float
    start_ratio: float | None = None
    end_ratio: float | None = None
    hot_values: tuple[int | str, ...] = ()

    def ratio_for_progress(self, progress: float) -> float:
        if self.start_ratio is None and self.end_ratio is None:
            return self.ratio
        start = self.ratio if self.start_ratio is None else self.start_ratio
        end = self.ratio if self.end_ratio is None else self.end_ratio
        return start + (end - start) * max(0.0, min(1.0, progress))


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: str
    type_class: str
    ndv: int
    width: int
    key_semantics: str
    skew: SkewSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "dtype": self.dtype,
            "type_class": self.type_class,
            "ndv": self.ndv,
            "width": self.width,
            "key_semantics": self.key_semantics,
        }
        if self.skew is not None:
            value["skew"] = {
                "ratio": self.skew.ratio,
                "hot_fraction": self.skew.hot_fraction,
                "start_ratio": self.skew.start_ratio,
                "end_ratio": self.skew.end_ratio,
                "hot_values": list(self.skew.hot_values),
            }
        return value


@dataclass(frozen=True)
class TableSpec:
    name: str
    rows: int
    columns: tuple[ColumnSpec, ...]

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        return {column.name: column for column in self.columns}

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rows": self.rows, "columns": [c.to_dict() for c in self.columns]}


@dataclass(frozen=True)
class JoinPredicate:
    id: str
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    operator: str
    left_type: str
    right_type: str

    @property
    def source_table(self) -> str:
        return self.left_table

    @property
    def source_key(self) -> str:
        return self.left_column

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "left_table": self.left_table,
            "left_column": self.left_column,
            "right_table": self.right_table,
            "right_column": self.right_column,
            "operator": self.operator,
            "left_type": self.left_type,
            "right_type": self.right_type,
        }


@dataclass(frozen=True)
class BatchSpec:
    batch_id: str
    arrival_seq: int
    rows: tuple[tuple[str, int], ...]

    @property
    def row_map(self) -> dict[str, int]:
        return dict(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "arrival_seq": self.arrival_seq,
            "rows": dict(self.rows),
        }


@dataclass(frozen=True)
class Plan:
    plan_id: str
    seed: int
    predicates: tuple[JoinPredicate, ...]
    table_order: tuple[str, ...]
    batches: tuple[BatchSpec, ...]
    initial_order: tuple[str, ...]
    initial_algorithms: tuple[str, ...]

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    @property
    def batch_by_id(self) -> dict[str, BatchSpec]:
        return {batch.batch_id: batch for batch in self.batches}

    @property
    def batch_by_seq(self) -> dict[int, BatchSpec]:
        return {batch.arrival_seq: batch for batch in self.batches}

    def predicate_map(self) -> dict[str, JoinPredicate]:
        return {predicate.id: predicate for predicate in self.predicates}

    def predicates_for_step(self, joined: Iterable[str], next_table: str) -> list[JoinPredicate]:
        joined_set = set(joined)
        return [
            p
            for p in self.predicates
            if (p.left_table in joined_set and p.right_table == next_table)
            or (p.right_table in joined_set and p.left_table == next_table)
        ]

    def valid_order(self, order: Iterable[str]) -> bool:
        names = list(order)
        if len(names) != len(self.table_order) or set(names) != set(self.table_order):
            return False
        joined: list[str] = []
        for name in names:
            if not joined:
                joined.append(name)
                continue
            if not self.predicates_for_step(joined, name):
                return False
            joined.append(name)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "seed": self.seed,
            "initial_order": list(self.initial_order),
            "initial_algorithms": list(self.initial_algorithms),
            "batch_order": [batch.batch_id for batch in self.batches],
        }


@dataclass(frozen=True)
class InitialStats:
    snapshot_id: str
    join_selectivities: tuple[tuple[str, float], ...]
    estimated_cardinalities: tuple[tuple[str, float], ...]
    table_cardinalities: tuple[tuple[str, float], ...]

    @property
    def selectivity_map(self) -> dict[str, float]:
        return dict(self.join_selectivities)

    @property
    def cardinality_map(self) -> dict[str, float]:
        return dict(self.estimated_cardinalities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "join_selectivities": dict(self.join_selectivities),
            "estimated_cardinalities": dict(self.estimated_cardinalities),
            "table_cardinalities": dict(self.table_cardinalities),
        }


@dataclass(frozen=True)
class ReoptConfig:
    drift_threshold: float
    cooldown_ms: int
    switch_cost_budget: float
    fixed_scale: int
    float_tolerance: float
    logical_clock_step_ms: int
    observation_window: int = 3
    switch_base_cost: float = 5.0
    max_candidates: int = 128
    enable_reopt: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "drift_threshold": self.drift_threshold,
            "cooldown_ms": self.cooldown_ms,
            "switch_cost_budget": self.switch_cost_budget,
            "fixed_scale": self.fixed_scale,
            "float_tolerance": self.float_tolerance,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "observation_window": self.observation_window,
            "switch_base_cost": self.switch_base_cost,
            "max_candidates": self.max_candidates,
            "enable_reopt": self.enable_reopt,
        }


@dataclass(frozen=True)
class ExecutionShape:
    version: int
    join_order: tuple[str, ...]
    algorithms: tuple[str, ...]
    reason: str = "initial"

    @property
    def content_digest(self) -> str:
        return digest_value(
            {
                "join_order": list(self.join_order),
                "algorithms": list(self.algorithms),
            }
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": self.version,
            "join_order": list(self.join_order),
            "algorithms": list(self.algorithms),
            "reason": self.reason,
        }
        if include_digest:
            value["content_digest"] = self.content_digest
        return value


@dataclass(frozen=True)
class Scenario:
    tables: tuple[TableSpec, ...]
    plan: Plan
    initial_stats: InitialStats
    config: ReoptConfig
    source_digest: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def table_map(self) -> dict[str, TableSpec]:
        return {table.name: table for table in self.tables}

    @property
    def initial_shape(self) -> ExecutionShape:
        return ExecutionShape(
            version=1,
            join_order=self.plan.initial_order,
            algorithms=self.plan.initial_algorithms,
            reason="initial",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": [table.to_dict() for table in self.tables],
            "plan": self.plan.to_dict(),
            "initial_stats": self.initial_stats.to_dict(),
            "config": self.config.to_dict(),
        }


def _parse_skew(raw: Any, table: TableSpec | None, column_name: str, row_count: int) -> SkewSpec | None:
    if raw is None or raw is False:
        return None
    code = ErrorCode.BAD_PLAN
    data = _iter_mapping(raw, code, f"skew[{column_name}]")
    ratio = _as_number(_require(data, "ratio", code, f"skew[{column_name}]"), code, f"skew[{column_name}].ratio", minimum=0.0, maximum=1.0)
    hot_fraction = _as_number(
        _require(data, "hot_fraction", code, f"skew[{column_name}]"),
        code,
        f"skew[{column_name}].hot_fraction",
        minimum=0.0,
        maximum=1.0,
        strict_minimum=True,
    )
    start_ratio = data.get("start_ratio")
    end_ratio = data.get("end_ratio")
    if start_ratio is not None:
        start_ratio = _as_number(start_ratio, code, f"skew[{column_name}].start_ratio", minimum=0.0, maximum=1.0)
    if end_ratio is not None:
        end_ratio = _as_number(end_ratio, code, f"skew[{column_name}].end_ratio", minimum=0.0, maximum=1.0)
    hot_values_raw = data.get("hot_values", [])
    if not isinstance(hot_values_raw, list):
        _fail(code, f"skew[{column_name}].hot_values must be a list", field=f"skew[{column_name}].hot_values")
    hot_values = tuple(hot_values_raw)
    return SkewSpec(
        ratio=ratio,
        hot_fraction=hot_fraction,
        start_ratio=start_ratio,
        end_ratio=end_ratio,
        hot_values=hot_values,
    )


def _parse_tables(raw_tables: Any) -> tuple[TableSpec, ...]:
    code = ErrorCode.BAD_PLAN
    if not isinstance(raw_tables, list) or not raw_tables:
        _fail(code, "tables must be a non-empty array", field="tables")
    tables: list[TableSpec] = []
    names: set[str] = set()
    for ti, raw_table in enumerate(raw_tables):
        path = f"tables[{ti}]"
        data = _iter_mapping(raw_table, code, path)
        name = _as_str(_require(data, "name", code, path), code, f"{path}.name")
        if name in names:
            _fail(code, f"duplicate table name: {name}", field=f"{path}.name", table=name)
        names.add(name)
        rows = _as_int(_require(data, "rows", code, path), code, f"{path}.rows", minimum=1)
        raw_columns = _require(data, "columns", code, path)
        if not isinstance(raw_columns, list) or not raw_columns:
            _fail(code, f"{path}.columns must be a non-empty array", field=f"{path}.columns")
        columns: list[ColumnSpec] = []
        column_names: set[str] = set()
        for ci, raw_column in enumerate(raw_columns):
            cpath = f"{path}.columns[{ci}]"
            cdata = _iter_mapping(raw_column, code, cpath)
            cname = _as_str(_require(cdata, "name", code, cpath), code, f"{cpath}.name")
            if cname in column_names:
                _fail(code, f"duplicate column {name}.{cname}", field=f"{cpath}.name")
            column_names.add(cname)
            dtype = _as_str(_require(cdata, "type", code, cpath), code, f"{cpath}.type").lower()
            if dtype not in _ALL_TYPES:
                _fail(code, f"unsupported column type {dtype!r}", field=f"{cpath}.type", column=f"{name}.{cname}")
            if dtype in _INTEGER_TYPES:
                type_class = "integer"
            elif dtype in _FLOAT_TYPES:
                type_class = "float"
            else:
                type_class = "string"
            ndv = _as_int(_require(cdata, "ndv", code, cpath), code, f"{cpath}.ndv", minimum=1)
            if ndv > rows:
                _fail(
                    code,
                    f"NDV must not exceed row count for {name}.{cname}",
                    field=f"{cpath}.ndv",
                    ndv=ndv,
                    rows=rows,
                )
            width = _as_int(_require(cdata, "width", code, cpath), code, f"{cpath}.width", minimum=1)
            key_semantics = str(cdata.get("key_semantics", "none")).lower()
            if key_semantics not in _KEY_SEMANTICS:
                _fail(
                    code,
                    f"unsupported key_semantics {key_semantics!r}",
                    field=f"{cpath}.key_semantics",
                    column=f"{name}.{cname}",
                )
            skew = _parse_skew(cdata.get("skew"), None, cname, rows)
            if skew is not None and len(skew.hot_values) > ndv:
                _fail(
                    code,
                    "skew.hot_values cannot contain more entries than NDV",
                    field=f"{cpath}.skew.hot_values",
                    ndv=ndv,
                    hot_values=len(skew.hot_values),
                )
            columns.append(
                ColumnSpec(
                    name=cname,
                    dtype=dtype,
                    type_class=type_class,
                    ndv=ndv,
                    width=width,
                    key_semantics=key_semantics,
                    skew=skew,
                )
            )
        tables.append(TableSpec(name=name, rows=rows, columns=tuple(columns)))
    return tuple(tables)


def _parse_predicates(raw_predicates: Any, table_map: Mapping[str, TableSpec]) -> tuple[JoinPredicate, ...]:
    code = ErrorCode.BAD_PLAN
    if not isinstance(raw_predicates, list) or not raw_predicates:
        _fail(code, "plan.predicates must be a non-empty array", field="plan.predicates")
    predicates: list[JoinPredicate] = []
    ids: set[str] = set()
    for pi, raw_predicate in enumerate(raw_predicates):
        path = f"plan.predicates[{pi}]"
        data = _iter_mapping(raw_predicate, code, path)
        predicate_id = _as_str(_require(data, "id", code, path), code, f"{path}.id")
        if predicate_id in ids:
            _fail(code, f"duplicate predicate id: {predicate_id}", field=f"{path}.id")
        ids.add(predicate_id)
        left_table = _as_str(_require(data, "left_table", code, path), code, f"{path}.left_table")
        right_table = _as_str(_require(data, "right_table", code, path), code, f"{path}.right_table")
        if left_table == right_table:
            _fail(code, f"predicate {predicate_id} must join two distinct tables", predicate=predicate_id)
        if left_table not in table_map or right_table not in table_map:
            _fail(
                code,
                f"predicate {predicate_id} references an unknown table",
                predicate=predicate_id,
                tables=[left_table, right_table],
            )
        left_column = _as_str(_require(data, "left_column", code, path), code, f"{path}.left_column")
        right_column = _as_str(_require(data, "right_column", code, path), code, f"{path}.right_column")
        left_columns = table_map[left_table].column_map
        right_columns = table_map[right_table].column_map
        if left_column not in left_columns or right_column not in right_columns:
            _fail(
                code,
                f"predicate {predicate_id} references an unknown join column",
                predicate=predicate_id,
                left=f"{left_table}.{left_column}",
                right=f"{right_table}.{right_column}",
            )
        left_type = left_columns[left_column].type_class
        right_type = right_columns[right_column].type_class
        comparable = left_type == right_type or {left_type, right_type} <= {"integer", "float"}
        if not comparable:
            _fail(
                ErrorCode.TYPE_MISMATCH,
                f"join columns {left_table}.{left_column} and {right_table}.{right_column} are not comparable",
                predicate=predicate_id,
                left_type=left_type,
                right_type=right_type,
            )
        operator = str(data.get("operator", "=")).strip()
        if operator not in {"=", "==", "!=", "<", "<=", ">", ">="}:
            _fail(code, f"unsupported join operator {operator!r}", field=f"{path}.operator")
        predicates.append(
            JoinPredicate(
                id=predicate_id,
                left_table=left_table,
                left_column=left_column,
                right_table=right_table,
                right_column=right_column,
                operator=operator,
                left_type=left_type,
                right_type=right_type,
            )
        )
    return tuple(predicates)


def _parse_batches(
    plan_data: Mapping[str, Any],
    tables: tuple[TableSpec, ...],
) -> tuple[BatchSpec, ...]:
    code = ErrorCode.BAD_PLAN
    table_names = tuple(table.name for table in tables)
    raw_batches = plan_data.get("batches")
    if raw_batches is None:
        order = plan_data.get("batch_order")
        sizes = plan_data.get("batch_rows", plan_data.get("batch_sizes"))
        if not isinstance(order, list) or not order:
            _fail(code, "plan.batches or plan.batch_order is required", field="plan.batches")
        if sizes is None:
            _fail(code, "plan.batch_rows is required when plan.batches is absent", field="plan.batch_rows")
        raw_batches = []
        for index, batch_id in enumerate(order):
            if isinstance(sizes, int):
                count = sizes
                rows = {name: count for name in table_names}
            elif isinstance(sizes, list):
                if index >= len(sizes):
                    _fail(code, "plan.batch_rows has fewer entries than batch_order", field="plan.batch_rows")
                value = sizes[index]
                if isinstance(value, int):
                    rows = {name: value for name in table_names}
                elif isinstance(value, Mapping):
                    rows = dict(value)
                else:
                    _fail(code, "each batch_rows entry must be an integer or object", field=f"plan.batch_rows[{index}]")
            elif isinstance(sizes, Mapping):
                rows = {}
                for name in table_names:
                    if name not in sizes:
                        _fail(code, f"plan.batch_rows lacks table {name}", field=f"plan.batch_rows.{name}")
                    table_sizes = sizes[name]
                    if isinstance(table_sizes, list):
                        if index >= len(table_sizes):
                            _fail(code, f"plan.batch_rows.{name} is too short", field=f"plan.batch_rows.{name}")
                        rows[name] = table_sizes[index]
                    elif isinstance(table_sizes, Mapping):
                        if str(batch_id) not in table_sizes:
                            _fail(code, f"plan.batch_rows.{name} lacks batch {batch_id}", field=f"plan.batch_rows.{name}")
                        rows[name] = table_sizes[str(batch_id)]
                    elif isinstance(table_sizes, int):
                        rows[name] = table_sizes
                    else:
                        _fail(code, f"invalid batch size for {name}", field=f"plan.batch_rows.{name}")
            else:
                _fail(code, "plan.batch_rows must be an integer, array, or object", field="plan.batch_rows")
            raw_batches.append({"batch_id": batch_id, "arrival_seq": index, "rows": rows})
    if not isinstance(raw_batches, list) or not raw_batches:
        _fail(code, "plan.batches must be a non-empty array", field="plan.batches")
    batches: list[BatchSpec] = []
    ids: set[str] = set()
    sequences: set[int] = set()
    for bi, raw_batch in enumerate(raw_batches):
        path = f"plan.batches[{bi}]"
        if isinstance(raw_batch, Mapping):
            data = raw_batch
            batch_id = _as_str(data.get("batch_id", data.get("id", f"b{bi:06d}")), code, f"{path}.batch_id")
            arrival_seq = _as_int(data.get("arrival_seq", data.get("sequence", bi)), code, f"{path}.arrival_seq", minimum=0)
            raw_rows = data.get("rows", data.get("batch_rows"))
            if raw_rows is None:
                _fail(code, f"missing rows for batch {batch_id}", field=f"{path}.rows")
            if isinstance(raw_rows, int):
                row_map = {name: raw_rows for name in table_names}
            elif isinstance(raw_rows, list):
                if len(raw_rows) != len(table_names):
                    _fail(code, f"{path}.rows array must align with tables", field=f"{path}.rows")
                row_map = dict(zip(table_names, raw_rows))
            elif isinstance(raw_rows, Mapping):
                row_map = dict(raw_rows)
            else:
                _fail(code, f"{path}.rows must be an integer, array, or object", field=f"{path}.rows")
        else:
            _fail(code, f"{path} must be an object", field=path)
        if batch_id in ids:
            _fail(code, f"duplicate batch id: {batch_id}", field=f"{path}.batch_id")
        if arrival_seq in sequences:
            _fail(code, f"duplicate batch arrival sequence: {arrival_seq}", field=f"{path}.arrival_seq")
        ids.add(batch_id)
        sequences.add(arrival_seq)
        if set(row_map) != set(table_names):
            missing = sorted(set(table_names) - set(row_map))
            extra = sorted(set(row_map) - set(table_names))
            _fail(
                code,
                f"batch {batch_id} rows must define every table exactly once",
                batch=batch_id,
                missing=missing,
                extra=extra,
            )
        normalized_rows: list[tuple[str, int]] = []
        for name in table_names:
            count = _as_int(row_map[name], code, f"{path}.rows.{name}", minimum=1)
            normalized_rows.append((name, count))
        batches.append(BatchSpec(batch_id=batch_id, arrival_seq=arrival_seq, rows=tuple(normalized_rows)))
    batches.sort(key=lambda batch: batch.arrival_seq)
    expected = list(range(len(batches)))
    actual = [batch.arrival_seq for batch in batches]
    if actual != expected:
        _fail(
            code,
            "batch arrival sequences must be contiguous and start at zero",
            expected=expected,
            actual=actual,
        )
    return tuple(batches)


def _parse_plan(
    raw_plan: Any,
    tables: tuple[TableSpec, ...],
) -> tuple[Plan, tuple[JoinPredicate, ...]]:
    code = ErrorCode.BAD_PLAN
    data = _iter_mapping(raw_plan, code, "plan")
    plan_id = _as_str(_require(data, "plan_id", code, "plan"), code, "plan.plan_id")
    seed = _as_int(_require(data, "seed", code, "plan"), code, "plan.seed", minimum=0)
    table_map = {table.name: table for table in tables}
    predicates = _parse_predicates(_require(data, "predicates", code, "plan"), table_map)
    raw_order = _require(data, "initial_join_order", code, "plan")
    if not isinstance(raw_order, list) or not all(isinstance(name, str) for name in raw_order):
        _fail(code, "plan.initial_join_order must be an array of table names", field="plan.initial_join_order")
    initial_order = tuple(raw_order)
    expected_tables = tuple(table.name for table in tables)
    if len(initial_order) != len(expected_tables) or set(initial_order) != set(expected_tables):
        _fail(
            code,
            "initial_join_order must contain every table exactly once",
            expected=list(expected_tables),
            actual=list(initial_order),
        )
    joined: list[str] = []
    for name in initial_order:
        if joined and not any(
            (p.left_table in joined and p.right_table == name) or (p.right_table in joined and p.left_table == name)
            for p in predicates
        ):
            _fail(
                code,
                f"join order is disconnected before table {name}",
                table=name,
                joined=joined,
            )
        joined.append(name)
    raw_algorithms = _require(data, "join_algorithms", code, "plan")
    if isinstance(raw_algorithms, Mapping):
        algorithms = []
        for index in range(len(initial_order) - 1):
            value = raw_algorithms.get(str(index), raw_algorithms.get(index))
            if value is None:
                _fail(code, f"missing join algorithm for step {index}", field="plan.join_algorithms")
            algorithms.append(value)
    elif isinstance(raw_algorithms, list):
        algorithms = list(raw_algorithms)
    else:
        _fail(code, "plan.join_algorithms must be an array or object", field="plan.join_algorithms")
    if len(algorithms) != len(initial_order) - 1:
        _fail(
            code,
            "join_algorithms must have exactly len(initial_join_order)-1 entries",
            expected=len(initial_order) - 1,
            actual=len(algorithms),
        )
    normalized_algorithms: list[str] = []
    for index, algorithm in enumerate(algorithms):
        if not isinstance(algorithm, str) or algorithm.lower() not in _ALGORITHMS:
            _fail(code, f"unsupported join algorithm at step {index}: {algorithm!r}", field=f"plan.join_algorithms[{index}]")
        normalized_algorithms.append(_ALGORITHMS[algorithm.lower()])
    batches = _parse_batches(data, tables)
    plan = Plan(
        plan_id=plan_id,
        seed=seed,
        predicates=predicates,
        table_order=expected_tables,
        batches=batches,
        initial_order=initial_order,
        initial_algorithms=tuple(normalized_algorithms),
    )
    return plan, predicates


def _parse_stats(
    raw_stats: Any,
    tables: tuple[TableSpec, ...],
    predicates: tuple[JoinPredicate, ...],
) -> InitialStats:
    code = ErrorCode.BAD_STATS
    data = _iter_mapping(raw_stats, code, "initial_stats")
    snapshot_id = str(data.get("snapshot_id", "initial"))
    raw_selectivities = data.get("join_selectivities", data.get("join_stats"))
    if raw_selectivities is None:
        _fail(code, "initial_stats.join_selectivities is required", field="initial_stats.join_selectivities")
    selectivity_values: dict[str, float] = {}
    cardinality_values: dict[str, float] = {}
    if isinstance(raw_selectivities, Mapping):
        for predicate_id, value in raw_selectivities.items():
            if isinstance(value, Mapping):
                selectivity = _as_number(
                    value.get("selectivity", value.get("estimated_selectivity")),
                    code,
                    f"initial_stats.join_selectivities.{predicate_id}.selectivity",
                    minimum=0.0,
                    maximum=1.0,
                    strict_minimum=True,
                    strict_maximum=True,
                )
                if "estimated_cardinality" in value:
                    cardinality_values[predicate_id] = _as_number(
                        value["estimated_cardinality"],
                        code,
                        f"initial_stats.join_selectivities.{predicate_id}.estimated_cardinality",
                        minimum=0.0,
                        strict_minimum=True,
                    )
            else:
                selectivity = _as_number(
                    value,
                    code,
                    f"initial_stats.join_selectivities.{predicate_id}",
                    minimum=0.0,
                    maximum=1.0,
                    strict_minimum=True,
                    strict_maximum=True,
                )
            selectivity_values[str(predicate_id)] = selectivity
    elif isinstance(raw_selectivities, list):
        for index, entry in enumerate(raw_selectivities):
            path = f"initial_stats.join_selectivities[{index}]"
            item = _iter_mapping(entry, code, path)
            predicate_id = _as_str(_require(item, "predicate_id", code, path), code, f"{path}.predicate_id")
            selectivity = _as_number(
                item.get("selectivity", item.get("estimated_selectivity")),
                code,
                f"{path}.selectivity",
                minimum=0.0,
                maximum=1.0,
                strict_minimum=True,
                strict_maximum=True,
            )
            selectivity_values[predicate_id] = selectivity
            if "estimated_cardinality" in item:
                cardinality_values[predicate_id] = _as_number(
                    item["estimated_cardinality"],
                    code,
                    f"{path}.estimated_cardinality",
                    minimum=0.0,
                    strict_minimum=True,
                )
    else:
        _fail(code, "initial_stats.join_selectivities must be an object or array", field="initial_stats.join_selectivities")
    predicate_ids = {predicate.id for predicate in predicates}
    missing = sorted(predicate_ids - set(selectivity_values))
    extra = sorted(set(selectivity_values) - predicate_ids)
    if missing or extra:
        _fail(
            code,
            "join selectivity snapshot must match plan predicates",
            missing=missing,
            extra=extra,
        )
    table_map = {table.name: table for table in tables}
    raw_table_cardinalities = data.get("table_cardinalities", {})
    table_cardinalities: dict[str, float] = {}
    if raw_table_cardinalities:
        card_map = _iter_mapping(raw_table_cardinalities, code, "initial_stats.table_cardinalities")
        for name, value in card_map.items():
            if name not in table_map:
                _fail(code, f"unknown table in table_cardinalities: {name}", field="initial_stats.table_cardinalities")
            cardinality = _as_number(value, code, f"initial_stats.table_cardinalities.{name}", minimum=1.0)
            if int(cardinality) != table_map[name].rows:
                _fail(
                    code,
                    f"table cardinality for {name} must equal table.rows",
                    table=name,
                    expected=table_map[name].rows,
                    actual=cardinality,
                )
            table_cardinalities[name] = cardinality
    for table in tables:
        table_cardinalities.setdefault(table.name, float(table.rows))
    for predicate in predicates:
        if predicate.id not in cardinality_values:
            left = table_map[predicate.left_table].rows
            right = table_map[predicate.right_table].rows
            cardinality_values[predicate.id] = max(0.0, left * right * selectivity_values[predicate.id])
    return InitialStats(
        snapshot_id=snapshot_id,
        join_selectivities=tuple(selectivity_values.items()),
        estimated_cardinalities=tuple(cardinality_values.items()),
        table_cardinalities=tuple(table_cardinalities.items()),
    )


def _parse_config(raw_config: Any) -> ReoptConfig:
    switch_code = ErrorCode.SWITCH_BUDGET_INVALID
    code = ErrorCode.BAD_PLAN
    data = _iter_mapping(raw_config, code, "config")
    drift_threshold = _as_number(
        _require(data, "drift_threshold", code, "config"),
        code,
        "config.drift_threshold",
        minimum=0.0,
        strict_minimum=True,
    )
    cooldown_ms = _as_int(_require(data, "cooldown_ms", code, "config"), code, "config.cooldown_ms", minimum=0)
    switch_cost_budget = _as_number(
        _require(data, "switch_cost_budget", switch_code, "config"),
        switch_code,
        "config.switch_cost_budget",
        minimum=0.0,
    )
    fixed_scale = _as_int(_require(data, "fixed_scale", code, "config"), code, "config.fixed_scale", minimum=1)
    float_tolerance = _as_number(
        _require(data, "float_tolerance", code, "config"),
        code,
        "config.float_tolerance",
        minimum=0.0,
        strict_minimum=True,
    )
    logical_clock_step_ms = _as_int(
        _require(data, "logical_clock_step_ms", code, "config"),
        code,
        "config.logical_clock_step_ms",
        minimum=1,
    )
    observation_window = _as_int(data.get("observation_window", 3), code, "config.observation_window", minimum=1)
    switch_base_cost = _as_number(data.get("switch_base_cost", 5.0), switch_code, "config.switch_base_cost", minimum=0.0)
    max_candidates = _as_int(data.get("max_candidates", 128), code, "config.max_candidates", minimum=1)
    enable_reopt = data.get("enable_reopt", True)
    if not isinstance(enable_reopt, bool):
        _fail(code, "config.enable_reopt must be boolean", field="config.enable_reopt")
    return ReoptConfig(
        drift_threshold=drift_threshold,
        cooldown_ms=cooldown_ms,
        switch_cost_budget=switch_cost_budget,
        fixed_scale=fixed_scale,
        float_tolerance=float_tolerance,
        logical_clock_step_ms=logical_clock_step_ms,
        observation_window=observation_window,
        switch_base_cost=switch_base_cost,
        max_candidates=max_candidates,
        enable_reopt=enable_reopt,
    )


def parse_scenario(raw: Mapping[str, Any]) -> Scenario:
    if not isinstance(raw, Mapping):
        _fail(ErrorCode.BAD_PLAN, "scenario must be an object")
    tables = _parse_tables(raw.get("tables"))
    plan, predicates = _parse_plan(raw.get("plan"), tables)
    stats = _parse_stats(raw.get("initial_stats", raw.get("stats")), tables, predicates)
    config = _parse_config(raw.get("config"))
    scenario = Scenario(
        tables=tables,
        plan=plan,
        initial_stats=stats,
        config=config,
        source_digest=digest_value(raw),
        raw=dict(raw),
    )
    return scenario


def load_scenario(path: str) -> Scenario:
    return parse_scenario(load_json(path))

