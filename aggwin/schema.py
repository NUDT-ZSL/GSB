"""Input table loading with strict, locatable validation (E_SCHEMA).

The input file is a JSON document::

    {
      "version": 1,
      "tables": [
        {
          "name": "orders",
          "columns": [
            {"name": "ts", "type": "timestamp"},
            {"name": "grp", "type": "string"},
            {"name": "amount", "type": "int"}
          ],
          "timestamp_column": "ts",
          "ordering": "monotonic" | "out_of_order",
          "allowed_lag": 100,
          "rows": [[1, "a", 10], ...]
        }
      ]
    }
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field

from .errors import BusinessError, E_SCHEMA

SUPPORTED_TYPES = ("timestamp", "int", "long", "double", "string")
NUMERIC_TYPES = ("int", "long", "double")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema_fail(message: str, **context) -> BusinessError:
    return BusinessError(E_SCHEMA, message, context)


def _valid_identifier(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(IDENT_RE.match(name))
        and not keyword.iskeyword(name)
    )


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    timestamp_column: str
    ordering: str  # "monotonic" | "out_of_order"
    allowed_lag: int
    ooo_ratio_limit: float
    rows: list[list] = field(default_factory=list)

    def column_index(self, name: str) -> int:
        for idx, col in enumerate(self.columns):
            if col.name == name:
                return idx
        raise KeyError(name)

    def column_type(self, name: str) -> str:
        return self.columns[self.column_index(name)].type


def _check_value(value, col: Column, table_name: str, row_no: int) -> None:
    if value is None:
        raise _schema_fail(
            "null values are not supported (deterministic pipeline)",
            table=table_name,
            row=row_no,
            field=col.name,
        )
    if col.type in ("int", "long", "timestamp"):
        if not isinstance(value, int) or isinstance(value, bool):
            raise _schema_fail(
                f"field {col.name} requires an integer value",
                table=table_name,
                row=row_no,
                field=col.name,
                actual=type(value).__name__,
            )
    elif col.type == "double":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _schema_fail(
                f"field {col.name} requires a numeric value",
                table=table_name,
                row=row_no,
                field=col.name,
                actual=type(value).__name__,
            )
    elif col.type == "string":
        if not isinstance(value, str):
            raise _schema_fail(
                f"field {col.name} requires a string value",
                table=table_name,
                row=row_no,
                field=col.name,
                actual=type(value).__name__,
            )


def _parse_table(raw: object, seen_names: set[str]) -> Table:
    if not isinstance(raw, dict):
        raise _schema_fail("each table must be an object", table_index=len(seen_names))
    name = raw.get("name")
    if not _valid_identifier(name):
        raise _schema_fail(
            "table name missing or not a legal identifier",
            table=name,
            field="name",
        )
    if name in seen_names:
        raise _schema_fail("duplicate table name", table=name, field="name")
    seen_names.add(name)

    raw_cols = raw.get("columns")
    if not isinstance(raw_cols, list) or not raw_cols:
        raise _schema_fail("columns must be a non-empty list", table=name, field="columns")
    columns: list[Column] = []
    seen_cols: set[str] = set()
    for idx, raw_col in enumerate(raw_cols):
        if not isinstance(raw_col, dict):
            raise _schema_fail(
                "column declaration must be an object",
                table=name,
                field=f"columns[{idx}]",
            )
        col_name = raw_col.get("name")
        col_type = raw_col.get("type")
        if not _valid_identifier(col_name):
            raise _schema_fail(
                "column name missing or illegal",
                table=name,
                field=f"columns[{idx}].name",
            )
        if col_name in seen_cols:
            raise _schema_fail(
                "duplicate column name",
                table=name,
                field=f"columns[{idx}].name",
            )
        if col_type not in SUPPORTED_TYPES:
            raise _schema_fail(
                "unsupported column type",
                table=name,
                field=f"columns[{idx}].type",
                actual=col_type,
                supported=list(SUPPORTED_TYPES),
            )
        seen_cols.add(col_name)
        columns.append(Column(col_name, col_type))

    ts_col = raw.get("timestamp_column")
    if ts_col not in seen_cols:
        raise _schema_fail(
            "timestamp_column must reference a declared column",
            table=name,
            field="timestamp_column",
            actual=ts_col,
        )
    ts_type = next(c.type for c in columns if c.name == ts_col)
    if ts_type != "timestamp":
        raise _schema_fail(
            "timestamp_column must be of type timestamp",
            table=name,
            field="timestamp_column",
            actual_type=ts_type,
        )

    ordering = raw.get("ordering", "monotonic")
    if ordering not in ("monotonic", "out_of_order"):
        raise _schema_fail(
            "ordering must be monotonic or out_of_order",
            table=name,
            field="ordering",
            actual=ordering,
        )
    allowed_lag = raw.get("allowed_lag", 0)
    if not isinstance(allowed_lag, int) or isinstance(allowed_lag, bool) or allowed_lag < 0:
        raise _schema_fail(
            "allowed_lag must be a non-negative integer",
            table=name,
            field="allowed_lag",
            actual=allowed_lag,
        )
    ooo_ratio_limit = raw.get("ooo_ratio_limit", 1.0)
    if (
        not isinstance(ooo_ratio_limit, (int, float))
        or isinstance(ooo_ratio_limit, bool)
        or not 0.0 <= float(ooo_ratio_limit) <= 1.0
    ):
        raise _schema_fail(
            "ooo_ratio_limit must be a number in [0, 1]",
            table=name,
            field="ooo_ratio_limit",
            actual=ooo_ratio_limit,
        )

    raw_rows = raw.get("rows", [])
    if not isinstance(raw_rows, list):
        raise _schema_fail("rows must be a list", table=name, field="rows")
    rows: list[list] = []
    ts_idx = next(i for i, c in enumerate(columns) if c.name == ts_col)
    last_ts: int | None = None
    for row_idx, row in enumerate(raw_rows):
        row_no = row_idx + 1
        if not isinstance(row, list) or len(row) != len(columns):
            raise _schema_fail(
                "row must be a list matching the column count",
                table=name,
                row=row_no,
                field="<row>",
                expected_columns=len(columns),
            )
        for col, value in zip(columns, row):
            _check_value(value, col, name, row_no)
        ts_value = row[ts_idx]
        if ordering == "monotonic" and last_ts is not None and ts_value < last_ts:
            raise _schema_fail(
                "monotonic table received a decreasing timestamp",
                table=name,
                row=row_no,
                field=ts_col,
                previous_timestamp=last_ts,
                actual_timestamp=ts_value,
            )
        last_ts = ts_value
        rows.append(list(row))

    return Table(
        name=name,
        columns=columns,
        timestamp_column=ts_col,
        ordering=ordering,
        allowed_lag=allowed_lag,
        ooo_ratio_limit=float(ooo_ratio_limit),
        rows=rows,
    )


def load_tables(path: str) -> dict[str, Table]:
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise _schema_fail("input file not found", path=path)
    except json.JSONDecodeError as exc:
        raise _schema_fail(
            "input file is not valid JSON",
            path=path,
            line=exc.lineno,
            column=exc.colno,
            detail=exc.msg,
        )
    if not isinstance(document, dict) or not isinstance(document.get("tables"), list):
        raise _schema_fail("top-level document needs a 'tables' list", path=path)
    seen: set[str] = set()
    tables = [_parse_table(raw, seen) for raw in document["tables"]]
    return {table.name: table for table in tables}
