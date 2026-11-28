"""Version and operation specification loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import WorkbenchError, validation_error


FORMAT_ALIASES = {
    "matrix_market": "matrix_market",
    "matrix-market": "matrix_market",
    "matrixmarket": "matrix_market",
    "mtx": "matrix_market",
    "mm": "matrix_market",
    "csr_text": "csr",
    "csr-text": "csr",
    "csr": "csr",
    "csc_text": "csc",
    "csc-text": "csc",
    "csc": "csc",
}

DIRECTION_ALIASES = {
    "row": "row_accumulate",
    "rows": "row_accumulate",
    "row_accumulate": "row_accumulate",
    "row-accumulate": "row_accumulate",
    "column": "column_accumulate",
    "columns": "column_accumulate",
    "col": "column_accumulate",
    "column_accumulate": "column_accumulate",
    "column-accumulate": "column_accumulate",
}


@dataclass(frozen=True)
class VectorSpec:
    path: Path
    ids_field: str
    values_field: str
    index_base: int
    expected_length: int


@dataclass(frozen=True)
class VersionSpec:
    path: Path
    version: str
    format: str
    matrix_path: Path
    index_base: int
    rows: int
    columns: int
    highest_row_index: int
    highest_column_index: int
    fields: dict[str, str]
    weight_field: str
    weight_unit: str
    weight_factor: Decimal
    vector: VectorSpec
    raw: dict[str, Any]


@dataclass(frozen=True)
class OperationSpec:
    path: Path
    direction: str
    batch_budget: int
    dense_chunk_size: int
    hotspot_multiplier: Decimal
    tolerance: float
    strategies: tuple[str, ...]
    hotspot_average_basis: str
    raw: dict[str, Any]


def _load_json(path: Path, stage: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkbenchError(
            "SKW-IO-001",
            f"Specification file does not exist: {path}",
            stage=stage,
            context={"file": str(path), "field": "file", "element": "spec"},
        ) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            "SKW-IO-002",
            f"Specification file is not valid JSON: {path}",
            stage=stage,
            context={
                "file": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "field": "json",
                "element": "spec",
                "reason": exc.msg,
            },
        ) from exc
    if not isinstance(value, dict):
        raise WorkbenchError(
            "SKW-IO-003",
            f"Specification root must be an object: {path}",
            stage=stage,
            context={"file": str(path), "field": "root", "element": "spec"},
        )
    return value


def _as_int(value: Any, *, field_name: str, path: Path, stage: str, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise validation_error(
            "SKW-SPEC-001",
            f"Specification field {field_name!r} must be an integer",
            stage=stage,
            file=str(path),
            field=field_name,
            element="spec",
            raw_value=value,
        ) from exc
    if isinstance(value, bool) or parsed < minimum:
        raise validation_error(
            "SKW-SPEC-001",
            f"Specification field {field_name!r} must be an integer >= {minimum}",
            stage=stage,
            file=str(path),
            field=field_name,
            element="spec",
            raw_value=value,
        )
    return parsed


def _as_decimal(
    value: Any,
    *,
    field_name: str,
    path: Path,
    stage: str,
    positive: bool = False,
) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise validation_error(
            "SKW-SPEC-002",
            f"Specification field {field_name!r} must be a decimal number",
            stage=stage,
            file=str(path),
            field=field_name,
            element="spec",
            raw_value=value,
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise validation_error(
            "SKW-SPEC-002",
            f"Specification field {field_name!r} must be a finite "
            + ("positive " if positive else "")
            + "decimal number",
            stage=stage,
            file=str(path),
            field=field_name,
            element="spec",
            raw_value=value,
        )
    return parsed


def _resolve(base: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _field_map(spec: dict[str, Any], defaults: dict[str, str]) -> dict[str, str]:
    result = dict(defaults)
    fields = spec.get("fields", {})
    if fields is not None and not isinstance(fields, dict):
        raise WorkbenchError(
            "SKW-SPEC-003",
            "Version spec field 'fields' must be an object",
            stage="spec",
            context={"field": "fields", "element": "spec"},
        )
    if isinstance(fields, dict):
        for key, value in fields.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise WorkbenchError(
                    "SKW-SPEC-003",
                    "Version spec fields must map string names to string source tags",
                    stage="spec",
                    context={"field": key, "element": "spec", "raw_value": value},
                )
            result[key] = value
    return result


def load_version_spec(path: Path) -> VersionSpec:
    path = path.resolve()
    raw = _load_json(path, "spec")
    base = path.parent
    version = str(raw.get("version") or raw.get("id") or path.stem)
    format_raw = str(raw.get("format", "")).lower()
    if format_raw not in FORMAT_ALIASES:
        raise validation_error(
            "SKW-SPEC-004",
            f"Unsupported fragment format: {format_raw!r}",
            stage="spec",
            version=version,
            file=str(path),
            field="format",
            element="spec",
            raw_value=format_raw,
        )
    format_name = FORMAT_ALIASES[format_raw]

    matrix_raw = raw.get("matrix_file") or raw.get("matrix") or raw.get("path")
    if not matrix_raw:
        raise validation_error(
            "SKW-SPEC-005",
            "Version spec must declare matrix_file",
            stage="spec",
            version=version,
            file=str(path),
            field="matrix_file",
            element="spec",
        )
    matrix_path = _resolve(base, str(matrix_raw))

    index_base = _as_int(raw.get("index_base"), field_name="index_base", path=path, stage="spec")
    if index_base not in (0, 1):
        raise validation_error(
            "SKW-SPEC-006",
            "index_base must be 0 or 1",
            stage="spec",
            version=version,
            file=str(path),
            field="index_base",
            element="spec",
            raw_value=index_base,
        )

    highest = raw.get("highest_index") or raw.get("declared_highest_index")
    declared = raw.get("declared_shape") or raw.get("shape")
    if isinstance(highest, dict):
        highest_row = highest.get("row")
        highest_column = highest.get("column", highest.get("col"))
    elif isinstance(declared, dict):
        highest_row = None
        highest_column = None
    else:
        highest_row = raw.get("highest_row_index")
        highest_column = raw.get("highest_column_index")

    if highest_row is None or highest_column is None:
        if not isinstance(declared, dict):
            raise validation_error(
                "SKW-SPEC-007",
                "Version spec must declare highest_index or declared_shape",
                stage="spec",
                version=version,
                file=str(path),
                field="highest_index",
                element="spec",
            )
        declared_rows = _as_int(
            declared.get("rows"), field_name="declared_shape.rows", path=path, stage="spec", minimum=1
        )
        declared_columns = _as_int(
            declared.get("columns", declared.get("cols")),
            field_name="declared_shape.columns",
            path=path,
            stage="spec",
            minimum=1,
        )
        highest_row = declared_rows - 1 + index_base
        highest_column = declared_columns - 1 + index_base

    highest_row = _as_int(
        highest_row, field_name="highest_index.row", path=path, stage="spec", minimum=index_base
    )
    highest_column = _as_int(
        highest_column,
        field_name="highest_index.column",
        path=path,
        stage="spec",
        minimum=index_base,
    )
    rows = highest_row - index_base + 1
    columns = highest_column - index_base + 1
    if rows < 1 or columns < 1:
        raise validation_error(
            "SKW-SPEC-007",
            "Declared highest index yields an empty matrix",
            stage="spec",
            version=version,
            file=str(path),
            field="highest_index",
            element="spec",
        )

    defaults = {
        "row": "row",
        "column": "column",
        "value": "value",
        "rows": "rows",
        "columns": "columns",
        "nonzero_count": "nnz",
        "row_ptr": "row_ptr",
        "row_ids": "row_ids",
        "column_ptr": "column_ptr",
        "column_ids": "column_ids",
        "column_indices": "column_indices",
        "row_indices": "row_indices",
        "values": "values",
    }
    fields = _field_map(raw, defaults)
    weight = raw.get("weight", {})
    if not isinstance(weight, dict):
        raise validation_error(
            "SKW-SPEC-008",
            "Version spec field 'weight' must be an object",
            stage="spec",
            version=version,
            file=str(path),
            field="weight",
            element="spec",
        )
    weight_field = str(weight.get("raw_field") or fields.get("value", "value"))
    weight_unit = str(weight.get("unit") or weight.get("raw_unit") or "count")
    weight_factor = _as_decimal(
        weight.get("factor", weight.get("to_canonical_factor", 1)),
        field_name="weight.factor",
        path=path,
        stage="spec",
        positive=True,
    )

    vector_raw = raw.get("dense_vector") or raw.get("vector")
    if isinstance(vector_raw, str):
        vector_raw = {"path": vector_raw}
    if not isinstance(vector_raw, dict) or not vector_raw.get("path"):
        raise validation_error(
            "SKW-SPEC-009",
            "Version spec must declare dense_vector.path",
            stage="spec",
            version=version,
            file=str(path),
            field="dense_vector.path",
            element="spec",
        )
    vector_fields = vector_raw.get("fields", {})
    if not isinstance(vector_fields, dict):
        raise validation_error(
            "SKW-SPEC-009",
            "dense_vector.fields must be an object",
            stage="spec",
            version=version,
            file=str(path),
            field="dense_vector.fields",
            element="spec",
        )
    vector_ids_field = str(vector_fields.get("ids", "indices"))
    vector_values_field = str(vector_fields.get("values", "values"))
    vector_base = _as_int(
        vector_raw.get("index_base", index_base),
        field_name="dense_vector.index_base",
        path=path,
        stage="spec",
    )
    if vector_base not in (0, 1):
        raise validation_error(
            "SKW-SPEC-010",
            "dense_vector.index_base must be 0 or 1",
            stage="spec",
            version=version,
            file=str(path),
            field="dense_vector.index_base",
            element="spec",
            raw_value=vector_base,
        )
    expected_length = _as_int(
        vector_raw.get("expected_length", vector_raw.get("length", columns)),
        field_name="dense_vector.expected_length",
        path=path,
        stage="spec",
        minimum=1,
    )
    if expected_length != columns:
        raise validation_error(
            "SKW-SPEC-011",
            "Dense vector length must equal the normalized column count",
            stage="spec",
            version=version,
            file=str(path),
            field="dense_vector.expected_length",
            element="spec",
            raw_value=expected_length,
            normalized_columns=columns,
        )
    vector = VectorSpec(
        path=_resolve(base, str(vector_raw["path"])),
        ids_field=vector_ids_field,
        values_field=vector_values_field,
        index_base=vector_base,
        expected_length=expected_length,
    )

    return VersionSpec(
        path=path,
        version=version,
        format=format_name,
        matrix_path=matrix_path,
        index_base=index_base,
        rows=rows,
        columns=columns,
        highest_row_index=highest_row,
        highest_column_index=highest_column,
        fields=fields,
        weight_field=weight_field,
        weight_unit=weight_unit,
        weight_factor=weight_factor,
        vector=vector,
        raw=raw,
    )


def load_operation_spec(path: Path) -> OperationSpec:
    path = path.resolve()
    raw = _load_json(path, "operation")
    direction_raw = str(
        raw.get("direction", raw.get("accumulation_direction", ""))
    ).lower()
    if direction_raw not in DIRECTION_ALIASES:
        raise validation_error(
            "SKW-OPSPEC-001",
            f"Unsupported operation direction: {direction_raw!r}",
            stage="operation",
            file=str(path),
            field="direction",
            element="operation_spec",
            raw_value=direction_raw,
        )
    direction = DIRECTION_ALIASES[direction_raw]

    batch_budget = _as_int(
        raw.get("batch_nonzero_budget", raw.get("batch_budget", raw.get("max_nonzeros_per_batch"))),
        field_name="batch_nonzero_budget",
        path=path,
        stage="operation",
        minimum=1,
    )
    dense_chunk_size = _as_int(
        raw.get("dense_vector_chunk_size", raw.get("dense_chunk_size", raw.get("chunk_size"))),
        field_name="dense_vector_chunk_size",
        path=path,
        stage="operation",
        minimum=1,
    )
    hotspot_multiplier = _as_decimal(
        raw.get("hotspot_multiplier", raw.get("hot_batch_multiplier", raw.get("hotspot_factor", 1.5))),
        field_name="hotspot_multiplier",
        path=path,
        stage="operation",
        positive=True,
    )
    tolerance = float(
        _as_decimal(
            raw.get("tolerance", 1e-9),
            field_name="tolerance",
            path=path,
            stage="operation",
            positive=True,
        )
    )
    strategy_raw = raw.get("strategies", ["csr", "csc"])
    if not isinstance(strategy_raw, list):
        raise validation_error(
            "SKW-OPSPEC-002",
            "strategies must be a non-empty list",
            stage="operation",
            file=str(path),
            field="strategies",
            element="operation_spec",
            raw_value=strategy_raw,
        )
    strategies: list[str] = []
    for value in strategy_raw:
        alias = FORMAT_ALIASES.get(str(value).lower())
        if alias not in ("csr", "csc"):
            raise validation_error(
                "SKW-OPSPEC-002",
                f"Unsupported strategy: {value!r}",
                stage="operation",
                file=str(path),
                field="strategies",
                element="operation_spec",
                raw_value=value,
            )
        if alias not in strategies:
            strategies.append(alias)
    if len(strategies) < 2:
        raise validation_error(
            "SKW-OPSPEC-003",
            "At least two strategies are required for an evidence-based recommendation",
            stage="operation",
            file=str(path),
            field="strategies",
            element="operation_spec",
            raw_value=strategy_raw,
        )
    hotspot_basis = str(raw.get("hotspot_average_basis", "per_batch"))
    if hotspot_basis != "per_batch":
        raise validation_error(
            "SKW-OPSPEC-004",
            "Only the declared per_batch hotspot average basis is supported",
            stage="operation",
            file=str(path),
            field="hotspot_average_basis",
            element="operation_spec",
            raw_value=hotspot_basis,
        )
    return OperationSpec(
        path=path,
        direction=direction,
        batch_budget=batch_budget,
        dense_chunk_size=dense_chunk_size,
        hotspot_multiplier=hotspot_multiplier,
        tolerance=tolerance,
        strategies=tuple(strategies),
        hotspot_average_basis=hotspot_basis,
        raw=raw,
    )


def apply_operation_overrides(
    spec: OperationSpec,
    *,
    direction: str | None = None,
    batch_budget: int | None = None,
    dense_chunk_size: int | None = None,
    hotspot_multiplier: Decimal | None = None,
    tolerance: float | None = None,
) -> OperationSpec:
    updates: dict[str, Any] = {}
    if direction is not None:
        alias = DIRECTION_ALIASES.get(direction.lower())
        if alias is None:
            raise validation_error(
                "SKW-OPSPEC-001",
                f"Unsupported operation direction override: {direction!r}",
                stage="operation",
                field="direction",
                element="cli_override",
                raw_value=direction,
            )
        updates["direction"] = alias
    if batch_budget is not None:
        if batch_budget < 1:
            raise validation_error(
                "SKW-OPSPEC-005",
                "batch budget override must be positive",
                stage="operation",
                field="batch_nonzero_budget",
                element="cli_override",
                raw_value=batch_budget,
            )
        updates["batch_budget"] = batch_budget
    if dense_chunk_size is not None:
        if dense_chunk_size < 1:
            raise validation_error(
                "SKW-OPSPEC-006",
                "dense chunk size override must be positive",
                stage="operation",
                field="dense_vector_chunk_size",
                element="cli_override",
                raw_value=dense_chunk_size,
            )
        updates["dense_chunk_size"] = dense_chunk_size
    if hotspot_multiplier is not None:
        if hotspot_multiplier <= 0:
            raise validation_error(
                "SKW-OPSPEC-007",
                "hotspot multiplier override must be positive",
                stage="operation",
                field="hotspot_multiplier",
                element="cli_override",
                raw_value=str(hotspot_multiplier),
            )
        updates["hotspot_multiplier"] = hotspot_multiplier
    if tolerance is not None:
        if tolerance <= 0:
            raise validation_error(
                "SKW-OPSPEC-008",
                "tolerance override must be positive",
                stage="operation",
                field="tolerance",
                element="cli_override",
                raw_value=tolerance,
            )
        updates["tolerance"] = tolerance
    return replace(spec, **updates)
