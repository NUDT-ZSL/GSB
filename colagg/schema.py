"""Parsing and validation for batches, queries, and precision settings."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, read_json, sha256_text, sha256_value
from .errors import (
    ColAggError,
    E_BAD_BATCH,
    E_BAD_PRECISION,
    E_BAD_QUERY,
    E_TYPE_MISMATCH,
)


NUMERIC_KINDS = {"integer", "float"}
SCALAR_KINDS = {"integer", "float", "string", "boolean"}
AGGREGATE_OPS = {
    "sum": "sum",
    "mean": "mean",
    "avg": "mean",
    "average": "mean",
    "variance": "variance",
    "var": "variance",
    "var_pop": "variance",
    "count": "count",
}
WINDOW_OPS = {
    "sum": "sum",
    "mean": "mean",
    "avg": "mean",
    "average": "mean",
    "variance": "variance",
    "var": "variance",
    "var_pop": "variance",
    "count": "count",
}
BOUNDARIES = {"emit", "mark", "skip"}

_EXPR_RE = re.compile(
    r"^\s*(?P<op>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<column>[^)]*?)\s*\)"
    r"\s*(?:\bas\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*))?\s*$",
    re.IGNORECASE,
)
_DIGEST_FIELDS = (
    "content_digest",
    "digest",
    "sha256",
    "batch_digest",
    "checksum",
)


def _error(code: str, message: str, **details: Any) -> ColAggError:
    return ColAggError(code, message, details=details)


def _first(mapping: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _as_int(value: Any, *, code: str, field_name: str) -> int:
    if isinstance(value, bool):
        raise _error(code, f"{field_name} must be an integer", field=field_name)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise _error(code, f"{field_name} must be an integer", field=field_name)


def _normalize_kind(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(E_BAD_BATCH, "column type must be a string", type=type(value).__name__)
    raw = value.strip().lower()
    aliases = {
        "int": "integer",
        "int32": "integer",
        "int64": "integer",
        "integer": "integer",
        "float": "float",
        "float32": "float",
        "float64": "float",
        "double": "float",
        "number": "float",
        "str": "string",
        "string": "string",
        "text": "string",
        "bool": "boolean",
        "boolean": "boolean",
    }
    if raw not in aliases:
        raise _error(E_BAD_BATCH, f"unsupported column type: {value}", type=value)
    return aliases[raw]


def _infer_kind(values: list[Any]) -> str:
    if not values:
        return "empty"
    saw_float = False
    saw_int = False
    saw_string = False
    saw_bool = False
    for value in values:
        if isinstance(value, bool):
            saw_bool = True
        elif isinstance(value, int):
            saw_int = True
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise _error(E_BAD_BATCH, "non-finite float in column")
            saw_float = True
        elif isinstance(value, str):
            saw_string = True
        else:
            raise _error(
                E_BAD_BATCH,
                f"unsupported column value type: {type(value).__name__}",
            )
    categories = sum((saw_bool, saw_string, saw_int or saw_float))
    if categories > 1:
        raise _error(E_BAD_BATCH, "column mixes incompatible value types")
    if saw_bool:
        return "boolean"
    if saw_string:
        return "string"
    if saw_float:
        return "float"
    return "integer"


def _validate_values(kind: str, values: list[Any], *, column: str) -> None:
    for index, value in enumerate(values):
        if value is None:
            raise _error(
                E_BAD_BATCH,
                f"column {column!r} contains null at row {index}",
                column=column,
                row_index=index,
            )
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise _error(
                    E_TYPE_MISMATCH,
                    f"column {column!r} expected integer at row {index}",
                    column=column,
                    row_index=index,
                )
        elif kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _error(
                    E_TYPE_MISMATCH,
                    f"column {column!r} expected number at row {index}",
                    column=column,
                    row_index=index,
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise _error(
                    E_BAD_BATCH,
                    f"column {column!r} contains a non-finite float",
                    column=column,
                    row_index=index,
                )
        elif kind == "string":
            if not isinstance(value, str):
                raise _error(
                    E_TYPE_MISMATCH,
                    f"column {column!r} expected string at row {index}",
                    column=column,
                    row_index=index,
                )
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise _error(
                    E_TYPE_MISMATCH,
                    f"column {column!r} expected boolean at row {index}",
                    column=column,
                    row_index=index,
                )
        else:
            raise _error(E_BAD_BATCH, f"cannot validate column {column!r}")


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    values: tuple[Any, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "values": list(self.values)}


@dataclass(frozen=True)
class Batch:
    seq: int
    row_count: int
    columns: tuple[Column, ...]
    digest: str
    provided_digest: str | None = None

    @property
    def column_map(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "row_count": self.row_count,
            "columns": [column.as_dict() for column in self.columns],
        }

    def payload_json(self) -> str:
        return canonical_json(self.body())


def _parse_columns(raw_columns: Any, *, seq: int) -> tuple[Column, ...]:
    if isinstance(raw_columns, dict):
        entries = [
            {"name": name, "values": value}
            if isinstance(value, list)
            else {"name": name, **value}
            for name, value in raw_columns.items()
        ]
    elif isinstance(raw_columns, list):
        entries = raw_columns
    else:
        raise _error(E_BAD_BATCH, "batch columns must be an object or array", seq=seq)

    result: list[Column] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _error(E_BAD_BATCH, "column entry must be an object", seq=seq, index=index)
        name = entry.get("name", entry.get("column"))
        if not isinstance(name, str) or not name:
            raise _error(E_BAD_BATCH, "column name must be a non-empty string", seq=seq, index=index)
        if name in seen:
            raise _error(E_BAD_BATCH, f"duplicate column name: {name}", seq=seq, column=name)
        seen.add(name)
        values = entry.get("values", entry.get("data"))
        if not isinstance(values, list):
            raise _error(E_BAD_BATCH, f"column {name!r} values must be an array", seq=seq, column=name)
        declared_kind = _normalize_kind(
            entry.get("type", entry.get("kind", entry.get("dtype")))
        )
        inferred_kind = _infer_kind(values)
        if declared_kind is None:
            kind = inferred_kind
        else:
            kind = declared_kind
        _validate_values(kind, values, column=name)
        result.append(Column(name=name, kind=kind, values=tuple(values)))
    return tuple(result)


def parse_batches(path: Path, *, verify_digest: bool = True) -> list[Batch]:
    document = read_json(path, code=E_BAD_BATCH)
    if isinstance(document, dict):
        raw_batches = document.get("batches", document.get("chunks"))
        if raw_batches is None and "columns" in document:
            raw_batches = [document]
    else:
        raw_batches = document
    if not isinstance(raw_batches, list) or not raw_batches:
        raise _error(E_BAD_BATCH, "batch file must contain a non-empty batches array")

    batches: list[Batch] = []
    previous_seq: int | None = None
    seen_seqs: set[int] = set()
    for position, raw in enumerate(raw_batches):
        if not isinstance(raw, dict):
            raise _error(E_BAD_BATCH, "each batch must be an object", position=position)
        seq = _as_int(
            _first(
                raw,
                (
                    "seq",
                    "batch_seq",
                    "batch_id",
                    "batch_index",
                    "batch_no",
                    "ordinal",
                    "index",
                ),
            ),
            code=E_BAD_BATCH,
            field_name="batch seq",
        )
        if seq < 0:
            raise _error(E_BAD_BATCH, "batch seq must be non-negative", seq=seq)
        if seq in seen_seqs:
            raise _error(E_BAD_BATCH, f"duplicate batch seq: {seq}", seq=seq)
        if previous_seq is not None and seq <= previous_seq:
            raise _error(
                E_BAD_BATCH,
                "batch seq must be strictly increasing",
                previous_seq=previous_seq,
                seq=seq,
            )
        row_count = _as_int(
            _first(raw, ("row_count", "rows", "num_rows", "n_rows", "count")),
            code=E_BAD_BATCH,
            field_name="row_count",
        )
        if row_count < 0:
            raise _error(E_BAD_BATCH, "row_count must be non-negative", seq=seq)
        raw_columns = _first(raw, ("columns", "columnar"))
        columns = _parse_columns(raw_columns, seq=seq)
        if not columns and row_count:
            raise _error(E_BAD_BATCH, "a non-empty batch must have columns", seq=seq)
        for column in columns:
            if len(column.values) != row_count:
                raise _error(
                    E_BAD_BATCH,
                    f"column {column.name!r} length {len(column.values)} != row_count {row_count}",
                    seq=seq,
                    column=column.name,
                    actual=len(column.values),
                    expected=row_count,
                )
        body = {
            "seq": seq,
            "row_count": row_count,
            "columns": [column.as_dict() for column in columns],
        }
        digest = sha256_value(body)
        provided_digest: str | None = None
        for field_name in _DIGEST_FIELDS:
            if field_name in raw:
                raw_digest = raw[field_name]
                if not isinstance(raw_digest, str):
                    raise _error(
                        E_BAD_BATCH,
                        f"{field_name} must be a string",
                        seq=seq,
                        field=field_name,
                    )
                provided_digest = raw_digest.removeprefix("sha256:").lower()
                break
        if verify_digest and provided_digest is not None and provided_digest != digest:
            raise _error(
                "E_BATCH_DIGEST_MISMATCH",
                f"batch {seq} content digest does not match its payload",
                seq=seq,
                expected=digest,
                actual=provided_digest,
            )
        batches.append(
            Batch(
                seq=seq,
                row_count=row_count,
                columns=columns,
                digest=digest,
                provided_digest=provided_digest,
            )
        )
        previous_seq = seq
        seen_seqs.add(seq)
    return batches


@dataclass(frozen=True)
class Precision:
    accumulator: str
    scale: int
    relative_tolerance: float
    clock_step: int
    clock_start: int
    int_bits: int

    def signature(self) -> dict[str, Any]:
        return {
            "accumulator": self.accumulator,
            "scale": self.scale,
            "int_bits": self.int_bits,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "accumulator": self.accumulator,
            "scale": self.scale,
            "relative_tolerance": self.relative_tolerance,
            "clock_step": self.clock_step,
            "clock_start": self.clock_start,
            "int_bits": self.int_bits,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Precision":
        return cls(
            accumulator=value["accumulator"],
            scale=int(value["scale"]),
            relative_tolerance=float(value["relative_tolerance"]),
            clock_step=int(value["clock_step"]),
            clock_start=int(value["clock_start"]),
            int_bits=int(value["int_bits"]),
        )


def parse_precision(path: Path) -> Precision:
    document = read_json(path, code=E_BAD_PRECISION)
    if not isinstance(document, dict):
        raise _error(E_BAD_PRECISION, "precision config must be an object")
    raw_accumulator = _first(
        document,
        (
            "accumulator",
            "accum_type",
            "accumulation",
            "accumulation_type",
            "type",
        ),
        "float64",
    )
    if not isinstance(raw_accumulator, str):
        raise _error(E_BAD_PRECISION, "accumulator must be a string")
    accumulator = raw_accumulator.strip().lower()
    accumulator_aliases = {
        "float64": "float64",
        "float": "float64",
        "double": "float64",
        "fixed": "fixed",
        "fixed_int": "fixed",
        "fixed_integer": "fixed",
        "fixed-point": "fixed",
        "integer": "fixed",
        "int": "fixed",
    }
    if accumulator not in accumulator_aliases:
        raise _error(
            E_BAD_PRECISION,
            "accumulator must be float64 or fixed",
            accumulator=raw_accumulator,
        )
    accumulator = accumulator_aliases[accumulator]
    raw_scale = _first(document, ("scale", "fixed_scale", "decimal_scale"))
    if raw_scale is None:
        raise _error(E_BAD_PRECISION, "fixed scale is required")
    scale = _as_int(raw_scale, code=E_BAD_PRECISION, field_name="scale")
    if scale <= 0 or scale > 10**12:
        raise _error(E_BAD_PRECISION, "scale must be in [1, 10^12]", scale=scale)
    raw_tolerance = _first(
        document,
        ("relative_tolerance", "rel_tol", "tolerance"),
        1e-9,
    )
    if isinstance(raw_tolerance, bool) or not isinstance(raw_tolerance, (int, float)):
        raise _error(E_BAD_PRECISION, "relative_tolerance must be numeric")
    tolerance = float(raw_tolerance)
    if not math.isfinite(tolerance) or tolerance < 0 or tolerance >= 1:
        raise _error(
            E_BAD_PRECISION,
            "relative_tolerance must be finite and in [0, 1)",
            relative_tolerance=raw_tolerance,
        )
    clock_step = _as_int(
        _first(document, ("clock_step", "logical_clock_step"), 1),
        code=E_BAD_PRECISION,
        field_name="clock_step",
    )
    if clock_step <= 0:
        raise _error(E_BAD_PRECISION, "clock_step must be a positive integer")
    clock_start = _as_int(
        _first(document, ("clock_start", "logical_clock_start", "start_clock"), 0),
        code=E_BAD_PRECISION,
        field_name="clock_start",
    )
    int_bits = _as_int(
        _first(document, ("int_bits", "fixed_bits"), 64),
        code=E_BAD_PRECISION,
        field_name="int_bits",
    )
    if int_bits not in (32, 64):
        raise _error(E_BAD_PRECISION, "int_bits must be 32 or 64", int_bits=int_bits)
    return Precision(
        accumulator=accumulator,
        scale=scale,
        relative_tolerance=tolerance,
        clock_step=clock_step,
        clock_start=clock_start,
        int_bits=int_bits,
    )


@dataclass(frozen=True)
class OperatorSpec:
    kind: str
    name: str
    op: str
    column: str | None
    group_by: tuple[str, ...]
    precision_signature: dict[str, Any] = field(compare=False, hash=False)
    window: dict[str, Any] | None = None
    operator_id: str = ""

    def logical_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "op": self.op,
            "column": self.column,
            "group_by": list(self.group_by),
            "precision": self.precision_signature,
        }
        if self.window is not None:
            payload["window"] = self.window
        return payload

    def as_dict(self) -> dict[str, Any]:
        value = self.logical_dict()
        value["operator_id"] = self.operator_id
        return value


@dataclass(frozen=True)
class PlanDefinition:
    version: int
    digest: str
    group_by: tuple[str, ...]
    aggregates: tuple[OperatorSpec, ...]
    windows: tuple[OperatorSpec, ...]
    precision: Precision

    @property
    def operators(self) -> tuple[OperatorSpec, ...]:
        return tuple(
            sorted(self.aggregates + self.windows, key=lambda item: item.operator_id)
        )

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "digest": self.digest,
            "group_by": list(self.group_by),
            "aggregates": [item.as_dict() for item in self.aggregates],
            "windows": [item.as_dict() for item in self.windows],
            "precision": self.precision.as_dict(),
        }

    @classmethod
    def from_normalized_dict(cls, value: dict[str, Any]) -> "PlanDefinition":
        precision = Precision.from_dict(value["precision"])

        def make(spec: dict[str, Any]) -> OperatorSpec:
            return OperatorSpec(
                kind=spec["kind"],
                name=spec["name"],
                op=spec["op"],
                column=spec.get("column"),
                group_by=tuple(spec["group_by"]),
                precision_signature=spec["precision"],
                window=spec.get("window"),
                operator_id=spec["operator_id"],
            )

        return cls(
            version=int(value["version"]),
            digest=value["digest"],
            group_by=tuple(value["group_by"]),
            aggregates=tuple(make(item) for item in value["aggregates"]),
            windows=tuple(make(item) for item in value["windows"]),
            precision=precision,
        )


def _parse_expr(text: str, *, code: str) -> tuple[str, str | None, str | None]:
    match = _EXPR_RE.match(text)
    if not match:
        raise _error(code, f"cannot parse expression: {text!r}", expression=text)
    return (
        match.group("op").lower(),
        match.group("column") or None,
        match.group("name"),
    )


def _parse_group_by(document: dict[str, Any]) -> tuple[str, ...]:
    raw = _first(
        document,
        ("group_by", "group_keys", "group_key", "groupBy", "groups", "group"),
        [],
    )
    if isinstance(raw, str):
        raw = [raw]
    if raw is None:
        raw = []
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise _error(E_BAD_QUERY, "group_by must be an array of non-empty column names")
    if len(set(raw)) != len(raw):
        raise _error(E_BAD_QUERY, "group_by contains duplicate column names")
    return tuple(raw)


def _parse_aggregates(
    document: dict[str, Any],
    group_by: tuple[str, ...],
    precision: Precision,
) -> tuple[OperatorSpec, ...]:
    raw_aggregates = _first(document, ("aggregates", "aggregations", "metrics"), [])
    if raw_aggregates is None:
        raw_aggregates = []
    if isinstance(raw_aggregates, dict) and not any(
        key in raw_aggregates
        for key in ("op", "func", "function", "expr", "expression", "column")
    ):
        raw_aggregates = [
            {"name": name, **value}
            if isinstance(value, dict)
            else {"name": name, "expr": value}
            for name, value in raw_aggregates.items()
        ]
    elif isinstance(raw_aggregates, (str, dict)):
        raw_aggregates = [raw_aggregates]
    if not isinstance(raw_aggregates, list):
        raise _error(E_BAD_QUERY, "aggregates must be an array")

    result: list[OperatorSpec] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_aggregates):
        if isinstance(raw, str):
            op, column, name = _parse_expr(raw, code=E_BAD_QUERY)
            if op == "count" and column == "*":
                column = None
        elif isinstance(raw, dict):
            expression = raw.get("expr", raw.get("expression"))
            if expression is not None:
                if not isinstance(expression, str):
                    raise _error(E_BAD_QUERY, "aggregate expression must be a string")
                expr_op, expr_column, expr_name = _parse_expr(expression, code=E_BAD_QUERY)
                op = raw.get("op", expr_op)
                column = raw.get("column", expr_column)
                name = _first(raw, ("name", "as", "alias"), expr_name)
            else:
                op = _first(raw, ("op", "func", "function", "agg"))
                column = _first(raw, ("column", "col", "field"))
                name = _first(raw, ("name", "as", "alias"))
        else:
            raise _error(E_BAD_QUERY, "aggregate entry must be a string or object", index=index)
        if not isinstance(op, str):
            raise _error(E_BAD_QUERY, "aggregate op is required", index=index)
        op_key = op.strip().lower()
        if op_key not in AGGREGATE_OPS:
            raise _error(E_BAD_QUERY, f"unsupported aggregate op: {op}", index=index)
        op_key = AGGREGATE_OPS[op_key]
        if column == "*":
            column = None
        if column is not None and (not isinstance(column, str) or not column):
            raise _error(E_BAD_QUERY, "aggregate column must be a non-empty string", index=index)
        if op_key != "count" and column is None:
            raise _error(E_BAD_QUERY, f"{op_key} requires a column", index=index)
        if not isinstance(name, str) or not name:
            name = f"{op_key}_{column}" if column else "count"
        if name in names:
            raise _error(E_BAD_QUERY, f"duplicate output name: {name}", name=name)
        names.add(name)
        logical = {
            "kind": "aggregate",
            "name": name,
            "op": op_key,
            "column": column,
            "group_by": list(group_by),
            "precision": precision.signature(),
        }
        operator_id = f"agg:{name}:{sha256_value(logical)[:12]}"
        result.append(
            OperatorSpec(
                kind="aggregate",
                name=name,
                op=op_key,
                column=column,
                group_by=group_by,
                precision_signature=precision.signature(),
                operator_id=operator_id,
            )
        )
    return tuple(result)


def _parse_window_function(
    raw: Any,
    *,
    default_column: str | None,
    index: int,
) -> tuple[str, str | None, str | None]:
    if isinstance(raw, str):
        if "(" in raw:
            return _parse_expr(raw, code=E_BAD_QUERY)
        return raw.strip().lower(), default_column, None
    if isinstance(raw, dict):
        expression = raw.get("expr", raw.get("expression"))
        if expression is not None:
            if not isinstance(expression, str):
                raise _error(E_BAD_QUERY, "window expression must be a string")
            expr_op, expr_column, expr_name = _parse_expr(expression, code=E_BAD_QUERY)
            return (
                str(raw.get("op", expr_op)).lower(),
                raw.get("column", expr_column),
                _first(raw, ("name", "as", "alias"), expr_name),
            )
        op = _first(raw, ("op", "func", "function"))
        if not isinstance(op, str):
            raise _error(E_BAD_QUERY, "window function op is required", index=index)
        return (
            op.lower(),
            _first(raw, ("column", "col", "field"), default_column),
            _first(raw, ("name", "as", "alias")),
        )
    raise _error(E_BAD_QUERY, "window function must be a string or object", index=index)


def _parse_windows(
    document: dict[str, Any],
    group_by: tuple[str, ...],
    precision: Precision,
) -> tuple[OperatorSpec, ...]:
    raw_windows = _first(document, ("windows", "window_functions"))
    if raw_windows is None:
        raw_window = document.get("window")
        raw_windows = [] if raw_window is None else [raw_window]
    if isinstance(raw_windows, dict):
        raw_windows = [raw_windows]
    if not isinstance(raw_windows, list):
        raise _error(E_BAD_QUERY, "windows must be an array")

    result: list[OperatorSpec] = []
    names: set[str] = set()
    for window_index, raw in enumerate(raw_windows):
        if not isinstance(raw, dict):
            raise _error(E_BAD_QUERY, "window definition must be an object", index=window_index)
        column = _first(raw, ("column", "col", "field"))
        if column is not None and (not isinstance(column, str) or not column):
            raise _error(E_BAD_QUERY, "window column must be a non-empty string")
        length = _as_int(
            _first(
                raw,
                ("length", "window_length", "size", "rows"),
                _first(document, ("window_length", "length"), None),
            ),
            code=E_BAD_QUERY,
            field_name="window length",
        )
        step = _as_int(
            _first(
                raw,
                ("step", "window_step", "stride"),
                _first(document, ("window_step", "step"), 1),
            ),
            code=E_BAD_QUERY,
            field_name="window step",
        )
        if length <= 0:
            raise _error(E_BAD_QUERY, "window length must be a positive integer")
        if step <= 0:
            raise _error(E_BAD_QUERY, "window step must be a positive integer")
        if step > length:
            raise _error(E_BAD_QUERY, "window step must be <= window length")
        boundary = str(raw.get("boundary", "emit")).strip().lower()
        if boundary not in BOUNDARIES:
            raise _error(
                E_BAD_QUERY,
                "window boundary must be emit, mark, or skip",
                boundary=boundary,
            )
        functions = _first(raw, ("functions", "ops", "aggregates", "function"), [])
        if not functions and any(
            key in raw for key in ("op", "func", "expr", "expression")
        ):
            functions = [raw]
        if isinstance(functions, (str, dict)):
            functions = [functions]
        if not isinstance(functions, list) or not functions:
            raise _error(E_BAD_QUERY, "window requires at least one function", index=window_index)
        for function_index, function in enumerate(functions):
            op, function_column, name = _parse_window_function(
                function,
                default_column=column,
                index=function_index,
            )
            if op not in WINDOW_OPS:
                raise _error(E_BAD_QUERY, f"unsupported window op: {op}", index=function_index)
            op = WINDOW_OPS[op]
            if function_column == "*":
                function_column = None
            if function_column is not None and (
                not isinstance(function_column, str) or not function_column
            ):
                raise _error(E_BAD_QUERY, "window column must be a non-empty string")
            if op != "count" and function_column is None:
                raise _error(E_BAD_QUERY, f"{op} window requires a column", index=function_index)
            if not isinstance(name, str) or not name:
                name = f"w_{op}_{function_column}" if function_column else "w_count"
            if name in names:
                raise _error(E_BAD_QUERY, f"duplicate output name: {name}", name=name)
            names.add(name)
            window_spec = {
                "column": function_column,
                "length": length,
                "step": step,
                "boundary": boundary,
            }
            logical = {
                "kind": "window",
                "name": name,
                "op": op,
                "column": function_column,
                "group_by": list(group_by),
                "precision": precision.signature(),
                "window": window_spec,
            }
            operator_id = f"win:{name}:{sha256_value(logical)[:12]}"
            result.append(
                OperatorSpec(
                    kind="window",
                    name=name,
                    op=op,
                    column=function_column,
                    group_by=group_by,
                    precision_signature=precision.signature(),
                    window=window_spec,
                    operator_id=operator_id,
                )
            )
    return tuple(result)


def parse_query(path: Path, precision: Precision, *, plan_version: int | None = None) -> PlanDefinition:
    document = read_json(path, code=E_BAD_QUERY)
    if not isinstance(document, dict):
        raise _error(E_BAD_QUERY, "query file must contain an object")
    raw_version = plan_version
    if raw_version is None:
        raw_version = _first(document, ("plan_version", "version"), 1)
    version = _as_int(raw_version, code=E_BAD_QUERY, field_name="plan_version")
    if version <= 0:
        raise _error(E_BAD_QUERY, "plan_version must be positive", plan_version=version)
    group_by = _parse_group_by(document)
    aggregates = _parse_aggregates(document, group_by, precision)
    windows = _parse_windows(document, group_by, precision)
    if not aggregates and not windows:
        raise _error(E_BAD_QUERY, "query must define at least one aggregate or window")
    all_names = [item.name for item in aggregates + windows]
    if len(set(all_names)) != len(all_names):
        raise _error(E_BAD_QUERY, "aggregate and window output names must be unique")
    digest_payload = {
        "group_by": list(group_by),
        "aggregates": [item.logical_dict() for item in aggregates],
        "windows": [item.logical_dict() for item in windows],
        "precision": precision.as_dict(),
    }
    digest = sha256_value(digest_payload)
    return PlanDefinition(
        version=version,
        digest=digest,
        group_by=group_by,
        aggregates=aggregates,
        windows=windows,
        precision=precision,
    )


def _check_reference(
    batches: list[Batch],
    column_name: str,
    *,
    numeric: bool,
    group: bool,
    query_name: str,
) -> None:
    first_kind: str | None = None
    for batch in batches:
        columns = batch.column_map
        if column_name not in columns:
            raise ColAggError(
                E_BAD_QUERY,
                f"query {query_name!r} references missing column {column_name!r}",
                details={
                    "query": query_name,
                    "column": column_name,
                    "batch_seq": batch.seq,
                },
            )
        kind = columns[column_name].kind
        if numeric and kind not in NUMERIC_KINDS:
            raise ColAggError(
                E_TYPE_MISMATCH,
                f"column {column_name!r} must be numeric for {query_name!r}",
                details={"column": column_name, "kind": kind, "query": query_name},
            )
        if group and kind not in SCALAR_KINDS:
            raise ColAggError(
                E_TYPE_MISMATCH,
                f"group column {column_name!r} is not comparable",
                details={"column": column_name, "kind": kind},
            )
        if first_kind is None:
            first_kind = kind
        elif first_kind != kind and not (
            first_kind in NUMERIC_KINDS and kind in NUMERIC_KINDS
        ):
            raise ColAggError(
                E_TYPE_MISMATCH,
                f"column {column_name!r} changes type across batches",
                details={
                    "column": column_name,
                    "batch_seq": batch.seq,
                    "expected": first_kind,
                    "actual": kind,
                },
            )


def validate_plan_against_batches(plan: PlanDefinition, batches: list[Batch]) -> None:
    for name in plan.group_by:
        _check_reference(batches, name, numeric=False, group=True, query_name=f"group_by.{name}")
    for operator in plan.operators:
        if operator.column is None:
            continue
        numeric = operator.op != "count"
        _check_reference(
            batches,
            operator.column,
            numeric=numeric,
            group=False,
            query_name=operator.name,
        )

