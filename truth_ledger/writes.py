from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import LedgerError
from .ingest import validate_csr
from .model import CSRFragment, CSRMatrix
from .parsing import parse_int, parse_int_array, require_dict, require_fields


@dataclass
class ParsedWrite:
    row_start: int
    row_end: int
    fragment: CSRFragment
    content_hash: str
    raw: dict[str, Any]


def _parse_write(item: Any, matrix: CSRMatrix, index: int) -> ParsedWrite:
    stage = "write_receive"
    where = f"writes[{index}]"
    write = require_dict(item, stage=stage, what=where)
    require_fields(
        write,
        ("row_start", "row_end", "row_ptr", "col_idx", "values", "timestamps", "content_hash"),
        stage=stage,
        where=where,
    )
    row_start = parse_int(write["row_start"], field="row_start", stage=stage, where=where)
    row_end = parse_int(write["row_end"], field="row_end", stage=stage, where=where)
    if not (0 <= row_start < row_end <= matrix.nrows):
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write row interval is outside the CSR row domain",
            stage=stage,
            details={"where": where, "row_start": row_start, "row_end": row_end},
        )
    row_ptr = parse_int_array(write["row_ptr"], field="row_ptr", stage=stage, where=where)
    if len(row_ptr) != row_end - row_start + 1:
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write row_ptr length does not match affected row interval",
            stage=stage,
            details={"where": where},
        )
    col_idx = parse_int_array(write["col_idx"], field="col_idx", stage=stage, where=where)
    values_value = write["values"]
    if not isinstance(values_value, list):
        raise LedgerError("E_PARSE", "write values must be an array", stage=stage)
    from .parsing import parse_float

    values = [
        parse_float(value, field="values", stage=stage, where=where)
        for value in values_value
    ]
    timestamps = parse_int_array(
        write["timestamps"], field="timestamps", stage=stage, where=where
    )
    if not (len(col_idx) == len(values) == len(timestamps) == int(row_ptr[-1])):
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write arrays and row_ptr NNZ lengths disagree",
            stage=stage,
            details={"where": where},
        )
    fragment = CSRFragment(
        row_start=row_start,
        row_end=row_end,
        row_ptr=np.asarray(row_ptr, dtype=np.int64),
        col_idx=np.asarray(col_idx, dtype=np.int64),
        values=np.asarray(values, dtype=np.float64),
        timestamps=np.asarray(timestamps, dtype=np.int64),
    )
    content_hash = str(write["content_hash"])
    if fragment.digest != content_hash:
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write interval content hash mismatch; batch rejected atomically",
            stage=stage,
            details={
                "reason": "hash_mismatch",
                "where": where,
                "row_start": row_start,
                "row_end": row_end,
                "expected_hash": content_hash,
                "computed_hash": fragment.digest,
            },
        )
    return ParsedWrite(
        row_start=row_start,
        row_end=row_end,
        fragment=fragment,
        content_hash=content_hash,
        raw=write,
    )


def replace_fragment(matrix: CSRMatrix, write: ParsedWrite, write_version: int) -> CSRMatrix:
    start = write.row_start
    end = write.row_end
    fragment = write.fragment
    if int(fragment.row_ptr[0]) != 0:
        raise LedgerError(
            "E_STRUCT",
            "replacement fragment row_ptr must start at zero",
            stage="write_apply",
            details={"row_start": start, "row_end": end},
        )

    old_entry_start = int(matrix.row_ptr[start])
    old_entry_end = int(matrix.row_ptr[end])
    old_nnz = old_entry_end - old_entry_start
    new_nnz = fragment.nnz
    pointer_delta = new_nnz - old_nnz
    base_pointer = int(matrix.row_ptr[start])

    new_row_ptr = np.concatenate(
        [
            matrix.row_ptr[: start + 1],
            base_pointer + fragment.row_ptr[1:],
            matrix.row_ptr[end + 1 :] + pointer_delta,
        ]
    ).astype(np.int64, copy=False)
    new_col_idx = np.concatenate(
        [
            matrix.col_idx[:old_entry_start],
            fragment.col_idx,
            matrix.col_idx[old_entry_end:],
        ]
    ).astype(np.int64, copy=False)
    new_values = np.concatenate(
        [
            matrix.values[:old_entry_start],
            fragment.values,
            matrix.values[old_entry_end:],
        ]
    ).astype(np.float64, copy=False)
    new_timestamps = np.concatenate(
        [
            matrix.timestamps[:old_entry_start],
            fragment.timestamps,
            matrix.timestamps[old_entry_end:],
        ]
    ).astype(np.int64, copy=False)

    return CSRMatrix(
        case_id=matrix.case_id,
        seed=matrix.seed,
        nrows=matrix.nrows,
        ncols=matrix.ncols,
        row_ptr=new_row_ptr,
        col_idx=new_col_idx,
        values=new_values,
        timestamps=new_timestamps,
        config=matrix.config,
        write_version=write_version,
    )


def _overlaps(left: ParsedWrite, right: ParsedWrite) -> bool:
    return left.row_start < right.row_end and right.row_start < left.row_end


def receive_write_batch(
    event: dict[str, Any],
    matrix: CSRMatrix,
    *,
    last_version: int | None = None,
) -> tuple[CSRMatrix, dict[str, Any]]:
    stage = "write_receive"
    event = require_dict(event, stage=stage, what="write event")
    require_fields(event, ("write_version", "writes"), stage=stage, where="write event")
    write_version = parse_int(
        event["write_version"], field="write_version", stage=stage, where="write event"
    )
    known_version = matrix.write_version if last_version is None else int(last_version)
    if write_version <= known_version:
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write version is not strictly newer than committed version",
            stage=stage,
            details={
                "reason": "version_regressed",
                "write_version": write_version,
                "committed_version": known_version,
            },
        )

    raw_writes = event["writes"]
    if not isinstance(raw_writes, list) or not raw_writes:
        raise LedgerError(
            "E_WRITE_CONFLICT",
            "write batch must contain a non-empty writes array",
            stage=stage,
            details={"write_version": write_version},
        )
    writes = [_parse_write(item, matrix, index) for index, item in enumerate(raw_writes)]

    for i in range(len(writes)):
        for j in range(i + 1, len(writes)):
            if _overlaps(writes[i], writes[j]):
                raise LedgerError(
                    "E_WRITE_CONFLICT",
                    "overlapping or duplicate row intervals in one write version",
                    stage=stage,
                    details={
                        "reason": "interval_overlap",
                        "write_version": write_version,
                        "intervals": [
                            [writes[i].row_start, writes[i].row_end],
                            [writes[j].row_start, writes[j].row_end],
                        ],
                    },
                )

    ordered = sorted(writes, key=lambda item: item.row_start)
    next_matrix = matrix
    interval_bounds = matrix.interval_bounds()
    affected_records: list[dict[str, Any]] = []
    invalidated: dict[str, dict[str, Any]] = {}

    for write in ordered:
        next_matrix = replace_fragment(next_matrix, write, write_version)
        col_min, col_max = write.fragment.column_range()
        mapped: list[str] = []
        for interval_index, (start, end) in enumerate(interval_bounds):
            if write.row_start < end and start < write.row_end:
                interval_id = f"R{interval_index:02d}:{start}-{end}"
                mapped.append(interval_id)
                invalidated[interval_id] = {
                    "interval_id": interval_id,
                    "row_start": start,
                    "row_end": end,
                    "trigger_write_version": write_version,
                }
        affected_records.append(
            {
                "row_start": write.row_start,
                "row_end": write.row_end,
                "content_hash": write.content_hash,
                "col_min": col_min,
                "col_max": col_max,
                "column_index_range": None
                if col_min is None
                else [col_min, col_max + 1],
                "mapped_intervals": mapped,
                "trigger_write_version": write_version,
            }
        )

    validate_csr(next_matrix, stage=stage)
    receipt = {
        "ok": True,
        "code": None,
        "write_version": write_version,
        "applied_in_row_start_order": True,
        "affected_row_ranges": affected_records,
        "invalidated_intervals": sorted(
            invalidated.values(), key=lambda item: item["row_start"]
        ),
        "trigger_write_version": write_version,
        "committed_content_hash": next_matrix.digest,
    }
    return next_matrix, receipt
