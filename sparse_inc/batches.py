"""Versioned, checksum-protected CSR row-range writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import sha256_json
from .codes import (
    E_WRITE_CONFLICT,
    E_WRITE_CORRUPT,
    E_WRITE_RANGE,
    E_WRITE_STALE,
)
from .errors import raise_business


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_batch_payload(raw: Any) -> dict[str, Any]:
    """Build the exact payload covered by a batch checksum.

    Rows are sorted by row number and columns by index. Values are normalized
    to Python float, making the checksum independent of object key ordering.
    """

    rows_payload: list[dict[str, Any]] = []
    raw_rows = raw.get("rows", []) if isinstance(raw, dict) else []
    if isinstance(raw_rows, list):
        normalized: list[tuple[int, int, dict[str, Any]]] = []
        for order, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                normalized.append((order, order, {"row": row}))
                continue
            row_number = row.get("row")
            cols = row.get("col_indices", [])
            values = row.get("values", [])
            pairs: list[tuple[int, float]] = []
            if isinstance(cols, list) and isinstance(values, list):
                for col, value in zip(cols, values):
                    try:
                        pairs.append((int(col), float(value)))
                    except (TypeError, ValueError, OverflowError):
                        pairs.append((col, value))  # type: ignore[arg-type]
            pairs.sort(key=lambda pair: (str(type(pair[0])), pair[0]))
            normalized.append(
                (
                    int(row_number) if _is_int(row_number) else order,
                    order,
                    {
                        "row": row_number,
                        "col_indices": [pair[0] for pair in pairs],
                        "values": [pair[1] for pair in pairs],
                    },
                )
            )
        rows_payload = [item[2] for item in sorted(normalized, key=lambda item: (item[0], item[1]))]
    return {
        "batch_id": raw.get("batch_id") if isinstance(raw, dict) else None,
        "write_version": raw.get("write_version") if isinstance(raw, dict) else None,
        "row_start": raw.get("row_start") if isinstance(raw, dict) else None,
        "row_end": raw.get("row_end") if isinstance(raw, dict) else None,
        "mode": raw.get("mode") if isinstance(raw, dict) else None,
        "rows": rows_payload,
    }


def batch_checksum_payload(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


def seal_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical batch dict with a correctly computed checksum."""

    result = canonical_batch_payload(payload)
    result["checksum"] = batch_checksum_payload(result)
    return result


@dataclass(frozen=True)
class RowWrite:
    row: int
    col_indices: tuple[int, ...]
    values: tuple[float, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col_indices": list(self.col_indices),
            "values": list(self.values),
        }


@dataclass(frozen=True)
class WriteBatch:
    batch_id: str
    write_version: int
    row_start: int
    row_end: int
    mode: str
    rows: tuple[RowWrite, ...]
    checksum: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "write_version": self.write_version,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "mode": self.mode,
            "rows": [row.public_dict() for row in self.rows],
        }

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.canonical_payload()
        payload["checksum"] = self.checksum
        return payload


def parse_write_batch(
    raw: Any,
    *,
    case_id: str,
    rows: int,
    cols: int,
    latest_version: int | None = None,
    existing_batch_ids: Iterable[str] = (),
) -> WriteBatch:
    if not isinstance(raw, dict):
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="batch",
            message="write batch must be an object",
        )
    batch_id = raw.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="batch_id",
            message="batch_id must be a non-empty string",
        )
    write_version = raw.get("write_version")
    if not _is_int(write_version) or write_version < 0:
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="write_version",
            message="write_version must be a non-negative integer",
            batch_id=batch_id,
        )

    canonical_payload = canonical_batch_payload(raw)
    try:
        expected_checksum = batch_checksum_payload(canonical_payload)
    except ValueError as exc:
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="rows.values",
            message=f"write batch contains a non-serializable numeric value: {exc}",
            batch_id=batch_id,
            write_version=write_version,
        )
    provided_checksum = raw.get("checksum")
    if provided_checksum != expected_checksum:
        raise_business(
            E_WRITE_CORRUPT,
            case_id=case_id,
            segment="checksum",
            message="write batch content checksum mismatch",
            batch_id=batch_id,
            write_version=write_version,
            expected_checksum=expected_checksum,
            provided_checksum=provided_checksum,
        )

    row_start = raw.get("row_start")
    row_end = raw.get("row_end")
    if (
        not _is_int(row_start)
        or not _is_int(row_end)
        or row_start < 0
        or row_end <= row_start
        or row_end > rows
    ):
        raise_business(
            E_WRITE_RANGE,
            case_id=case_id,
            segment="row_interval",
            message="row interval must be a non-empty half-open interval inside the matrix",
            batch_id=batch_id,
            write_version=write_version,
            row_start=row_start,
            row_end=row_end,
            row_bound=rows,
        )

    mode = raw.get("mode")
    if mode not in {"replace", "invalidate"}:
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="mode",
            message="mode must be replace or invalidate",
            batch_id=batch_id,
            write_version=write_version,
        )

    if latest_version is not None and write_version <= latest_version:
        raise_business(
            E_WRITE_STALE,
            case_id=case_id,
            segment="write_version",
            message="write_version must be strictly greater than the accepted ledger version",
            batch_id=batch_id,
            write_version=write_version,
            latest_version=latest_version,
        )

    if batch_id in set(existing_batch_ids):
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="batch_id",
            message="batch_id has already been accepted",
            batch_id=batch_id,
            write_version=write_version,
        )

    raw_rows = raw.get("rows", [])
    if not isinstance(raw_rows, list):
        raise_business(
            E_WRITE_CONFLICT,
            case_id=case_id,
            segment="rows",
            message="rows must be a list",
            batch_id=batch_id,
            write_version=write_version,
        )
    if mode == "invalidate":
        if raw_rows:
            raise_business(
                E_WRITE_CONFLICT,
                case_id=case_id,
                segment="rows",
                message="invalidate batches must not carry row content",
                batch_id=batch_id,
                write_version=write_version,
            )
        return WriteBatch(
            batch_id=batch_id,
            write_version=write_version,
            row_start=row_start,
            row_end=row_end,
            mode=mode,
            rows=(),
            checksum=expected_checksum,
        )

    parsed_rows: list[RowWrite] = []
    seen_rows: set[int] = set()
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict):
            raise_business(
                E_WRITE_CONFLICT,
                case_id=case_id,
                segment=f"rows[{index}]",
                message="row write must be an object",
                batch_id=batch_id,
                write_version=write_version,
            )
        row_number = raw_row.get("row")
        if not _is_int(row_number):
            raise_business(
                E_WRITE_RANGE,
                case_id=case_id,
                segment=f"rows[{index}].row",
                message="row number must be an integer",
                batch_id=batch_id,
                write_version=write_version,
            )
        if row_number < row_start or row_number >= row_end:
            raise_business(
                E_WRITE_RANGE,
                case_id=case_id,
                segment=f"rows[{index}].row",
                message="row number is outside the declared row interval",
                batch_id=batch_id,
                write_version=write_version,
                row=row_number,
                row_start=row_start,
                row_end=row_end,
            )
        if row_number in seen_rows:
            raise_business(
                E_WRITE_CONFLICT,
                case_id=case_id,
                segment=f"rows[{index}].row",
                message="a row may appear only once per replace batch",
                batch_id=batch_id,
                write_version=write_version,
                row=row_number,
            )
        seen_rows.add(row_number)
        raw_cols = raw_row.get("col_indices")
        raw_values = raw_row.get("values")
        if not isinstance(raw_cols, list) or not isinstance(raw_values, list):
            raise_business(
                E_WRITE_CONFLICT,
                case_id=case_id,
                segment=f"rows[{index}]",
                message="col_indices and values must be lists",
                batch_id=batch_id,
                write_version=write_version,
            )
        if len(raw_cols) != len(raw_values):
            raise_business(
                E_WRITE_CONFLICT,
                case_id=case_id,
                segment=f"rows[{index}].values",
                message="col_indices and values lengths differ",
                batch_id=batch_id,
                write_version=write_version,
                columns=len(raw_cols),
                values=len(raw_values),
            )
        pairs: list[tuple[int, float]] = []
        for position, (col, value) in enumerate(zip(raw_cols, raw_values)):
            if not _is_int(col) or col < 0 or col >= cols:
                raise_business(
                    E_WRITE_RANGE,
                    case_id=case_id,
                    segment=f"rows[{index}].col_indices[{position}]",
                    message="column index is outside the matrix",
                    batch_id=batch_id,
                    write_version=write_version,
                    column_index=col,
                    column_bound=cols,
                )
            try:
                float_value = float(value)
            except (TypeError, ValueError, OverflowError):
                raise_business(
                    E_WRITE_CONFLICT,
                    case_id=case_id,
                    segment=f"rows[{index}].values[{position}]",
                    message="value must be numeric",
                    batch_id=batch_id,
                    write_version=write_version,
                )
            if not float_value == float_value or float_value in {
                float("inf"),
                float("-inf"),
            }:
                raise_business(
                    E_WRITE_CONFLICT,
                    case_id=case_id,
                    segment=f"rows[{index}].values[{position}]",
                    message="value must be finite",
                    batch_id=batch_id,
                    write_version=write_version,
                )
            pairs.append((col, float_value))
        pairs.sort(key=lambda pair: pair[0])
        for left, right in zip(pairs, pairs[1:]):
            if left[0] == right[0]:
                raise_business(
                    E_WRITE_CONFLICT,
                    case_id=case_id,
                    segment=f"rows[{index}].col_indices",
                    message="column indices must be unique within a row write",
                    batch_id=batch_id,
                    write_version=write_version,
                    row=row_number,
                    duplicate_column=left[0],
                )
        parsed_rows.append(
            RowWrite(
                row=row_number,
                col_indices=tuple(pair[0] for pair in pairs),
                values=tuple(pair[1] for pair in pairs),
            )
        )
    missing = sorted(set(range(row_start, row_end)) - seen_rows)
    if missing:
        raise_business(
            E_WRITE_RANGE,
            case_id=case_id,
            segment="rows",
            message="replace batches must supply exactly one row object for every row in the interval",
            batch_id=batch_id,
            write_version=write_version,
            missing_rows=missing,
        )
    parsed_rows.sort(key=lambda item: item.row)
    return WriteBatch(
        batch_id=batch_id,
        write_version=write_version,
        row_start=row_start,
        row_end=row_end,
        mode=mode,
        rows=tuple(parsed_rows),
        checksum=expected_checksum,
    )
