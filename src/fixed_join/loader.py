from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import EngineError
from .models import AggregateSpec, Column, Dataset, Predicate, Projection, Query, Table


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMPARISON_OPS = {"=", "!=", "<", "<=", ">", ">="}
COLUMN_TYPES = {"int", "float"}


def _read_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise EngineError("E_TABLE", f"cannot read JSON file: {exc}", details={"path": str(source)}) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EngineError(
            "E_TABLE",
            "invalid JSON document",
            row=exc.lineno,
            column=str(exc.colno),
            details={"path": str(source), "problem": exc.msg},
        ) from exc


def _table_error(message: str, table: str | None, row: int | None, column: str | None, details: dict[str, Any] | None = None) -> EngineError:
    return EngineError("E_TABLE", message, table=table, row=row, column=column, details=details or {})


def _nullmap_error(message: str, table: str, row: int | None, column: str | None, details: dict[str, Any] | None = None) -> EngineError:
    return EngineError(
        "E_NULLMAP",
        message,
        table=table,
        row=row,
        column=column,
        parent_code="E_TABLE",
        details=details or {},
    )


def load_dataset(path: str | Path) -> Dataset:
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), list):
        raise _table_error("dataset must contain a tables array", None, None, None)
    raw_tables = payload["tables"]
    if len(raw_tables) != 2:
        raise _table_error(
            "dataset must contain exactly two logical tables",
            None,
            None,
            None,
            {"table_count": len(raw_tables)},
        )

    tables: list[Table] = []
    names: set[str] = set()
    for table_index, raw_table in enumerate(raw_tables, start=1):
        table = _parse_table(raw_table, table_index, str(path))
        if table.name in names:
            raise _table_error(
                "table names must be unique",
                table.name,
                None,
                None,
                {"duplicate_table": table.name},
            )
        names.add(table.name)
        tables.append(table)
    return Dataset(tables=tuple(tables), source_path=str(path))


def _parse_table(raw_table: Any, table_index: int, source_path: str) -> Table:
    if not isinstance(raw_table, dict):
        raise _table_error("table definition must be an object", None, table_index, None)
    name = raw_table.get("name")
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise _table_error("table name is not a valid identifier", str(name) if name is not None else None, None, None)

    raw_columns = raw_table.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise _table_error("table must declare at least one column", name, None, None)

    columns: list[Column] = []
    column_names: set[str] = set()
    for column_index, raw_column in enumerate(raw_columns, start=1):
        if not isinstance(raw_column, dict):
            raise _table_error("column definition must be an object", name, column_index, None)
        column_name = raw_column.get("name")
        if not isinstance(column_name, str) or not IDENTIFIER_RE.match(column_name):
            raise _table_error("column name is not a valid identifier", name, None, str(column_name))
        if column_name in column_names:
            raise _table_error("column names must be unique", name, None, column_name)
        column_type = raw_column.get("type")
        if column_type not in COLUMN_TYPES:
            raise _table_error("column type must be int or float", name, None, column_name)
        nullable = raw_column.get("nullable", False)
        if not isinstance(nullable, bool):
            raise _table_error("nullable must be a boolean", name, None, column_name)

        min_value = raw_column.get("min_value")
        max_value = raw_column.get("max_value")
        if column_type == "int":
            if isinstance(min_value, bool) or not isinstance(min_value, int):
                raise _table_error("integer column requires integer min_value", name, None, column_name)
            if isinstance(max_value, bool) or not isinstance(max_value, int):
                raise _table_error("integer column requires integer max_value", name, None, column_name)
            if min_value > max_value:
                raise _table_error("integer column min_value exceeds max_value", name, None, column_name)
        else:
            min_value = None
            max_value = None
        column_names.add(column_name)
        columns.append(
            Column(
                name=column_name,
                type=column_type,
                nullable=nullable,
                min_value=min_value,
                max_value=max_value,
            )
        )

    raw_rows = raw_table.get("rows")
    if not isinstance(raw_rows, list):
        raise _table_error("rows must be an array", name, None, None)
    row_count = len(raw_rows)

    raw_bitmap = raw_table.get("null_bitmap", {})
    if not isinstance(raw_bitmap, dict):
        raise _nullmap_error("null_bitmap must be an object", name, None, None)
    known_columns = {column.name for column in columns}
    for bitmap_column in raw_bitmap:
        if bitmap_column not in known_columns:
            raise _nullmap_error(
                "null bitmap references an unknown column",
                name,
                None,
                bitmap_column,
            )

    bitmap: dict[str, tuple[int, ...]] = {}
    for column in columns:
        if not column.nullable:
            if column.name in raw_bitmap:
                raise _nullmap_error(
                    "non-nullable column must not declare a null bitmap",
                    name,
                    None,
                    column.name,
                )
            bitmap[column.name] = tuple(0 for _ in range(row_count))
            continue
        bits = raw_bitmap.get(column.name)
        if not isinstance(bits, list):
            raise _nullmap_error(
                "nullable column requires a null bitmap array",
                name,
                None,
                column.name,
            )
        if len(bits) != row_count:
            raise _nullmap_error(
                "null bitmap length does not match row count",
                name,
                min(len(bits) + 1, row_count if row_count else 1),
                column.name,
                {"bitmap_length": len(bits), "row_count": row_count},
            )
        for offset, bit in enumerate(bits, start=1):
            if isinstance(bit, bool) or bit not in (0, 1):
                raise _nullmap_error(
                    "null bitmap entries must be 0 or 1",
                    name,
                    offset,
                    column.name,
                )
        bitmap[column.name] = tuple(int(bit) for bit in bits)

    rows: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, dict):
            raise _table_error("row must be an object", name, row_index, None)
        actual_keys = set(raw_row.keys())
        if actual_keys != column_names:
            missing = sorted(column_names - actual_keys)
            unknown = sorted(actual_keys - column_names)
            raise _table_error(
                "row columns do not match the declared schema",
                name,
                row_index,
                missing[0] if missing else (unknown[0] if unknown else None),
                {"missing": missing, "unknown": unknown},
            )

        normalized: dict[str, Any] = {}
        for column in columns:
            value = raw_row[column.name]
            is_null = value is None
            bitmap_null = bitmap[column.name][row_index - 1] == 1
            if is_null != bitmap_null:
                raise _nullmap_error(
                    "row nullability disagrees with the null bitmap",
                    name,
                    row_index,
                    column.name,
                    {"row_is_null": is_null, "bitmap_is_null": bitmap_null},
                )
            if is_null:
                if not column.nullable:
                    raise _table_error("non-nullable column contains null", name, row_index, column.name)
                normalized[column.name] = None
                continue

            if column.type == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _table_error("integer column contains a non-integer value", name, row_index, column.name)
                if column.min_value is not None and value < column.min_value:
                    raise _table_error(
                        "integer value is below the declared range",
                        name,
                        row_index,
                        column.name,
                        {"value": value, "min_value": column.min_value, "max_value": column.max_value},
                    )
                if column.max_value is not None and value > column.max_value:
                    raise _table_error(
                        "integer value is above the declared range",
                        name,
                        row_index,
                        column.name,
                        {"value": value, "min_value": column.min_value, "max_value": column.max_value},
                    )
                normalized[column.name] = value
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _table_error("float column contains a non-numeric value", name, row_index, column.name)
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise _table_error("float column contains a non-finite value", name, row_index, column.name)
                normalized[column.name] = numeric
        rows.append(normalized)

    return Table(
        name=name,
        columns=tuple(columns),
        rows=tuple(rows),
        null_bitmap=bitmap,
        source_path=source_path,
    )


def load_query(path: str | Path, dataset: Dataset) -> Query:
    payload = _read_json_query(path)
    if not isinstance(payload, dict):
        raise EngineError("E_QUERY", "query must be a JSON object", details={"path": str(path)})

    join_type = payload.get("join_type")
    if not isinstance(join_type, str) or join_type.upper() != "INNER":
        raise EngineError("E_QUERY", "only INNER joins are supported", details={"join_type": join_type})

    left_name = payload.get("left_table")
    right_name = payload.get("right_table")
    table_names = {table.name for table in dataset.tables}
    if left_name not in table_names:
        raise EngineError("E_QUERY", "left_table does not exist", table=left_name, details={"available": sorted(table_names)})
    if right_name not in table_names:
        raise EngineError("E_QUERY", "right_table does not exist", table=right_name, details={"available": sorted(table_names)})
    if left_name == right_name:
        raise EngineError("E_QUERY", "left_table and right_table must be different")
    left_table = dataset.table(left_name)
    right_table = dataset.table(right_name)

    join_key = payload.get("join_key")
    if not isinstance(join_key, dict):
        raise EngineError("E_QUERY", "join_key must be an object")
    left_key = join_key.get("left")
    right_key = join_key.get("right")
    left_column = _require_query_column(left_table, left_key, "left join key")
    right_column = _require_query_column(right_table, right_key, "right join key")
    if left_column.type not in {"int", "float"} or right_column.type not in {"int", "float"}:
        raise EngineError("E_QUERY", "join key columns must be numeric", table=left_table.name, column=left_key)

    build_side = payload.get("build_side", "right")
    if build_side not in {"left", "right"}:
        raise EngineError("E_QUERY", "build_side must be left or right", details={"build_side": build_side})

    raw_predicates = payload.get("predicates", {})
    if not isinstance(raw_predicates, dict):
        raise EngineError("E_QUERY", "predicates must be an object")
    predicates: dict[str, tuple[Predicate, ...]] = {"left": tuple(), "right": tuple()}
    for side, table in (("left", left_table), ("right", right_table)):
        raw_side = raw_predicates.get(side, [])
        if not isinstance(raw_side, list):
            raise EngineError("E_QUERY", "predicate list must be an array", table=table.name, details={"side": side})
        parsed: list[Predicate] = []
        for predicate_index, raw_predicate in enumerate(raw_side, start=1):
            if not isinstance(raw_predicate, dict):
                raise EngineError("E_QUERY", "predicate must be an object", table=table.name, row=predicate_index)
            column_name = raw_predicate.get("column")
            column = _require_query_column(table, column_name, f"{side} predicate")
            op = raw_predicate.get("op")
            if op not in COMPARISON_OPS:
                raise EngineError("E_QUERY", "unsupported predicate operator", table=table.name, column=column_name, details={"op": op})
            value = raw_predicate.get("value")
            if column.type == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise EngineError("E_QUERY", "integer predicate requires an integer constant", table=table.name, column=column_name)
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise EngineError("E_QUERY", "float predicate requires a finite numeric constant", table=table.name, column=column_name)
                value = float(value)
            parsed.append(Predicate(side=side, column=column_name, op=op, value=value))
        predicates[side] = tuple(parsed)

    raw_projection = payload.get("projection")
    if not isinstance(raw_projection, list) or not raw_projection:
        raise EngineError("E_QUERY", "projection must be a non-empty array")
    projections: list[Projection] = []
    output_names: set[str] = set()
    for raw_item in raw_projection:
        if not isinstance(raw_item, dict):
            raise EngineError("E_QUERY", "projection item must be an object")
        output_name = raw_item.get("name")
        if not isinstance(output_name, str) or not IDENTIFIER_RE.match(output_name):
            raise EngineError("E_QUERY", "projection name is not a valid identifier", column=str(output_name))
        if output_name in output_names:
            raise EngineError("E_QUERY", "projection names must be unique", column=output_name)
        side = raw_item.get("side")
        if side not in {"left", "right"}:
            raise EngineError("E_QUERY", "projection side must be left or right", column=output_name)
        table = left_table if side == "left" else right_table
        column_name = raw_item.get("column")
        _require_query_column(table, column_name, "projection")
        output_names.add(output_name)
        projections.append(Projection(name=output_name, side=side, column=column_name))

    raw_aggregate = payload.get("aggregate")
    if not isinstance(raw_aggregate, dict):
        raise EngineError("E_QUERY", "aggregate must be an object")
    aggregate_side = raw_aggregate.get("side")
    if aggregate_side not in {"left", "right"}:
        raise EngineError("E_QUERY", "aggregate side must be left or right")
    aggregate_table = left_table if aggregate_side == "left" else right_table
    aggregate_column = raw_aggregate.get("column")
    _require_query_column(aggregate_table, aggregate_column, "aggregate")

    return Query(
        left_table=left_name,
        right_table=right_name,
        join_key_left=left_key,
        join_key_right=right_key,
        build_side=build_side,
        predicates=predicates,
        projection=tuple(projections),
        aggregate=AggregateSpec(side=aggregate_side, column=aggregate_column),
        join_type="INNER",
        source_path=str(path),
    )


def _read_json_query(path: str | Path) -> Any:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise EngineError("E_QUERY", f"cannot read query file: {exc}", details={"path": str(source)}) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EngineError(
            "E_QUERY",
            "invalid query JSON",
            row=exc.lineno,
            column=str(exc.colno),
            details={"path": str(source), "problem": exc.msg},
        ) from exc


def _require_query_column(table: Table, column_name: Any, context: str) -> Column:
    if not isinstance(column_name, str):
        raise EngineError("E_QUERY", f"{context} requires a column name", table=table.name)
    try:
        return table.column(column_name)
    except KeyError as exc:
        raise EngineError(
            "E_QUERY",
            f"{context} column does not exist",
            table=table.name,
            column=column_name,
            details={"available": list(table.column_names)},
        ) from exc
