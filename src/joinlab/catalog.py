"""Atomic loading and validation of local table/catalog JSON documents."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .errors import SchemaError, SkewError
from .typesys import SUPPORTED_TYPES, normalize_type, validate_value


@dataclass(frozen=True)
class ColumnDef:
    name: str
    key: str
    type: str
    nullable: bool
    ndv: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
        }
        if self.ndv is not None:
            result["ndv"] = self.ndv
        return result


@dataclass(frozen=True)
class TableDef:
    name: str
    key: str
    row_count: int
    skew: bool
    data_file: str | None
    columns: tuple[ColumnDef, ...]
    skew_column: str | None = None
    skew_limit: float | None = None

    @property
    def column_keys(self) -> tuple[str, ...]:
        return tuple(column.key for column in self.columns)

    def column(self, name: str) -> ColumnDef | None:
        key = name.lower()
        for column in self.columns:
            if column.key == key:
                return column
        return None

    def to_dict(self, *, include_rows: bool = False, data: "TableData | None" = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "row_count": self.row_count,
            "skew": self.skew,
            "columns": [column.to_dict() for column in self.columns],
        }
        if self.data_file is not None:
            result["data_file"] = self.data_file
        if self.skew_column is not None:
            result["skew_column"] = self.skew_column
        if self.skew_limit is not None:
            result["skew_limit"] = self.skew_limit
        if include_rows:
            if data is None:
                raise ValueError("data is required when include_rows=True")
            names = [column.key for column in self.columns]
            result["rows"] = [
                {name: row[index] for index, name in enumerate(names)}
                for row in data.rows
            ]
        return result


@dataclass(frozen=True)
class TableData:
    table_key: str
    rows: tuple[tuple[Any, ...], ...]
    actual_ndv: tuple[int, ...]

    def row_dicts(self, table: TableDef) -> list[dict[str, Any]]:
        names = [column.key for column in table.columns]
        return [
            {name: row[index] for index, name in enumerate(names)}
            for row in self.rows
        ]


@dataclass(frozen=True)
class Catalog:
    name: str
    schema_version: int
    data_version: int
    tables: tuple[TableDef, ...]
    data: tuple[TableData, ...]
    skew_limit_default: float = 0.75
    source_path: str = ""

    def _map(self, items: Iterable[Any], attr: str) -> dict[str, Any]:
        return {getattr(item, attr): item for item in items}

    @property
    def table_by_key(self) -> dict[str, TableDef]:
        return {table.key: table for table in self.tables}

    @property
    def data_by_key(self) -> dict[str, TableData]:
        return {data.table_key: data for data in self.data}

    def table(self, name: str) -> TableDef | None:
        key = name.lower()
        return self.table_by_key.get(key)

    def table_data(self, name: str) -> TableData:
        key = name.lower()
        try:
            return self.data_by_key[key]
        except KeyError as exc:
            raise SchemaError(f"unknown table {name!r}", "tables") from exc

    def column(self, table_name: str, column_name: str) -> ColumnDef | None:
        table = self.table(table_name)
        if table is None:
            return None
        return table.column(column_name)

    def scoped_data(self, scale: float) -> tuple[tuple[TableData, ...], tuple[int, ...]]:
        """Return deterministic prefix views and their effective row counts."""

        if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not math.isfinite(float(scale)):
            raise SchemaError("sample scale must be a finite number", "sample_scale")
        if scale <= 0 or scale > 1:
            raise SchemaError("sample scale must be in (0, 1]", "sample_scale")
        data_items: list[TableData] = []
        counts: list[int] = []
        for item in self.data:
            count = int(round(len(item.rows) * float(scale)))
            if len(item.rows) and count == 0:
                count = 1
            data_items.append(replace(item, rows=item.rows[:count]))
            counts.append(count)
        return tuple(data_items), tuple(counts)

    def to_snapshot_dict(self) -> dict[str, Any]:
        data_map = self.data_by_key
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "data_version": self.data_version,
            "skew_limit": self.skew_limit_default,
            "tables": [
                table.to_dict(include_rows=True, data=data_map[table.key])
                for table in self.tables
            ],
        }


def _read_json(path: Path, code: str = "E_SCHEMA", path_label: str = "") -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SchemaError("file does not exist", path_label or str(path)) from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"invalid JSON: {exc.msg}",
            f"{path_label or str(path)}@{exc.lineno}:{exc.colno}",
        ) from exc
    except OSError as exc:
        raise SchemaError(f"cannot read file: {exc}", path_label or str(path)) from exc


def load_catalog(catalog_path: str | Path) -> Catalog:
    path = Path(catalog_path).expanduser().resolve()
    document = _read_json(path)
    return load_catalog_document(document, base_dir=path.parent, source_path=str(path))


def load_catalog_document(
    document: Any,
    *,
    base_dir: str | Path = ".",
    source_path: str = "<memory>",
) -> Catalog:
    """Validate a complete catalog document before exposing any object."""

    if not isinstance(document, dict):
        raise SchemaError("catalog root must be an object", "$")

    name = document.get("name", "local-joinlab")
    if not isinstance(name, str) or not name.strip():
        raise SchemaError("catalog name must be a non-empty string", "$.name")

    schema_version = document.get("schema_version")
    data_version = document.get("data_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise SchemaError("schema_version must be a positive integer", "$.schema_version")
    if isinstance(data_version, bool) or not isinstance(data_version, int) or data_version < 1:
        raise SchemaError("data_version must be a positive integer", "$.data_version")

    skew_limit_default = document.get("skew_limit", 0.75)
    if (
        isinstance(skew_limit_default, bool)
        or not isinstance(skew_limit_default, (int, float))
        or not math.isfinite(float(skew_limit_default))
        or not (0 < float(skew_limit_default) <= 1)
    ):
        raise SchemaError("skew_limit must be in (0, 1]", "$.skew_limit")
    skew_limit_default = float(skew_limit_default)

    raw_tables = document.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise SchemaError("tables must be a non-empty array", "$.tables")

    table_defs: list[TableDef] = []
    table_rows: list[tuple[tuple[Any, ...], ...]] = []
    table_ndvs: list[tuple[int, ...]] = []
    seen_tables: dict[str, int] = {}
    base = Path(base_dir).expanduser().resolve()

    for table_index, raw_table in enumerate(raw_tables):
        table_path = f"$.tables[{table_index}]"
        if not isinstance(raw_table, dict):
            raise SchemaError("table definition must be an object", table_path)

        table_name = raw_table.get("name")
        if not isinstance(table_name, str) or not table_name.strip():
            raise SchemaError("table name must be a non-empty string", f"{table_path}.name")
        if not table_name.replace("_", "").isalnum():
            raise SchemaError("table name must contain only letters, digits, or underscore", f"{table_path}.name")
        table_key = table_name.lower()
        if table_key in seen_tables:
            raise SchemaError(
                f"duplicate table name {table_name!r}",
                f"{table_path}.name",
                first_index=seen_tables[table_key],
            )
        seen_tables[table_key] = table_index

        row_count = raw_table.get("row_count")
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            raise SchemaError("row_count must be a non-negative integer", f"{table_path}.row_count")

        skew = raw_table.get("skew", False)
        if not isinstance(skew, bool):
            raise SchemaError("skew must be a boolean", f"{table_path}.skew")

        raw_columns = raw_table.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise SchemaError("columns must be a non-empty array", f"{table_path}.columns")

        columns: list[ColumnDef] = []
        seen_columns: dict[str, int] = {}
        for column_index, raw_column in enumerate(raw_columns):
            column_path = f"{table_path}.columns[{column_index}]"
            if not isinstance(raw_column, dict):
                raise SchemaError("column definition must be an object", column_path)
            column_name = raw_column.get("name")
            if not isinstance(column_name, str) or not column_name.strip():
                raise SchemaError("column name must be a non-empty string", f"{column_path}.name")
            if not column_name.replace("_", "").isalnum():
                raise SchemaError(
                    "column name must contain only letters, digits, or underscore",
                    f"{column_path}.name",
                )
            column_key = column_name.lower()
            if column_key in seen_columns:
                raise SchemaError(
                    f"duplicate column name {column_name!r}",
                    f"{column_path}.name",
                    first_index=seen_columns[column_key],
                )
            seen_columns[column_key] = column_index
            column_type = normalize_type(raw_column.get("type"), f"{column_path}.type")
            nullable = raw_column.get("nullable", True)
            if not isinstance(nullable, bool):
                raise SchemaError("nullable must be a boolean", f"{column_path}.nullable")
            ndv = raw_column.get("ndv")
            if ndv is not None and (
                isinstance(ndv, bool) or not isinstance(ndv, int) or ndv < 1
            ):
                raise SchemaError("ndv must be a positive integer when present", f"{column_path}.ndv")
            columns.append(ColumnDef(column_name, column_key, column_type, nullable, ndv))

        skew_column_raw = raw_table.get("skew_column")
        skew_column: str | None = None
        if skew_column_raw is not None:
            if not isinstance(skew_column_raw, str) or not skew_column_raw.strip():
                raise SchemaError("skew_column must be a non-empty string", f"{table_path}.skew_column")
            skew_column = skew_column_raw.lower()
            if skew_column not in seen_columns:
                raise SchemaError("skew_column does not exist", f"{table_path}.skew_column")

        skew_limit_raw = raw_table.get("skew_limit")
        skew_limit: float | None = None
        if skew_limit_raw is not None:
            if (
                isinstance(skew_limit_raw, bool)
                or not isinstance(skew_limit_raw, (int, float))
                or not math.isfinite(float(skew_limit_raw))
                or not (0 < float(skew_limit_raw) <= 1)
            ):
                raise SchemaError("skew_limit must be in (0, 1]", f"{table_path}.skew_limit")
            skew_limit = float(skew_limit_raw)

        data_file_raw = raw_table.get("data_file")
        data_file: str | None = None
        embedded_rows: Any = None
        if "rows" in raw_table:
            embedded_rows = raw_table.get("rows")
        else:
            if not isinstance(data_file_raw, str) or not data_file_raw.strip():
                raise SchemaError("data_file must be a non-empty string", f"{table_path}.data_file")
            data_file = data_file_raw
            data_path = (base / data_file).resolve()
            data_doc = _read_json(data_path, path_label=f"{table_path}.data_file")
            if isinstance(data_doc, list):
                embedded_rows = data_doc
            elif isinstance(data_doc, dict):
                if data_doc.get("table") is not None:
                    actual_table = data_doc.get("table")
                    if not isinstance(actual_table, str) or actual_table.lower() != table_key:
                        raise SchemaError(
                            "data file table does not match table definition",
                            f"{table_path}.data_file",
                            actual_table=actual_table,
                        )
                embedded_rows = data_doc.get("rows")
            else:
                raise SchemaError("data file root must be an object or array", f"{table_path}.data_file")

        if not isinstance(embedded_rows, list):
            raise SchemaError("sample rows must be an array", f"{table_path}.rows")
        if len(embedded_rows) != row_count:
            raise SchemaError(
                f"sample row count {len(embedded_rows)} does not match declared row_count {row_count}",
                f"{table_path}.row_count",
                expected=row_count,
                actual=len(embedded_rows),
            )

        column_keys = [column.key for column in columns]
        rows: list[tuple[Any, ...]] = []
        distinct_sets: list[set[Any]] = [set() for _ in columns]
        for row_index, raw_row in enumerate(embedded_rows):
            row_path = f"{table_path}.rows[{row_index}]"
            if not isinstance(raw_row, dict):
                raise SchemaError("sample row must be an object", row_path)
            unknown = sorted(set(raw_row.keys()) - set(column_keys))
            if unknown:
                raise SchemaError(
                    f"sample row contains unknown columns: {unknown}",
                    f"{row_path}.{unknown[0]}",
                )
            values: list[Any] = []
            for column in columns:
                if column.key not in raw_row:
                    raise SchemaError(
                        f"sample row is missing column {column.key!r}",
                        f"{row_path}.{column.key}",
                    )
                value = validate_value(
                    raw_row[column.key],
                    column.type,
                    f"{row_path}.{column.key}",
                    column.nullable,
                )
                values.append(value)
                if value is not None:
                    distinct_sets[columns.index(column)].add(value)
            rows.append(tuple(values))

        if skew_column is not None and rows:
            position = column_keys.index(skew_column)
            frequencies: dict[Any, int] = {}
            for row in rows:
                value = row[position]
                if value is not None:
                    frequencies[value] = frequencies.get(value, 0) + 1
            max_frequency = max(frequencies.values(), default=0)
            ratio = max_frequency / len(rows)
            effective_limit = skew_limit if skew_limit is not None else skew_limit_default
            if ratio > effective_limit:
                raise SkewError(
                    f"sample skew ratio {ratio:.6f} exceeds limit {effective_limit:.6f}",
                    f"{table_path}.rows",
                    column=skew_column,
                    ratio=round(ratio, 6),
                    limit=effective_limit,
                )

        actual_ndv = tuple(
            column.ndv if column.ndv is not None else max(1, len(distinct_sets[index]))
            for index, column in enumerate(columns)
        )
        table_defs.append(
            TableDef(
                name=table_name,
                key=table_key,
                row_count=row_count,
                skew=skew,
                data_file=data_file,
                columns=tuple(columns),
                skew_column=skew_column,
                skew_limit=skew_limit,
            )
        )
        table_rows.append(tuple(rows))
        table_ndvs.append(actual_ndv)

    data_items = tuple(
        TableData(table_defs[index].key, table_rows[index], table_ndvs[index])
        for index in range(len(table_defs))
    )
    return Catalog(
        name=name.strip(),
        schema_version=schema_version,
        data_version=data_version,
        tables=tuple(table_defs),
        data=data_items,
        skew_limit_default=skew_limit_default,
        source_path=source_path,
    )
