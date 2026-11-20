"""Manifest parsing and static business validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from .canonical import (
    ROUNDING_MODES,
    canonical_json,
    fixed_units,
    normalize_value,
    sha256_json,
)
from .errors import (
    BusinessError,
    E_BAD_INDEX_DECL,
    E_BAD_OP,
    E_BAD_SCHEMA_DECL,
    E_CLOCK_ROLLBACK,
    E_JSON_MALFORMED,
    E_MANIFEST_MISSING,
)


COLUMN_TYPES = {"INT", "TEXT", "BOOL", "DECIMAL"}


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    unique_constraints: tuple[tuple[str, ...], ...]

    @property
    def column_map(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class IndexDeclaration:
    name: str
    columns: tuple[str, ...]
    unique: bool
    covering: tuple[str, ...]
    covering_query: bool


@dataclass(frozen=True)
class QueryTemplate:
    name: str
    index: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class MaintenanceConfig:
    page_capacity: int
    split_factor: float
    max_pending_items: int
    max_touched_pages: int
    per_index_thresholds: dict[str, dict[str, int]]
    fixed_scale: int
    rounding: str
    float_tolerance: str
    tolerance_units: int
    clock_tolerance_ms: int
    logical_start_ms: int
    clock_steps_ms: tuple[int, ...]
    rebuild_window_ms: int
    max_tree_height: int


@dataclass(frozen=True)
class Operation:
    seq: int
    op_type: str
    pk: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    content_digest: str
    clock_ms: int

    def digest_payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.op_type,
            "pk": self.pk,
            "before": self.before,
            "after": self.after,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.digest_payload()
        result["content_digest"] = self.content_digest
        result["clock_ms"] = self.clock_ms
        return result


@dataclass(frozen=True)
class Manifest:
    format_version: int
    sample_id: str
    schema: TableSchema
    indexes: tuple[IndexDeclaration, ...]
    query_templates: tuple[QueryTemplate, ...]
    config: MaintenanceConfig
    operations: tuple[Operation, ...]
    sample_sha256: str

    @property
    def index_map(self) -> dict[str, IndexDeclaration]:
        return {index.name: index for index in self.indexes}

    @property
    def query_map(self) -> dict[str, QueryTemplate]:
        return {query.name: query for query in self.query_templates}

    def to_dict(self) -> dict[str, Any]:
        config = {
            "page_capacity": self.config.page_capacity,
            "split_factor": self.config.split_factor,
            "rebuild_threshold": {
                "max_pending_items": self.config.max_pending_items,
                "max_touched_pages": self.config.max_touched_pages,
            },
            "fixed_scale": self.config.fixed_scale,
            "rounding": self.config.rounding,
            "float_tolerance": self.config.float_tolerance,
            "clock_tolerance_ms": self.config.clock_tolerance_ms,
            "logical_start_ms": self.config.logical_start_ms,
            "clock_steps_ms": list(self.config.clock_steps_ms),
            "rebuild_window_ms": self.config.rebuild_window_ms,
            "max_tree_height": self.config.max_tree_height,
        }
        if self.config.per_index_thresholds:
            config["rebuild_threshold"]["per_index"] = self.config.per_index_thresholds
        return {
            "format_version": self.format_version,
            "sample_id": self.sample_id,
            "schema": {
                "name": self.schema.name,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "nullable": column.nullable,
                    }
                    for column in self.schema.columns
                ],
                "primary_key": list(self.schema.primary_key),
                "unique_constraints": [
                    list(constraint) for constraint in self.schema.unique_constraints
                ],
            },
            "indexes": [
                {
                    "name": index.name,
                    "columns": list(index.columns),
                    "unique": index.unique,
                    "covering": list(index.covering),
                    "covering_query": index.covering_query,
                }
                for index in self.indexes
            ],
            "query_templates": [
                {
                    "name": query.name,
                    "index": query.index,
                    "columns": list(query.columns),
                }
                for query in self.query_templates
            ],
            "operations": [operation.to_dict() for operation in self.operations],
            "config": config,
        }


def _business(code: str, message: str, **details: Any) -> BusinessError:
    return BusinessError(code, message, details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value: Any, message: str, **details: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _business(E_BAD_OP, message, **details)
    return value


def _validate_name(value: Any, *, code: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _business(code, "name must be a non-empty string", field=field)
    if any(char.isspace() for char in value):
        raise _business(code, "name must not contain whitespace", field=field, value=value)
    return value


def _parse_schema(raw: Any) -> TableSchema:
    if not isinstance(raw, dict):
        raise _business(E_BAD_SCHEMA_DECL, "schema must be an object")
    name = _validate_name(raw.get("name"), code=E_BAD_SCHEMA_DECL, field="schema.name")
    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise _business(
            E_BAD_SCHEMA_DECL,
            "schema.columns must be a non-empty array",
            field="schema.columns",
        )
    columns: list[Column] = []
    seen_columns: set[str] = set()
    for position, raw_column in enumerate(columns_raw):
        if not isinstance(raw_column, dict):
            raise _business(
                E_BAD_SCHEMA_DECL,
                "column declaration must be an object",
                position=position,
            )
        column_name = _validate_name(
            raw_column.get("name"),
            code=E_BAD_SCHEMA_DECL,
            field=f"schema.columns[{position}].name",
        )
        if column_name in seen_columns:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "duplicate column name",
                column=column_name,
            )
        seen_columns.add(column_name)
        column_type = raw_column.get("type")
        if column_type not in COLUMN_TYPES:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "unsupported column type",
                column=column_name,
                type=column_type,
                allowed=sorted(COLUMN_TYPES),
            )
        nullable = raw_column.get("nullable", False)
        if not isinstance(nullable, bool):
            raise _business(
                E_BAD_SCHEMA_DECL,
                "column nullable flag must be boolean",
                column=column_name,
            )
        columns.append(Column(column_name, column_type, nullable))

    column_map = {column.name: column for column in columns}
    primary_key_raw = raw.get("primary_key")
    if not isinstance(primary_key_raw, list) or not primary_key_raw:
        raise _business(
            E_BAD_SCHEMA_DECL,
            "primary_key must be a non-empty array",
            field="schema.primary_key",
        )
    primary_key = tuple(primary_key_raw)
    if len(set(primary_key)) != len(primary_key):
        raise _business(
            E_BAD_SCHEMA_DECL,
            "primary_key contains duplicate columns",
            primary_key=list(primary_key),
        )
    for column_name in primary_key:
        if not isinstance(column_name, str) or column_name not in column_map:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "primary key column does not exist",
                column=column_name,
            )
        if column_map[column_name].nullable:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "primary key columns must be non-nullable",
                column=column_name,
            )

    unique_raw = raw.get("unique_constraints", [])
    if not isinstance(unique_raw, list):
        raise _business(
            E_BAD_SCHEMA_DECL,
            "unique_constraints must be an array",
            field="schema.unique_constraints",
        )
    unique_constraints: list[tuple[str, ...]] = []
    seen_unique: set[tuple[str, ...]] = set()
    for position, constraint_raw in enumerate(unique_raw):
        if not isinstance(constraint_raw, list) or not constraint_raw:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "unique constraint must be a non-empty array",
                position=position,
            )
        constraint = tuple(constraint_raw)
        if len(set(constraint)) != len(constraint):
            raise _business(
                E_BAD_SCHEMA_DECL,
                "unique constraint contains duplicate columns",
                position=position,
                columns=list(constraint),
            )
        if constraint in seen_unique:
            raise _business(
                E_BAD_SCHEMA_DECL,
                "duplicate unique constraint",
                columns=list(constraint),
            )
        seen_unique.add(constraint)
        for column_name in constraint:
            if not isinstance(column_name, str) or column_name not in column_map:
                raise _business(
                    E_BAD_SCHEMA_DECL,
                    "unique constraint column does not exist",
                    position=position,
                    column=column_name,
                )
            if column_map[column_name].nullable:
                raise _business(
                    E_BAD_SCHEMA_DECL,
                    "unique constraint columns must be non-nullable",
                    position=position,
                    column=column_name,
                )
        unique_constraints.append(constraint)

    return TableSchema(
        name=name,
        columns=tuple(columns),
        primary_key=primary_key,
        unique_constraints=tuple(unique_constraints),
    )


def _parse_indexes(
    raw_indexes: Any,
    schema: TableSchema,
) -> tuple[IndexDeclaration, ...]:
    if not isinstance(raw_indexes, list) or not raw_indexes:
        raise _business(
            E_BAD_INDEX_DECL,
            "indexes must be a non-empty array",
            field="indexes",
        )
    result: list[IndexDeclaration] = []
    seen_names: set[str] = set()
    for position, raw_index in enumerate(raw_indexes):
        if not isinstance(raw_index, dict):
            raise _business(
                E_BAD_INDEX_DECL,
                "index declaration must be an object",
                position=position,
            )
        name = _validate_name(
            raw_index.get("name"),
            code=E_BAD_INDEX_DECL,
            field=f"indexes[{position}].name",
        )
        if name == schema.name or name in seen_names:
            raise _business(
                E_BAD_INDEX_DECL,
                "index name must be unique and differ from the table name",
                index=name,
            )
        seen_names.add(name)
        columns_raw = raw_index.get("columns")
        if not isinstance(columns_raw, list) or not columns_raw:
            raise _business(
                E_BAD_INDEX_DECL,
                "index columns must be a non-empty array",
                index=name,
            )
        if not all(isinstance(column, str) for column in columns_raw):
            raise _business(
                E_BAD_INDEX_DECL,
                "index columns must be strings",
                index=name,
            )
        columns = tuple(columns_raw)
        if len(set(columns)) != len(columns):
            raise _business(
                E_BAD_INDEX_DECL,
                "index columns contain duplicates",
                index=name,
                columns=list(columns),
            )
        schema_columns = set(schema.column_names)
        for column in columns:
            if column not in schema_columns:
                raise _business(
                    E_BAD_INDEX_DECL,
                    "index column does not exist",
                    index=name,
                    column=column,
                )
        unique = raw_index.get("unique")
        if not isinstance(unique, bool):
            raise _business(
                E_BAD_INDEX_DECL,
                "index unique flag must be boolean",
                index=name,
            )
        covering_raw = raw_index.get("covering", [])
        if not isinstance(covering_raw, list) or not all(
            isinstance(column, str) for column in covering_raw
        ):
            raise _business(
                E_BAD_INDEX_DECL,
                "index covering columns must be an array of strings",
                index=name,
            )
        covering = tuple(covering_raw)
        if len(set(covering)) != len(covering):
            raise _business(
                E_BAD_INDEX_DECL,
                "index covering columns contain duplicates",
                index=name,
            )
        for column in covering:
            if column not in schema_columns:
                raise _business(
                    E_BAD_INDEX_DECL,
                    "covering column does not exist",
                    index=name,
                    column=column,
                )
            if column in columns:
                raise _business(
                    E_BAD_INDEX_DECL,
                    "covering columns must not duplicate key columns",
                    index=name,
                    column=column,
                )
        covering_query = raw_index.get("covering_query", False)
        if not isinstance(covering_query, bool):
            raise _business(
                E_BAD_INDEX_DECL,
                "covering_query must be boolean",
                index=name,
            )
        if covering_query and not covering:
            raise _business(
                E_BAD_INDEX_DECL,
                "covering_query requires covering columns",
                index=name,
            )
        result.append(
            IndexDeclaration(
                name=name,
                columns=columns,
                unique=unique,
                covering=covering,
                covering_query=covering_query,
            )
        )
    return tuple(result)


def _parse_queries(
    raw_queries: Any,
    schema: TableSchema,
    indexes: tuple[IndexDeclaration, ...],
) -> tuple[QueryTemplate, ...]:
    if raw_queries is None:
        raw_queries = []
    if not isinstance(raw_queries, list):
        raise _business(
            E_BAD_INDEX_DECL,
            "query_templates must be an array",
            field="query_templates",
        )
    index_map = {index.name: index for index in indexes}
    seen: set[str] = set()
    result: list[QueryTemplate] = []
    for position, raw_query in enumerate(raw_queries):
        if not isinstance(raw_query, dict):
            raise _business(
                E_BAD_INDEX_DECL,
                "query template must be an object",
                position=position,
            )
        name = _validate_name(
            raw_query.get("name"),
            code=E_BAD_INDEX_DECL,
            field=f"query_templates[{position}].name",
        )
        if name in seen:
            raise _business(E_BAD_INDEX_DECL, "duplicate query template name", query=name)
        seen.add(name)
        index_name = raw_query.get("index")
        if index_name not in index_map:
            raise _business(
                E_BAD_INDEX_DECL,
                "query template references an unknown index",
                query=name,
                index=index_name,
            )
        columns_raw = raw_query.get("columns")
        if not isinstance(columns_raw, list) or not all(
            isinstance(column, str) for column in columns_raw
        ):
            raise _business(
                E_BAD_INDEX_DECL,
                "query columns must be an array of strings",
                query=name,
            )
        columns = tuple(columns_raw)
        declared = set(index_map[index_name].columns) | set(index_map[index_name].covering)
        missing = [column for column in columns if column not in declared]
        if missing:
            raise _business(
                E_BAD_INDEX_DECL,
                "query references columns not supplied by the index",
                query=name,
                missing=missing,
            )
        schema_columns = set(schema.column_names)
        invalid = [column for column in columns if column not in schema_columns]
        if invalid:
            raise _business(
                E_BAD_INDEX_DECL,
                "query references unknown columns",
                query=name,
                missing=invalid,
            )
        result.append(QueryTemplate(name=name, index=index_name, columns=columns))
    return tuple(result)


def _parse_config(
    raw: Any,
    *,
    operation_count: int,
    index_names: set[str],
) -> MaintenanceConfig:
    if not isinstance(raw, dict):
        raise _business(E_BAD_OP, "config must be an object", field="config")
    page_capacity = raw.get("page_capacity")
    if not _is_int(page_capacity) or page_capacity < 3:
        raise _business(
            E_BAD_OP,
            "page_capacity must be an integer >= 3",
            field="config.page_capacity",
            value=page_capacity,
        )
    split_factor = raw.get("split_factor")
    if isinstance(split_factor, bool) or not isinstance(split_factor, (int, float)):
        raise _business(
            E_BAD_OP,
            "split_factor must be numeric",
            field="config.split_factor",
        )
    split_factor = float(split_factor)
    if not 0.3 <= split_factor <= 0.8:
        raise _business(
            E_BAD_OP,
            "split_factor must be between 0.3 and 0.8",
            value=split_factor,
        )
    threshold_raw = raw.get("rebuild_threshold")
    if not isinstance(threshold_raw, dict):
        raise _business(
            E_BAD_OP,
            "rebuild_threshold must be an object",
            field="config.rebuild_threshold",
        )
    max_pending_items = threshold_raw.get("max_pending_items")
    max_touched_pages = threshold_raw.get("max_touched_pages")
    if not _is_int(max_pending_items) or max_pending_items < 0:
        raise _business(
            E_BAD_OP,
            "max_pending_items must be a non-negative integer",
            value=max_pending_items,
        )
    if not _is_int(max_touched_pages) or max_touched_pages < 0:
        raise _business(
            E_BAD_OP,
            "max_touched_pages must be a non-negative integer",
            value=max_touched_pages,
        )
    per_index_raw = threshold_raw.get("per_index", {})
    if not isinstance(per_index_raw, dict):
        raise _business(
            E_BAD_OP,
            "rebuild_threshold.per_index must be an object",
        )
    per_index: dict[str, dict[str, int]] = {}
    for index_name, override in per_index_raw.items():
        if index_name not in index_names:
            raise _business(
                E_BAD_OP,
                "per-index rebuild threshold references an unknown index",
                index=index_name,
            )
        if not isinstance(override, dict):
            raise _business(
                E_BAD_OP,
                "per-index rebuild threshold must be an object",
                index=index_name,
            )
        parsed_override: dict[str, int] = {}
        for key, value in override.items():
            if key not in {"max_pending_items", "max_touched_pages"}:
                raise _business(
                    E_BAD_OP,
                    "unknown per-index rebuild threshold key",
                    index=index_name,
                    key=key,
                )
            if not _is_int(value) or value < 0:
                raise _business(
                    E_BAD_OP,
                    "per-index rebuild threshold must be non-negative",
                    index=index_name,
                    key=key,
                )
            parsed_override[key] = value
        per_index[index_name] = parsed_override

    fixed_scale = raw.get("fixed_scale")
    if not _is_int(fixed_scale) or not 0 <= fixed_scale <= 9:
        raise _business(
            E_BAD_OP,
            "fixed_scale must be an integer between 0 and 9",
            value=fixed_scale,
        )
    rounding = raw.get("rounding", "HALF_UP")
    if rounding not in ROUNDING_MODES:
        raise _business(
            E_BAD_OP,
            "unsupported rounding mode",
            value=rounding,
            allowed=sorted(ROUNDING_MODES),
        )
    float_tolerance = raw.get("float_tolerance", "0")
    try:
        tolerance_decimal = Decimal(str(float_tolerance))
        if tolerance_decimal < 0:
            raise ValueError
    except (ValueError, ArithmeticError) as exc:
        raise _business(
            E_BAD_OP,
            "float_tolerance must be a non-negative decimal",
            value=float_tolerance,
        ) from exc
    tolerance_units = fixed_units(tolerance_decimal, fixed_scale, rounding)
    clock_tolerance = raw.get("clock_tolerance_ms", 0)
    if not _is_int(clock_tolerance) or clock_tolerance < 0:
        raise _business(
            E_BAD_OP,
            "clock_tolerance_ms must be a non-negative integer",
            value=clock_tolerance,
        )
    logical_start = raw.get("logical_start_ms")
    if not _is_int(logical_start):
        raise _business(
            E_BAD_OP,
            "logical_start_ms must be an integer",
            value=logical_start,
        )
    steps_raw = raw.get("clock_steps_ms")
    if not isinstance(steps_raw, list) or len(steps_raw) != operation_count:
        raise _business(
            E_BAD_OP,
            "clock_steps_ms must contain one step per operation",
            expected=operation_count,
            actual=len(steps_raw) if isinstance(steps_raw, list) else None,
        )
    if not all(_is_int(step) for step in steps_raw):
        raise _business(E_BAD_OP, "clock_steps_ms entries must be integers")
    rebuild_window = raw.get("rebuild_window_ms", 0)
    if not _is_int(rebuild_window) or rebuild_window < 0:
        raise _business(
            E_BAD_OP,
            "rebuild_window_ms must be a non-negative integer",
            value=rebuild_window,
        )
    max_tree_height = raw.get("max_tree_height", 8)
    if not _is_int(max_tree_height) or max_tree_height < 1:
        raise _business(
            E_BAD_OP,
            "max_tree_height must be a positive integer",
            value=max_tree_height,
        )
    return MaintenanceConfig(
        page_capacity=page_capacity,
        split_factor=split_factor,
        max_pending_items=max_pending_items,
        max_touched_pages=max_touched_pages,
        per_index_thresholds=per_index,
        fixed_scale=fixed_scale,
        rounding=rounding,
        float_tolerance=format(tolerance_decimal, "f"),
        tolerance_units=tolerance_units,
        clock_tolerance_ms=clock_tolerance,
        logical_start_ms=logical_start,
        clock_steps_ms=tuple(steps_raw),
        rebuild_window_ms=rebuild_window,
        max_tree_height=max_tree_height,
    )


def _normalize_row(
    raw: Any,
    schema: TableSchema,
    config: MaintenanceConfig,
    *,
    field: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _business(E_BAD_OP, "row must be an object", field=field)
    unknown = sorted(set(raw) - set(schema.column_names))
    if unknown:
        raise _business(
            E_BAD_OP,
            "row contains unknown columns",
            field=field,
            columns=unknown,
        )
    normalized: dict[str, Any] = {}
    for column in schema.columns:
        if column.name not in raw:
            if column.nullable:
                normalized[column.name] = None
                continue
            raise _business(
                E_BAD_OP,
                "row is missing a non-nullable column",
                field=field,
                column=column.name,
            )
        value = raw[column.name]
        if value is None:
            if not column.nullable:
                raise _business(
                    E_BAD_OP,
                    "non-nullable column received null",
                    field=field,
                    column=column.name,
                )
            normalized[column.name] = None
            continue
        normalized[column.name] = normalize_value(
            value,
            column.type,
            scale=config.fixed_scale,
            rounding=config.rounding,
            field=f"{field}.{column.name}",
        )
    return normalized


def _normalize_pk(
    raw: Any,
    schema: TableSchema,
    config: MaintenanceConfig,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        if len(schema.primary_key) == 1:
            raw = {schema.primary_key[0]: raw}
        else:
            raise _business(
                E_BAD_OP,
                "composite primary key must be an object",
                field="operation.pk",
            )
    unknown = sorted(set(raw) - set(schema.primary_key))
    if unknown:
        raise _business(
            E_BAD_OP,
            "primary key object contains non-key columns",
            columns=unknown,
        )
    result: dict[str, Any] = {}
    column_map = schema.column_map
    for column_name in schema.primary_key:
        if column_name not in raw:
            raise _business(
                E_BAD_OP,
                "primary key object is missing a key column",
                column=column_name,
            )
        result[column_name] = normalize_value(
            raw[column_name],
            column_map[column_name].type,
            scale=config.fixed_scale,
            rounding=config.rounding,
            field=f"operation.pk.{column_name}",
        )
    return result


def _parse_operations(
    raw_operations: Any,
    schema: TableSchema,
    config: MaintenanceConfig,
) -> tuple[Operation, ...]:
    if not isinstance(raw_operations, list) or not raw_operations:
        raise _business(
            E_BAD_OP,
            "operations must be a non-empty array",
            field="operations",
        )
    operations: list[Operation] = []
    seen_seq: set[int] = set()
    previous_seq: int | None = None
    previous_clock: int | None = None
    for position, raw in enumerate(raw_operations):
        if not isinstance(raw, dict):
            raise _business(
                E_BAD_OP,
                "operation must be an object",
                position=position,
            )
        seq = raw.get("seq")
        if not _is_int(seq) or seq <= 0:
            raise _business(
                E_BAD_OP,
                "operation seq must be a positive integer",
                position=position,
                seq=seq,
            )
        if seq in seen_seq:
            raise _business(E_BAD_OP, "duplicate operation seq", seq=seq)
        if previous_seq is not None and seq <= previous_seq:
            raise _business(
                E_BAD_OP,
                "operation seq must be strictly increasing",
                previous=previous_seq,
                seq=seq,
            )
        seen_seq.add(seq)
        previous_seq = seq
        op_type = raw.get("type")
        if op_type not in {"insert", "delete", "update"}:
            raise _business(
                E_BAD_OP,
                "operation type is not closed over the allowed enum",
                seq=seq,
                type=op_type,
            )
        pk = _normalize_pk(raw.get("pk"), schema, config)
        before_raw = raw.get("before")
        after_raw = raw.get("after")
        if op_type == "insert":
            if before_raw is not None or after_raw is None:
                raise _business(
                    E_BAD_OP,
                    "insert must have before=null and a complete after row",
                    seq=seq,
                )
        elif op_type == "delete":
            if before_raw is None or after_raw is not None:
                raise _business(
                    E_BAD_OP,
                    "delete must have a complete before row and after=null",
                    seq=seq,
                )
        else:
            if before_raw is None or after_raw is None:
                raise _business(
                    E_BAD_OP,
                    "update must have complete before and after rows",
                    seq=seq,
                )
        before = (
            None
            if before_raw is None
            else _normalize_row(before_raw, schema, config, field=f"operations[{position}].before")
        )
        after = (
            None
            if after_raw is None
            else _normalize_row(after_raw, schema, config, field=f"operations[{position}].after")
        )
        for side_name, row in (("before", before), ("after", after)):
            if row is None:
                continue
            row_pk = {column: row[column] for column in schema.primary_key}
            if row_pk != pk:
                raise _business(
                    E_BAD_OP,
                    f"{side_name} row primary key does not match operation pk",
                    seq=seq,
                    expected=pk,
                    actual=row_pk,
                )
        if op_type == "update" and before is not None and after is not None:
            before_pk = {column: before[column] for column in schema.primary_key}
            after_pk = {column: after[column] for column in schema.primary_key}
            if before_pk != after_pk:
                raise _business(
                    E_BAD_OP,
                    "primary key changes are not supported by update operations",
                    seq=seq,
                )
        clock_ms = config.logical_start_ms + sum(config.clock_steps_ms[: position + 1])
        if (
            previous_clock is not None
            and clock_ms < previous_clock - config.clock_tolerance_ms
        ):
            raise _business(
                E_CLOCK_ROLLBACK,
                "logical clock moved backwards beyond tolerance",
                seq=seq,
                previous_clock_ms=previous_clock,
                clock_ms=clock_ms,
                tolerance_ms=config.clock_tolerance_ms,
            )
        previous_clock = clock_ms
        payload = {
            "seq": seq,
            "type": op_type,
            "pk": pk,
            "before": before,
            "after": after,
        }
        content_digest = raw.get("content_digest")
        if not isinstance(content_digest, str) or len(content_digest) != 64:
            raise _business(
                E_BAD_OP,
                "content_digest must be a 64-character SHA-256 hex string",
                seq=seq,
            )
        expected_digest = sha256_json(payload)
        if content_digest.lower() != expected_digest:
            raise _business(
                E_BAD_OP,
                "content_digest does not match the normalized key/content values",
                seq=seq,
                expected=expected_digest,
                actual=content_digest,
            )
        operations.append(
            Operation(
                seq=seq,
                op_type=op_type,
                pk=pk,
                before=before,
                after=after,
                content_digest=expected_digest,
                clock_ms=clock_ms,
            )
        )
    return tuple(operations)


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError as exc:
        raise BusinessError(
            E_MANIFEST_MISSING,
            "manifest file does not exist",
            {"path": os.path.basename(path)},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            E_IO,
            "manifest file could not be read",
            {"path": os.path.basename(path), "reason": exc.strerror},
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_JSON_MALFORMED,
            "manifest JSON is malformed",
            {
                "line": exc.lineno,
                "column": exc.colno,
                "position": exc.pos,
                "reason": exc.msg,
            },
        ) from exc


def parse_manifest(document: Any) -> Manifest:
    if not isinstance(document, dict):
        raise BusinessError(
            E_BAD_SCHEMA_DECL,
            "manifest root must be an object",
            {"actual_type": type(document).__name__},
        )
    format_version = document.get("format_version", 1)
    if not _is_int(format_version) or format_version != 1:
        raise BusinessError(
            E_BAD_SCHEMA_DECL,
            "unsupported manifest format_version",
            {"format_version": format_version},
        )
    sample_id = _validate_name(
        document.get("sample_id"),
        code=E_BAD_SCHEMA_DECL,
        field="sample_id",
    )
    schema = _parse_schema(document.get("schema"))
    indexes = _parse_indexes(document.get("indexes"), schema)
    query_templates = _parse_queries(
        document.get("query_templates", []),
        schema,
        indexes,
    )
    raw_operations = document.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise BusinessError(
            E_BAD_OP,
            "operations must be a non-empty array",
            {"field": "operations"},
        )
    config = _parse_config(
        document.get("config"),
        operation_count=len(raw_operations),
        index_names={index.name for index in indexes},
    )
    operations = _parse_operations(raw_operations, schema, config)
    normalized_document = {
        "format_version": format_version,
        "sample_id": sample_id,
        "schema": {
            "name": schema.name,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                }
                for column in schema.columns
            ],
            "primary_key": list(schema.primary_key),
            "unique_constraints": [
                list(constraint) for constraint in schema.unique_constraints
            ],
        },
        "indexes": [
            {
                "name": index.name,
                "columns": list(index.columns),
                "unique": index.unique,
                "covering": list(index.covering),
                "covering_query": index.covering_query,
            }
            for index in indexes
        ],
        "query_templates": [
            {
                "name": query.name,
                "index": query.index,
                "columns": list(query.columns),
            }
            for query in query_templates
        ],
        "operations": [operation.to_dict() for operation in operations],
        "config": {
            "page_capacity": config.page_capacity,
            "split_factor": config.split_factor,
            "rebuild_threshold": {
                "max_pending_items": config.max_pending_items,
                "max_touched_pages": config.max_touched_pages,
                **(
                    {"per_index": config.per_index_thresholds}
                    if config.per_index_thresholds
                    else {}
                ),
            },
            "fixed_scale": config.fixed_scale,
            "rounding": config.rounding,
            "float_tolerance": config.float_tolerance,
            "clock_tolerance_ms": config.clock_tolerance_ms,
            "logical_start_ms": config.logical_start_ms,
            "clock_steps_ms": list(config.clock_steps_ms),
            "rebuild_window_ms": config.rebuild_window_ms,
            "max_tree_height": config.max_tree_height,
        },
    }
    return Manifest(
        format_version=format_version,
        sample_id=sample_id,
        schema=schema,
        indexes=indexes,
        query_templates=query_templates,
        config=config,
        operations=operations,
        sample_sha256=sha256_json(normalized_document),
    )


def load_manifest(path: str) -> Manifest:
    return parse_manifest(_load_json(path))


def manifest_from_json(text: str) -> Manifest:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_JSON_MALFORMED,
            "stored manifest JSON is malformed",
            {
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc
    return parse_manifest(document)


def all_changed_columns(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[str, ...]:
    if before is None and after is None:
        return ()
    if before is None:
        return tuple(sorted(after or {}))
    if after is None:
        return tuple(sorted(before))
    return tuple(
        sorted(
            column
            for column in set(before) | set(after)
            if before.get(column) != after.get(column)
        )
    )


def operation_signature(operation: Operation) -> str:
    return sha256_json(operation.digest_payload())


def ensure_unique_names(names: Iterable[str], *, code: str, kind: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise _business(code, f"duplicate {kind} name", name=name)
        seen.add(name)
