"""Declarative columnar catalog loading with all-or-nothing validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .codes import E_DATA
from .errors import PipeError
from .util import canonical_json, sha256_json, sha256_text

INPUT_FORMAT = "colpipe.input.v1"
SUPPORTED_TYPES = frozenset({"int64", "float64", "bool", "string"})
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Settings:
    parallelism: int = 4
    memory_budget_bytes: int = 1_048_576
    min_rows_per_task: int = 128

    @classmethod
    def from_json(cls, value: Any, path: str = "$.settings") -> "Settings":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise PipeError(E_DATA, "settings must be an object", path)
        allowed = {"parallelism", "memory_budget_bytes", "min_rows_per_task"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise PipeError(
                E_DATA,
                f"unknown settings field: {unknown[0]}",
                f"{path}.{unknown[0]}",
            )
        parallelism = value.get("parallelism", 4)
        budget = value.get("memory_budget_bytes", 1_048_576)
        min_rows = value.get("min_rows_per_task", 128)
        if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
            raise PipeError(E_DATA, "parallelism must be a positive integer", f"{path}.parallelism")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise PipeError(
                E_DATA,
                "memory_budget_bytes must be a positive integer",
                f"{path}.memory_budget_bytes",
            )
        if isinstance(min_rows, bool) or not isinstance(min_rows, int) or min_rows < 1:
            raise PipeError(
                E_DATA,
                "min_rows_per_task must be a positive integer",
                f"{path}.min_rows_per_task",
            )
        return cls(parallelism, budget, min_rows)

    def to_dict(self) -> dict[str, int]:
        return {
            "parallelism": self.parallelism,
            "memory_budget_bytes": self.memory_budget_bytes,
            "min_rows_per_task": self.min_rows_per_task,
        }


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    values: tuple[Any, ...]
    null_bitmap: tuple[bool, ...]

    @property
    def row_count(self) -> int:
        return len(self.values)

    @property
    def null_count(self) -> int:
        return sum(1 for item in self.null_bitmap if item)

    @property
    def nullable(self) -> bool:
        return self.null_count > 0

    def schema_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable}

    def data_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "values": list(self.values),
            "null_bitmap": list(self.null_bitmap),
        }


@dataclass(frozen=True)
class Table:
    name: str
    schema_version: str
    row_count: int
    columns: tuple[Column, ...]

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    def column_map(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}

    def schema_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "columns": [column.schema_dict() for column in self.columns],
        }

    def data_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "columns": [column.data_dict() for column in self.columns],
        }


@dataclass(frozen=True)
class Dataset:
    session_id: str
    data_version: str
    tables: tuple[Table, ...]

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    @property
    def table_map(self) -> dict[str, Table]:
        return {table.name: table for table in self.tables}

    @property
    def schema_fingerprint(self) -> str:
        return sha256_json([table.schema_dict() for table in self.tables])

    @property
    def data_digest(self) -> str:
        return sha256_json([table.data_dict() for table in self.tables])

    def schema_versions(self) -> dict[str, str]:
        return {table.name: table.schema_version for table in self.tables}

    def data_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "data_version": self.data_version,
            "tables": [table.data_dict() for table in self.tables],
        }


@dataclass(frozen=True)
class PipelineRequest:
    dataset: Dataset
    query_text: str
    query: Any
    settings: Settings

    def input_dict(self) -> dict[str, Any]:
        return {
            "format": INPUT_FORMAT,
            "session_id": self.dataset.session_id,
            "data_version": self.dataset.data_version,
            "tables": [table.data_dict() for table in self.dataset.tables],
            "query": self.query_text,
            "settings": self.settings.to_dict(),
        }


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipeError(E_DATA, "expected an object", path)
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise PipeError(E_DATA, "expected an array", path)
    return value


def _require_string(value: Any, path: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise PipeError(E_DATA, "expected a non-empty string", path)
    if identifier and not IDENTIFIER_RE.fullmatch(value):
        raise PipeError(
            E_DATA,
            "identifier must match [A-Za-z_][A-Za-z0-9_]*",
            path,
        )
    return value


def _normalise_value(value: Any, column_type: str, path: str) -> Any:
    if value is None:
        return None
    if column_type == "int64":
        if isinstance(value, bool) or not isinstance(value, int):
            raise PipeError(E_DATA, "int64 value must be a JSON integer", path)
        if value < -(2**63) or value > 2**63 - 1:
            raise PipeError(E_DATA, "int64 value is outside the signed 64-bit range", path)
        return value
    if column_type == "float64":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PipeError(E_DATA, "float64 value must be a JSON number", path)
        converted = float(value)
        if not math.isfinite(converted):
            raise PipeError(E_DATA, "float64 value must be finite", path)
        return converted
    if column_type == "bool":
        if not isinstance(value, bool):
            raise PipeError(E_DATA, "bool value must be true or false", path)
        return value
    if column_type == "string":
        if not isinstance(value, str):
            raise PipeError(E_DATA, "string value must be a JSON string", path)
        return value
    raise PipeError(E_DATA, f"unsupported column type: {column_type}", path)


def _parse_column(value: Any, row_count: int, path: str) -> Column:
    obj = _require_object(value, path)
    name = _require_string(obj.get("name"), f"{path}.name", identifier=True)
    column_type = _require_string(obj.get("type"), f"{path}.type")
    if column_type not in SUPPORTED_TYPES:
        raise PipeError(
            E_DATA,
            f"unsupported column type {column_type!r}; expected one of {sorted(SUPPORTED_TYPES)}",
            f"{path}.type",
        )
    values_raw = _require_list(obj.get("values"), f"{path}.values")
    bitmap_raw = _require_list(obj.get("null_bitmap"), f"{path}.null_bitmap")
    if len(values_raw) != row_count:
        raise PipeError(
            E_DATA,
            f"values length {len(values_raw)} does not match row_count {row_count}",
            f"{path}.values",
        )
    if len(bitmap_raw) != row_count:
        raise PipeError(
            E_DATA,
            f"null bitmap length {len(bitmap_raw)} does not match row_count {row_count}",
            f"{path}.null_bitmap",
        )
    bitmap: list[bool] = []
    for index, item in enumerate(bitmap_raw):
        if not isinstance(item, bool):
            raise PipeError(E_DATA, "null bitmap entries must be booleans", f"{path}.null_bitmap[{index}]")
        bitmap.append(item)
    values: list[Any] = []
    for index, raw in enumerate(values_raw):
        value_path = f"{path}.values[{index}]"
        if bitmap[index]:
            if raw is not None:
                raise PipeError(
                    E_DATA,
                    "a null-bitmap position must carry JSON null",
                    value_path,
                )
            values.append(None)
        else:
            if raw is None:
                raise PipeError(
                    E_DATA,
                    "JSON null requires the null bitmap to be true",
                    value_path,
                )
            values.append(_normalise_value(raw, column_type, value_path))
    return Column(name, column_type, tuple(values), tuple(bitmap))


def _parse_table(value: Any, path: str) -> Table:
    obj = _require_object(value, path)
    name = _require_string(obj.get("name"), f"{path}.name", identifier=True)
    schema_version = _require_string(obj.get("schema_version"), f"{path}.schema_version")
    columns_raw = _require_list(obj.get("columns"), f"{path}.columns")
    if not columns_raw:
        raise PipeError(E_DATA, "table must declare at least one column", f"{path}.columns")

    lengths: list[int] = []
    for index, column in enumerate(columns_raw):
        col_obj = _require_object(column, f"{path}.columns[{index}]")
        values = col_obj.get("values")
        if isinstance(values, list):
            lengths.append(len(values))
    if "row_count" in obj:
        row_count_raw = obj.get("row_count")
        if isinstance(row_count_raw, bool) or not isinstance(row_count_raw, int) or row_count_raw < 0:
            raise PipeError(E_DATA, "row_count must be a non-negative integer", f"{path}.row_count")
        row_count = row_count_raw
    else:
        if not lengths:
            raise PipeError(E_DATA, "row_count is required for a table without columns", f"{path}.row_count")
        row_count = lengths[0]
    for index, length in enumerate(lengths):
        if length != row_count:
            raise PipeError(
                E_DATA,
                f"column length {length} does not match row_count {row_count}",
                f"{path}.columns[{index}].values",
            )

    columns: list[Column] = []
    seen: set[str] = set()
    for index, raw_column in enumerate(columns_raw):
        column = _parse_column(raw_column, row_count, f"{path}.columns[{index}]")
        if column.name in seen:
            raise PipeError(E_DATA, f"duplicate column name: {column.name}", f"{path}.columns[{index}].name")
        seen.add(column.name)
        columns.append(column)
    return Table(name, schema_version, row_count, tuple(columns))


def load_pipeline_dict(payload: Any, *, source_path: str = "$") -> PipelineRequest:
    """Validate a complete pipeline declaration and return it atomically."""

    from .query import bind_query, parse_query

    obj = _require_object(payload, source_path)
    required = {"format", "session_id", "data_version", "tables", "query"}
    missing = sorted(required - set(obj))
    if missing:
        raise PipeError(E_DATA, f"missing required field: {missing[0]}", f"{source_path}.{missing[0]}")
    fmt = obj.get("format")
    if fmt != INPUT_FORMAT:
        raise PipeError(E_DATA, f"unsupported input format {fmt!r}", f"{source_path}.format")
    session_id = _require_string(obj.get("session_id"), f"{source_path}.session_id")
    data_version = _require_string(obj.get("data_version"), f"{source_path}.data_version")
    settings = Settings.from_json(obj.get("settings"), f"{source_path}.settings")

    tables_raw = _require_list(obj.get("tables"), f"{source_path}.tables")
    if not tables_raw:
        raise PipeError(E_DATA, "at least one table is required", f"{source_path}.tables")
    tables: list[Table] = []
    seen_tables: set[str] = set()
    for index, raw_table in enumerate(tables_raw):
        table = _parse_table(raw_table, f"{source_path}.tables[{index}]")
        if table.name in seen_tables:
            raise PipeError(
                E_DATA,
                f"duplicate table name: {table.name}",
                f"{source_path}.tables[{index}].name",
            )
        seen_tables.add(table.name)
        tables.append(table)

    query_text = _require_string(obj.get("query"), f"{source_path}.query")
    dataset = Dataset(session_id, data_version, tuple(tables))
    query = parse_query(query_text, path=f"{source_path}.query")
    bound = bind_query(query, dataset.table_map, path=f"{source_path}.query")
    return PipelineRequest(dataset, query_text, bound, settings)


def load_pipeline_input(path: str | Path) -> PipelineRequest:
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipeError(E_DATA, f"cannot read input file: {exc}", "$") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipeError(
            E_DATA,
            f"invalid JSON: {exc.msg}",
            "$",
            {"line": exc.lineno, "column": exc.colno, "position": exc.pos},
        ) from exc
    return load_pipeline_dict(payload, source_path="$")


def validate_dataset_dict(payload: Mapping[str, Any], *, session_id: str, data_version: str) -> Dataset:
    """Validate an already separated dataset object used by snapshots."""

    tiles = payload.get("tables")
    tables: list[Table] = []
    for index, raw_table in enumerate(_require_list(tiles, "$.dataset.tables")):
        table = _parse_table(raw_table, f"$.dataset.tables[{index}]")
        if any(existing.name == table.name for existing in tables):
            raise PipeError(E_DATA, f"duplicate table name: {table.name}", f"$.dataset.tables[{index}].name")
        tables.append(table)
    if not tables:
        raise PipeError(E_DATA, "at least one table is required", "$.dataset.tables")
    return Dataset(session_id, data_version, tuple(tables))


def schema_description(dataset: Dataset) -> list[dict[str, Any]]:
    return [table.schema_dict() for table in dataset.tables]


def stable_dataset_hash(dataset: Dataset) -> str:
    return sha256_text(canonical_json(dataset.data_dict()))

