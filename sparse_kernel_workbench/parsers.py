"""Parsers for Matrix Market, CSR text, and CSC text fragments."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .errors import WorkbenchError, validation_error
from .model import RawEntry, RawMatrix
from .spec import VersionSpec


def parse_matrix(spec: VersionSpec) -> RawMatrix:
    if spec.format == "matrix_market":
        return _parse_matrix_market(spec)
    if spec.format == "csr":
        return _parse_compressed_text(spec, "csr")
    if spec.format == "csc":
        return _parse_compressed_text(spec, "csc")
    raise validation_error(
        "SKW-FMT-001",
        f"Unsupported format: {spec.format}",
        version=spec.version,
        file=str(spec.matrix_path),
        field="format",
        element="fragment",
    )


def _read_lines(path: Path, spec: VersionSpec) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise WorkbenchError(
            "SKW-IO-004",
            f"Matrix fragment does not exist: {path}",
            stage="parsing",
            context={
                "version": spec.version,
                "file": str(path),
                "field": "matrix_file",
                "element": "fragment",
            },
        ) from exc
    except UnicodeDecodeError as exc:
        raise WorkbenchError(
            "SKW-IO-005",
            f"Matrix fragment is not UTF-8: {path}",
            stage="parsing",
            context={
                "version": spec.version,
                "file": str(path),
                "field": "encoding",
                "element": "fragment",
                "reason": str(exc),
            },
        ) from exc


def _parse_int_token(
    token: str,
    *,
    spec: VersionSpec,
    line: int,
    element: str,
    field: str,
) -> int:
    try:
        value = int(token, 10)
    except (TypeError, ValueError) as exc:
        raise validation_error(
            "SKW-FMT-002",
            f"Expected an integer in field {field!r}",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=element,
            field=field,
            raw_value=token,
        ) from exc
    return value


def _parse_decimal_token(
    token: str,
    *,
    spec: VersionSpec,
    line: int,
    element: str,
    field: str,
) -> Decimal:
    try:
        value = Decimal(token)
    except (InvalidOperation, ValueError) as exc:
        raise validation_error(
            "SKW-FMT-003",
            f"Expected a finite decimal in field {field!r}",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=element,
            field=field,
            raw_value=token,
        ) from exc
    if not value.is_finite():
        raise validation_error(
            "SKW-FMT-003",
            f"Expected a finite decimal in field {field!r}",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=element,
            field=field,
            raw_value=token,
        )
    return value


def _check_declared_shape(
    rows: int,
    columns: int,
    *,
    spec: VersionSpec,
    line: int,
    rows_field: str,
    columns_field: str,
) -> None:
    if rows != spec.rows:
        raise validation_error(
            "SKW-NORM-004",
            "Fragment row count differs from the version's declared highest index",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element="header",
            field=rows_field,
            raw_value=rows,
            declared_highest_row_index=spec.highest_row_index,
            normalized_rows=spec.rows,
        )
    if columns != spec.columns:
        raise validation_error(
            "SKW-NORM-004",
            "Fragment column count differs from the version's declared highest index",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element="header",
            field=columns_field,
            raw_value=columns,
            declared_highest_column_index=spec.highest_column_index,
            normalized_columns=spec.columns,
        )


def _parse_matrix_market(spec: VersionSpec) -> RawMatrix:
    lines = _read_lines(spec.matrix_path, spec)
    row_field = spec.fields.get("row", "row_id")
    column_field = spec.fields.get("column", "column_id")
    value_field = spec.fields.get("value", "value")
    rows_field = spec.fields.get("rows", "rows")
    columns_field = spec.fields.get("columns", "columns")
    nnz_field = spec.fields.get("nonzero_count", "nnz")

    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines) or not lines[cursor].lstrip().startswith("%%MatrixMarket"):
        raise validation_error(
            "SKW-FMT-004",
            "Matrix Market fragment must start with a %%MatrixMarket header",
            version=spec.version,
            file=str(spec.matrix_path),
            line=cursor + 1,
            element="header",
            field="header",
            raw_value=lines[cursor] if cursor < len(lines) else None,
        )
    header_line = cursor + 1
    header = lines[cursor].strip().split()
    if len(header) < 5:
        raise validation_error(
            "SKW-FMT-004",
            "Incomplete Matrix Market header",
            version=spec.version,
            file=str(spec.matrix_path),
            line=header_line,
            element="header",
            field="header",
            raw_value=lines[cursor],
        )
    banner, matrix_kind, storage, field_type = (
        header[0].lower(),
        header[1].lower(),
        header[2].lower(),
        header[3].lower(),
    )
    symmetry = header[4].lower()
    if banner != "%%matrixmarket" or matrix_kind != "matrix" or storage != "coordinate":
        raise validation_error(
            "SKW-FMT-004",
            "Only Matrix Market coordinate matrices are supported",
            version=spec.version,
            file=str(spec.matrix_path),
            line=header_line,
            element="header",
            field="header",
            raw_value=lines[cursor],
        )
    if field_type not in ("integer", "real", "double", "pattern"):
        raise validation_error(
            "SKW-FMT-004",
            f"Unsupported Matrix Market value type: {field_type}",
            version=spec.version,
            file=str(spec.matrix_path),
            line=header_line,
            element="header",
            field="value_type",
            raw_value=field_type,
        )
    if symmetry != "general":
        raise validation_error(
            "SKW-FMT-004",
            "Only Matrix Market 'general' symmetry is supported",
            version=spec.version,
            file=str(spec.matrix_path),
            line=header_line,
            element="header",
            field="symmetry",
            raw_value=symmetry,
        )

    cursor += 1
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if stripped and not stripped.startswith("%"):
            break
        cursor += 1
    if cursor >= len(lines):
        raise validation_error(
            "SKW-FMT-005",
            "Matrix Market fragment is missing its size line",
            version=spec.version,
            file=str(spec.matrix_path),
            line=len(lines),
            element="header",
            field=rows_field,
        )
    size_line = cursor + 1
    size_tokens = lines[cursor].split()
    if len(size_tokens) != 3:
        raise validation_error(
            "SKW-FMT-005",
            "Matrix Market size line must contain rows, columns, and nonzeros",
            version=spec.version,
            file=str(spec.matrix_path),
            line=size_line,
            element="header",
            field="size",
            raw_value=lines[cursor],
        )
    rows = _parse_int_token(
        size_tokens[0], spec=spec, line=size_line, element="header", field=rows_field
    )
    columns = _parse_int_token(
        size_tokens[1], spec=spec, line=size_line, element="header", field=columns_field
    )
    declared_nnz = _parse_int_token(
        size_tokens[2],
        spec=spec,
        line=size_line,
        element="header",
        field=nnz_field,
    )
    _check_declared_shape(
        rows,
        columns,
        spec=spec,
        line=size_line,
        rows_field=rows_field,
        columns_field=columns_field,
    )
    if declared_nnz < 0:
        raise validation_error(
            "SKW-FMT-005",
            "Matrix Market nonzero count cannot be negative",
            version=spec.version,
            file=str(spec.matrix_path),
            line=size_line,
            element="header",
            field=nnz_field,
            raw_value=declared_nnz,
        )

    entries: list[RawEntry] = []
    cursor += 1
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if not stripped or stripped.startswith("%"):
            cursor += 1
            continue
        if len(entries) >= declared_nnz:
            raise validation_error(
                "SKW-FMT-006",
                "Matrix Market fragment contains more entries than declared",
                version=spec.version,
                file=str(spec.matrix_path),
                line=cursor + 1,
                element=f"nnz[{len(entries)}]",
                field=row_field,
                raw_value=stripped,
            )
        tokens = stripped.split()
        expected_tokens = 2 if field_type == "pattern" else 3
        if len(tokens) != expected_tokens:
            raise validation_error(
                "SKW-FMT-006",
                "Malformed Matrix Market entry",
                version=spec.version,
                file=str(spec.matrix_path),
                line=cursor + 1,
                element=f"nnz[{len(entries)}]",
                field=value_field,
                raw_value=stripped,
            )
        element = f"nnz[{len(entries)}]"
        row = _parse_int_token(
            tokens[0], spec=spec, line=cursor + 1, element=element, field=row_field
        )
        column = _parse_int_token(
            tokens[1], spec=spec, line=cursor + 1, element=element, field=column_field
        )
        value = (
            Decimal(1)
            if field_type == "pattern"
            else _parse_decimal_token(
                tokens[2],
                spec=spec,
                line=cursor + 1,
                element=element,
                field=value_field,
            )
        )
        entries.append(
            RawEntry(
                row=row,
                column=column,
                value=value,
                line=cursor + 1,
                element=element,
            )
        )
        cursor += 1
    if len(entries) != declared_nnz:
        raise validation_error(
            "SKW-FMT-006",
            "Matrix Market fragment contains fewer entries than declared",
            version=spec.version,
            file=str(spec.matrix_path),
            line=len(lines),
            element=f"nnz[{len(entries)}]",
            field=nnz_field,
            raw_value=declared_nnz,
            parsed_entries=len(entries),
        )
    return RawMatrix(
        rows=rows,
        columns=columns,
        entries=tuple(entries),
        metadata={
            "header": lines[header_line - 1].strip(),
            "storage": field_type,
            "symmetry": symmetry,
            "declared_nnz": declared_nnz,
        },
    )


def _parse_tagged_lines(
    spec: VersionSpec,
) -> tuple[dict[str, tuple[int, str]], list[str]]:
    lines = _read_lines(spec.matrix_path, spec)
    tags: dict[str, tuple[int, str]] = {}
    for index, raw_line in enumerate(lines):
        line_number = index + 1
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("%"):
            continue
        line = stripped.split("#", 1)[0].strip()
        if not line:
            continue
        pieces = re.split(r"[\s:=]+", line, maxsplit=1)
        tag = pieces[0]
        value = pieces[1].strip() if len(pieces) == 2 else ""
        if not tag:
            raise validation_error(
                "SKW-FMT-007",
                "Compressed text line must start with a field tag",
                version=spec.version,
                file=str(spec.matrix_path),
                line=line_number,
                element="fragment",
                field="line",
                raw_value=raw_line,
            )
        if tag in tags:
            raise validation_error(
                "SKW-FMT-008",
                f"Duplicate compressed-text tag: {tag}",
                version=spec.version,
                file=str(spec.matrix_path),
                line=line_number,
                element="fragment",
                field=tag,
                raw_value=value,
            )
        tags[tag] = (line_number, value)
    return tags, lines


def _required_tag(
    tags: dict[str, tuple[int, str]],
    *,
    spec: VersionSpec,
    internal_name: str,
) -> tuple[int, str, str]:
    raw_tag = spec.fields.get(internal_name, internal_name)
    if raw_tag not in tags:
        raise validation_error(
            "SKW-FMT-009",
            f"Compressed text fragment is missing required field {raw_tag!r}",
            version=spec.version,
            file=str(spec.matrix_path),
            line=1,
            element="fragment",
            field=raw_tag,
        )
    line, value = tags[raw_tag]
    return line, value, raw_tag


def _tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[\s,]+", value.strip()) if token]


def _scalar_int(
    tags: dict[str, tuple[int, str]],
    *,
    spec: VersionSpec,
    internal_name: str,
) -> tuple[int, int, str]:
    line, raw_value, raw_tag = _required_tag(tags, spec=spec, internal_name=internal_name)
    tokens = _tokens(raw_value)
    if len(tokens) != 1:
        raise validation_error(
            "SKW-FMT-010",
            f"Field {raw_tag!r} must contain one integer",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element="header",
            field=raw_tag,
            raw_value=raw_value,
        )
    value = _parse_int_token(
        tokens[0], spec=spec, line=line, element="header", field=raw_tag
    )
    return value, line, raw_tag


def _int_array(
    tags: dict[str, tuple[int, str]],
    *,
    spec: VersionSpec,
    internal_name: str,
    required: bool = True,
) -> tuple[list[int] | None, int, str]:
    raw_tag = spec.fields.get(internal_name, internal_name)
    if raw_tag not in tags:
        if required:
            _required_tag(tags, spec=spec, internal_name=internal_name)
        return None, 1, raw_tag
    line, raw_value = tags[raw_tag]
    tokens = _tokens(raw_value)
    values: list[int] = []
    for index, token in enumerate(tokens):
        try:
            values.append(int(token, 10))
        except (TypeError, ValueError) as exc:
            raise validation_error(
                "SKW-FMT-002",
                f"Expected an integer in field {raw_tag!r}",
                version=spec.version,
                file=str(spec.matrix_path),
                line=line,
                element=f"{raw_tag}[{index}]",
                field=raw_tag,
                raw_value=token,
            ) from exc
    return values, line, raw_tag


def _decimal_array(
    tags: dict[str, tuple[int, str]],
    *,
    spec: VersionSpec,
    internal_name: str,
) -> tuple[list[Decimal], int, str]:
    line, raw_value, raw_tag = _required_tag(tags, spec=spec, internal_name=internal_name)
    tokens = _tokens(raw_value)
    values: list[Decimal] = []
    for index, token in enumerate(tokens):
        values.append(
            _parse_decimal_token(
                token,
                spec=spec,
                line=line,
                element=f"{raw_tag}[{index}]",
                field=raw_tag,
            )
        )
    return values, line, raw_tag


def _validate_ptr(
    ptr: list[int],
    *,
    expected_length: int,
    nnz: int,
    spec: VersionSpec,
    line: int,
    field: str,
) -> None:
    if len(ptr) != expected_length:
        raise validation_error(
            "SKW-FMT-011",
            f"Pointer field {field!r} has the wrong length",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=field,
            field=field,
            raw_value=len(ptr),
            expected_length=expected_length,
        )
    if not ptr or ptr[0] != 0:
        raise validation_error(
            "SKW-FMT-011",
            f"Pointer field {field!r} must start at zero",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=f"{field}[0]",
            field=field,
            raw_value=ptr[0] if ptr else None,
        )
    if ptr[-1] != nnz:
        raise validation_error(
            "SKW-FMT-011",
            f"Pointer field {field!r} must end at the nonzero count",
            version=spec.version,
            file=str(spec.matrix_path),
            line=line,
            element=f"{field}[{len(ptr) - 1}]",
            field=field,
            raw_value=ptr[-1],
            declared_nnz=nnz,
        )
    for index in range(1, len(ptr)):
        if ptr[index] < ptr[index - 1] or ptr[index] > nnz:
            raise validation_error(
                "SKW-FMT-011",
                f"Pointer field {field!r} must be monotonic and bounded by nnz",
                version=spec.version,
                file=str(spec.matrix_path),
                line=line,
                element=f"{field}[{index}]",
                field=field,
                raw_value=ptr[index],
                previous_value=ptr[index - 1],
                declared_nnz=nnz,
            )


def _parse_compressed_text(spec: VersionSpec, layout: str) -> RawMatrix:
    tags, _ = _parse_tagged_lines(spec)
    if "format" in tags:
        line, raw_format = tags["format"]
        normalized_format = raw_format.strip().lower()
        accepted = (layout, f"{layout}_text", f"{layout}-text")
        if normalized_format not in accepted:
            raise validation_error(
                "SKW-FMT-012",
                f"Fragment declares format {raw_format!r}, expected {layout!r}",
                version=spec.version,
                file=str(spec.matrix_path),
                line=line,
                element="header",
                field="format",
                raw_value=raw_format,
            )

    rows, rows_line, rows_field = _scalar_int(tags, spec=spec, internal_name="rows")
    columns, columns_line, columns_field = _scalar_int(
        tags, spec=spec, internal_name="columns"
    )
    nnz, nnz_line, nnz_field = _scalar_int(
        tags, spec=spec, internal_name="nonzero_count"
    )
    _check_declared_shape(
        rows,
        columns,
        spec=spec,
        line=rows_line,
        rows_field=rows_field,
        columns_field=columns_field,
    )
    if nnz < 0:
        raise validation_error(
            "SKW-FMT-013",
            "Nonzero count cannot be negative",
            version=spec.version,
            file=str(spec.matrix_path),
            line=nnz_line,
            element="header",
            field=nnz_field,
            raw_value=nnz,
        )

    if layout == "csr":
        ptr, ptr_line, ptr_field = _int_array(
            tags, spec=spec, internal_name="row_ptr", required=True
        )
        assert ptr is not None
        _validate_ptr(
            ptr,
            expected_length=rows + 1,
            nnz=nnz,
            spec=spec,
            line=ptr_line,
            field=ptr_field,
        )
        row_ids, row_ids_line, row_ids_field = _int_array(
            tags, spec=spec, internal_name="row_ids", required=False
        )
        if row_ids is not None and len(row_ids) != rows:
            raise validation_error(
                "SKW-FMT-014",
                "Row-id array has the wrong length",
                version=spec.version,
                file=str(spec.matrix_path),
                line=row_ids_line,
                element=row_ids_field,
                field=row_ids_field,
                raw_value=len(row_ids),
                expected_length=rows,
            )
        column_indices, column_line, column_field = _int_array(
            tags, spec=spec, internal_name="column_indices", required=True
        )
        assert column_indices is not None
        values, value_line, value_field = _decimal_array(
            tags, spec=spec, internal_name="values"
        )
        if len(column_indices) != nnz:
            raise validation_error(
                "SKW-FMT-015",
                "Column-index count differs from declared nonzeros",
                version=spec.version,
                file=str(spec.matrix_path),
                line=column_line,
                element=f"{column_field}[{len(column_indices)}]",
                field=column_field,
                raw_value=len(column_indices),
                declared_nnz=nnz,
            )
        if len(values) != nnz:
            raise validation_error(
                "SKW-FMT-015",
                "Value count differs from declared nonzeros",
                version=spec.version,
                file=str(spec.matrix_path),
                line=value_line,
                element=f"{value_field}[{len(values)}]",
                field=value_field,
                raw_value=len(values),
                declared_nnz=nnz,
            )
        entries: list[RawEntry] = []
        for row_position in range(rows):
            raw_row = (
                row_ids[row_position]
                if row_ids is not None
                else row_position + spec.index_base
            )
            for position in range(ptr[row_position], ptr[row_position + 1]):
                entries.append(
                    RawEntry(
                        row=raw_row,
                        column=column_indices[position],
                        value=values[position],
                        line=column_line,
                        element=f"nnz[{position}]",
                    )
                )
        metadata = {
            "layout": "csr",
            "row_ptr_field": ptr_field,
            "row_ids_field": row_ids_field if row_ids is not None else None,
            "column_indices_field": column_field,
            "values_field": value_field,
            "declared_nnz": nnz,
        }
    else:
        ptr, ptr_line, ptr_field = _int_array(
            tags, spec=spec, internal_name="column_ptr", required=True
        )
        assert ptr is not None
        _validate_ptr(
            ptr,
            expected_length=columns + 1,
            nnz=nnz,
            spec=spec,
            line=ptr_line,
            field=ptr_field,
        )
        column_ids, column_ids_line, column_ids_field = _int_array(
            tags, spec=spec, internal_name="column_ids", required=False
        )
        if column_ids is not None and len(column_ids) != columns:
            raise validation_error(
                "SKW-FMT-014",
                "Column-id array has the wrong length",
                version=spec.version,
                file=str(spec.matrix_path),
                line=column_ids_line,
                element=column_ids_field,
                field=column_ids_field,
                raw_value=len(column_ids),
                expected_length=columns,
            )
        row_indices, row_line, row_field = _int_array(
            tags, spec=spec, internal_name="row_indices", required=True
        )
        assert row_indices is not None
        values, value_line, value_field = _decimal_array(
            tags, spec=spec, internal_name="values"
        )
        if len(row_indices) != nnz:
            raise validation_error(
                "SKW-FMT-015",
                "Row-index count differs from declared nonzeros",
                version=spec.version,
                file=str(spec.matrix_path),
                line=row_line,
                element=f"{row_field}[{len(row_indices)}]",
                field=row_field,
                raw_value=len(row_indices),
                declared_nnz=nnz,
            )
        if len(values) != nnz:
            raise validation_error(
                "SKW-FMT-015",
                "Value count differs from declared nonzeros",
                version=spec.version,
                file=str(spec.matrix_path),
                line=value_line,
                element=f"{value_field}[{len(values)}]",
                field=value_field,
                raw_value=len(values),
                declared_nnz=nnz,
            )
        entries = []
        for column_position in range(columns):
            raw_column = (
                column_ids[column_position]
                if column_ids is not None
                else column_position + spec.index_base
            )
            for position in range(ptr[column_position], ptr[column_position + 1]):
                entries.append(
                    RawEntry(
                        row=row_indices[position],
                        column=raw_column,
                        value=values[position],
                        line=row_line,
                        element=f"nnz[{position}]",
                    )
                )
        metadata = {
            "layout": "csc",
            "column_ptr_field": ptr_field,
            "column_ids_field": column_ids_field if column_ids is not None else None,
            "row_indices_field": row_field,
            "values_field": value_field,
            "declared_nnz": nnz,
        }

    metadata["nnz_line"] = nnz_line
    return RawMatrix(rows=rows, columns=columns, entries=tuple(entries), metadata=metadata)
