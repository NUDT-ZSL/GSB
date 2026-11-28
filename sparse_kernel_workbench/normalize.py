"""Normalize fragments to zero-based rows/columns and canonical count weights."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import WorkbenchError, validation_error
from .model import (
    CanonicalEntry,
    NormalizedMatrix,
    VectorData,
    decimal_text,
)
from .parsers import parse_matrix
from .spec import VersionSpec


def load_dense_vector(spec: VersionSpec) -> VectorData:
    path = spec.vector.path
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkbenchError(
            "SKW-IO-006",
            f"Dense vector file does not exist: {path}",
            stage="normalization",
            context={
                "version": spec.version,
                "file": str(path),
                "field": "dense_vector.path",
                "element": "dense_vector",
            },
        ) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            "SKW-IO-007",
            f"Dense vector file is not valid JSON: {path}",
            stage="normalization",
            context={
                "version": spec.version,
                "file": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "field": "json",
                "element": "dense_vector",
                "reason": exc.msg,
            },
        ) from exc

    if isinstance(raw, list):
        raw_ids: list[Any] = list(range(spec.vector.index_base, spec.vector.index_base + len(raw)))
        raw_values: list[Any] = raw
    elif isinstance(raw, dict):
        if spec.vector.ids_field in raw:
            raw_ids = raw[spec.vector.ids_field]
        else:
            raw_values_guess = raw.get(spec.vector.values_field)
            if not isinstance(raw_values_guess, list):
                raise validation_error(
                    "SKW-NORM-VEC-001",
                    "Dense vector is missing its declared values field",
                    version=spec.version,
                    file=str(path),
                    element="dense_vector",
                    field=spec.vector.values_field,
                )
            raw_ids = list(
                range(
                    spec.vector.index_base,
                    spec.vector.index_base + len(raw_values_guess),
                )
            )
        if spec.vector.values_field not in raw:
            raise validation_error(
                "SKW-NORM-VEC-001",
                "Dense vector is missing its declared values field",
                version=spec.version,
                file=str(path),
                element="dense_vector",
                field=spec.vector.values_field,
            )
        raw_values = raw[spec.vector.values_field]
    else:
        raise validation_error(
            "SKW-NORM-VEC-001",
            "Dense vector root must be an array or object",
            version=spec.version,
            file=str(path),
            element="dense_vector",
            field="root",
            raw_value=type(raw).__name__,
        )

    if not isinstance(raw_ids, list) or not isinstance(raw_values, list):
        raise validation_error(
            "SKW-NORM-VEC-001",
            "Dense vector indices and values must be arrays",
            version=spec.version,
            file=str(path),
            element="dense_vector",
            field=spec.vector.ids_field,
        )
    if len(raw_ids) != len(raw_values):
        raise validation_error(
            "SKW-NORM-VEC-002",
            "Dense vector index and value arrays have different lengths",
            version=spec.version,
            file=str(path),
            element="dense_vector",
            field=spec.vector.ids_field,
            raw_value=len(raw_ids),
            values_length=len(raw_values),
        )

    normalized: dict[int, float] = {}
    normalized_ids: list[int] = []
    corrections: list[dict[str, Any]] = []
    for position, (raw_id, raw_value) in enumerate(zip(raw_ids, raw_values, strict=True)):
        if isinstance(raw_id, bool):
            raise validation_error(
                "SKW-NORM-VEC-003",
                "Dense vector coordinate must be an integer",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.ids_field,
                raw_value=raw_id,
            )
        try:
            coordinate = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise validation_error(
                "SKW-NORM-VEC-003",
                "Dense vector coordinate must be an integer",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.ids_field,
                raw_value=raw_id,
            ) from exc
        normalized_coordinate = coordinate - spec.vector.index_base
        if normalized_coordinate < 0 or normalized_coordinate >= spec.columns:
            raise validation_error(
                "SKW-NORM-VEC-004",
                "Dense vector coordinate is outside the normalized matrix columns",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.ids_field,
                raw_value=coordinate,
                index_base=spec.vector.index_base,
                valid_range=[spec.vector.index_base, spec.columns - 1 + spec.vector.index_base],
            )
        if normalized_coordinate in normalized:
            raise validation_error(
                "SKW-NORM-VEC-005",
                "Dense vector contains a duplicate coordinate",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.ids_field,
                raw_value=coordinate,
                normalized_coordinate=normalized_coordinate,
            )
        if isinstance(raw_value, bool):
            raise validation_error(
                "SKW-NORM-VEC-006",
                "Dense vector value must be numeric",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.values_field,
                raw_value=raw_value,
            )
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise validation_error(
                "SKW-NORM-VEC-006",
                "Dense vector value must be numeric",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.values_field,
                raw_value=raw_value,
            ) from exc
        if not math.isfinite(value):
            raise validation_error(
                "SKW-NORM-VEC-006",
                "Dense vector value must be finite",
                version=spec.version,
                file=str(path),
                element=f"vector_entry[{position}]",
                field=spec.vector.values_field,
                raw_value=raw_value,
            )
        normalized[normalized_coordinate] = value
        normalized_ids.append(coordinate)

    missing = [
        index
        for index in range(spec.columns)
        if index not in normalized
    ]
    if missing:
        raise validation_error(
            "SKW-NORM-VEC-007",
            "Dense vector does not cover every normalized column",
            version=spec.version,
            file=str(path),
            element="dense_vector",
            field=spec.vector.ids_field,
            reason="missing normalized column coordinates",
            missing_normalized_indices=missing[:32],
        )
    if len(normalized) != spec.vector.expected_length:
        raise validation_error(
            "SKW-NORM-VEC-008",
            "Dense vector length differs from the declared expectation",
            version=spec.version,
            file=str(path),
            element="dense_vector",
            field=spec.vector.values_field,
            raw_value=len(normalized),
            expected_length=spec.vector.expected_length,
        )

    corrections.extend(
        [
            {
                "version": spec.version,
                "scope": "dense_vector",
                "kind": "field_name",
                "raw_field": spec.vector.ids_field,
                "canonical_field": "column_index",
                "elements_affected": len(raw_ids),
            },
            {
                "version": spec.version,
                "scope": "dense_vector",
                "kind": "field_name",
                "raw_field": spec.vector.values_field,
                "canonical_field": "interest_value",
                "elements_affected": len(raw_values),
            },
            {
                "version": spec.version,
                "scope": "dense_vector",
                "kind": "index_offset",
                "raw_field": spec.vector.ids_field,
                "raw_base": spec.vector.index_base,
                "canonical_base": 0,
                "offset": -spec.vector.index_base,
                "formula": f"normalized_index = raw_index + ({-spec.vector.index_base})",
                "elements_affected": len(raw_ids),
            },
        ]
    )
    corrections[-1]["example"] = {
        "raw_id": raw_ids[0],
        "normalized_id": normalized_ids[0],
    }
    return VectorData(
        values=tuple(normalized[index] for index in range(spec.columns)),
        raw_ids=tuple(raw_ids),
        index_base=spec.vector.index_base,
        source_path=path,
        ids_field=spec.vector.ids_field,
        values_field=spec.vector.values_field,
        corrections=corrections,
    )


def _matrix_corrections(
    spec: VersionSpec,
    *,
    raw_nonzero_count: int,
    duplicate_count: int,
) -> list[dict[str, Any]]:
    if spec.format == "csr":
        row_field = spec.fields.get("row_ids", spec.fields.get("row", "row_id"))
        column_field = spec.fields.get(
            "column_indices", spec.fields.get("column", "column_id")
        )
        value_field = spec.fields.get("values", spec.fields.get("value", "value"))
    elif spec.format == "csc":
        row_field = spec.fields.get(
            "row_indices", spec.fields.get("row", "row_id")
        )
        column_field = spec.fields.get(
            "column_ids", spec.fields.get("column", "column_id")
        )
        value_field = spec.fields.get("values", spec.fields.get("value", "value"))
    else:
        row_field = spec.fields.get("row", "row_id")
        column_field = spec.fields.get("column", "column_id")
        value_field = spec.fields.get("value", "value")
    corrections: list[dict[str, Any]] = [
        {
            "version": spec.version,
            "scope": "matrix",
            "kind": "format",
            "raw_format": spec.format,
            "canonical_model": "zero_based_coordinate_count",
        },
        {
            "version": spec.version,
            "scope": "matrix",
            "kind": "field_name",
            "raw_field": row_field,
            "canonical_field": "row",
        },
        {
            "version": spec.version,
            "scope": "matrix",
            "kind": "field_name",
            "raw_field": column_field,
            "canonical_field": "column",
        },
        {
            "version": spec.version,
            "scope": "matrix",
            "kind": "field_name",
            "raw_field": value_field,
            "canonical_field": "weight",
        },
        {
            "version": spec.version,
            "scope": "matrix.row",
            "kind": "index_offset",
            "raw_field": row_field,
            "raw_base": spec.index_base,
            "canonical_base": 0,
            "offset": -spec.index_base,
            "formula": f"normalized_row = raw_row + ({-spec.index_base})",
            "elements_affected": raw_nonzero_count,
        },
        {
            "version": spec.version,
            "scope": "matrix.column",
            "kind": "index_offset",
            "raw_field": column_field,
            "raw_base": spec.index_base,
            "canonical_base": 0,
            "offset": -spec.index_base,
            "formula": f"normalized_column = raw_column + ({-spec.index_base})",
            "elements_affected": raw_nonzero_count,
        },
        {
            "version": spec.version,
            "scope": "matrix.weight",
            "kind": "weight_unit",
            "raw_field": value_field,
            "raw_unit": spec.weight_unit,
            "canonical_unit": "count",
            "factor": decimal_text(spec.weight_factor),
            "formula": f"normalized_weight = raw_weight * {decimal_text(spec.weight_factor)}",
            "elements_affected": raw_nonzero_count,
        },
        {
            "version": spec.version,
            "scope": "matrix.dimension",
            "kind": "dimension_basis",
            "raw_field": "highest_index",
            "declared_highest_row_index": spec.highest_row_index,
            "declared_highest_column_index": spec.highest_column_index,
            "index_base": spec.index_base,
            "dimension_rule": "highest_declared_index - index_base + 1",
            "normalized_rows": spec.rows,
            "normalized_columns": spec.columns,
        },
    ]
    storage_fields: list[tuple[str, str]] = []
    if spec.format == "csr":
        storage_fields = [
            ("row_ptr", "row_offsets"),
            ("row_ids", "row_ids"),
            ("column_indices", "column_ids"),
            ("values", "weights"),
        ]
    elif spec.format == "csc":
        storage_fields = [
            ("column_ptr", "column_offsets"),
            ("column_ids", "column_ids"),
            ("row_indices", "row_ids"),
            ("values", "weights"),
        ]
    for internal_name, canonical_name in storage_fields:
        raw_field = spec.fields.get(internal_name)
        if raw_field is None:
            continue
        corrections.append(
            {
                "version": spec.version,
                "scope": f"matrix.{spec.format}",
                "kind": "storage_field_name",
                "raw_field": raw_field,
                "canonical_field": canonical_name,
            }
        )
    if duplicate_count:
        corrections.append(
            {
                "version": spec.version,
                "scope": "matrix",
                "kind": "duplicate_merge",
                "rule": "sum_duplicate_coordinates_once",
                "duplicate_entries_merged": duplicate_count,
            }
        )
    return corrections


def normalize_fragment(spec: VersionSpec) -> NormalizedMatrix:
    raw_matrix = parse_matrix(spec)
    if raw_matrix.rows != spec.rows or raw_matrix.columns != spec.columns:
        raise validation_error(
            "SKW-NORM-004",
            "Parsed dimensions differ from normalized dimensions",
            version=spec.version,
            file=str(spec.matrix_path),
            element="header",
            field="shape",
            raw_value=[raw_matrix.rows, raw_matrix.columns],
            normalized_value=[spec.rows, spec.columns],
        )

    row_error_field = (
        raw_matrix.metadata.get("row_ids_field")
        or raw_matrix.metadata.get("row_indices_field")
        or spec.fields.get("row", "row_id")
    )
    column_error_field = (
        raw_matrix.metadata.get("column_indices_field")
        or raw_matrix.metadata.get("column_ids_field")
        or spec.fields.get("column", "column_id")
    )
    value_error_field = (
        raw_matrix.metadata.get("values_field")
        or spec.fields.get("value", "value")
    )
    grouped: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for raw_entry in raw_matrix.entries:
        normalized_row = raw_entry.row - spec.index_base
        if normalized_row < 0 or normalized_row >= spec.rows:
            raise validation_error(
                "SKW-NORM-002",
                "Matrix row index is outside the declared normalized rows",
                version=spec.version,
                file=str(spec.matrix_path),
                line=raw_entry.line,
                element=raw_entry.element,
                field=row_error_field,
                raw_value=raw_entry.row,
                index_base=spec.index_base,
                valid_raw_range=[
                    spec.index_base,
                    spec.rows - 1 + spec.index_base,
                ],
            )
        normalized_column = raw_entry.column - spec.index_base
        if normalized_column < 0 or normalized_column >= spec.columns:
            raise validation_error(
                "SKW-NORM-002",
                "Matrix column index is outside the declared normalized columns",
                version=spec.version,
                file=str(spec.matrix_path),
                line=raw_entry.line,
                element=raw_entry.element,
                field=column_error_field,
                raw_value=raw_entry.column,
                index_base=spec.index_base,
                valid_raw_range=[
                    spec.index_base,
                    spec.columns - 1 + spec.index_base,
                ],
            )
        try:
            normalized_value = raw_entry.value * spec.weight_factor
        except (InvalidOperation, OverflowError) as exc:
            raise validation_error(
                "SKW-NORM-003",
                "Weight conversion failed",
                version=spec.version,
                file=str(spec.matrix_path),
                line=raw_entry.line,
                element=raw_entry.element,
                field=value_error_field,
                raw_value=str(raw_entry.value),
                factor=decimal_text(spec.weight_factor),
            ) from exc
        if not normalized_value.is_finite():
            raise validation_error(
                "SKW-NORM-003",
                "Converted weight is not finite",
                version=spec.version,
                file=str(spec.matrix_path),
                line=raw_entry.line,
                element=raw_entry.element,
                field=value_error_field,
                raw_value=str(raw_entry.value),
                factor=decimal_text(spec.weight_factor),
            )
        grouped[(normalized_row, normalized_column)].append(
            (normalized_value, raw_entry)
        )

    entries: list[CanonicalEntry] = []
    for (row, column) in sorted(grouped):
        group = grouped[(row, column)]
        total = sum((value for value, _ in group), Decimal(0))
        first = group[0][1]
        entries.append(
            CanonicalEntry(
                row=row,
                column=column,
                value=total,
                source_element=first.element,
                source_line=first.line,
                source_elements=tuple(item[1].element for item in group),
            )
        )

    duplicate_count = raw_matrix.metadata.get("declared_nnz", len(raw_matrix.entries)) - len(entries)
    if duplicate_count < 0:
        duplicate_count = 0
    vector = load_dense_vector(spec)
    corrections = _matrix_corrections(
        spec,
        raw_nonzero_count=len(raw_matrix.entries),
        duplicate_count=duplicate_count,
    )
    corrections.extend(vector.corrections)
    return NormalizedMatrix(
        version=spec.version,
        format=spec.format,
        rows=spec.rows,
        columns=spec.columns,
        entries=tuple(entries),
        vector=vector,
        index_base=spec.index_base,
        weight_factor=spec.weight_factor,
        weight_unit=spec.weight_unit,
        weight_field=spec.weight_field,
        highest_row_index=spec.highest_row_index,
        highest_column_index=spec.highest_column_index,
        raw_rows=raw_matrix.rows,
        raw_columns=raw_matrix.columns,
        raw_nonzero_count=len(raw_matrix.entries),
        duplicate_count=duplicate_count,
        source_path=spec.matrix_path,
        spec_path=spec.path,
        fields=dict(spec.fields),
        corrections=corrections,
    )
