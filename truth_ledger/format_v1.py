from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .errors import LedgerError
from .formats_common import ensure_reader_version, load_json_document
from .hashing import write_json_atomic
from .ingest import validate_csr
from .model import CSRMatrix
from .parsing import parse_float, parse_int, parse_int_array, require_dict, require_fields


V1_RECORD_FIELDS = ("row_index", "col_idx", "value", "timestamp")


def to_v1_dict(matrix: CSRMatrix) -> dict[str, Any]:
    flat_table: list[list[Any]] = []
    for row in range(matrix.nrows):
        start = int(matrix.row_ptr[row])
        end = int(matrix.row_ptr[row + 1])
        for entry in range(start, end):
            flat_table.append(
                [
                    row,
                    int(matrix.col_idx[entry]),
                    float(matrix.values[entry]),
                    int(matrix.timestamps[entry]),
                ]
            )
    return {
        "ledger": "csr-truth-ledger",
        "format": "v1-row-major-flat-table",
        "version": 1,
        "case_id": matrix.case_id,
        "seed": matrix.seed,
        "config": matrix.config,
        "nrows": matrix.nrows,
        "ncols": matrix.ncols,
        "write_version": matrix.write_version,
        "record_fields": list(V1_RECORD_FIELDS),
        "row_ptr": matrix.row_ptr.tolist(),
        "flat_table": flat_table,
        "content_hash": matrix.digest,
    }


def write_v1(path: str | Path, matrix: CSRMatrix) -> Path:
    target = Path(path)
    write_json_atomic(target, to_v1_dict(matrix))
    return target


def read_v1(path: str | Path) -> tuple[CSRMatrix, dict[str, Any]]:
    stage = "format_v1"
    data = load_json_document(path, stage=stage)
    data = require_dict(data, stage=stage)
    ensure_reader_version(data, 1, stage=stage)
    require_fields(
        data,
        (
            "case_id",
            "seed",
            "nrows",
            "ncols",
            "row_ptr",
            "flat_table",
        ),
        stage=stage,
        where="v1 top-level",
    )

    nrows = parse_int(data["nrows"], field="nrows", stage=stage)
    ncols = parse_int(data["ncols"], field="ncols", stage=stage)
    if nrows < 0 or ncols <= 0:
        raise LedgerError(
            "E_FORMAT",
            "v1 dimensions must be non-negative rows and positive columns",
            stage=stage,
            details={"nrows": nrows, "ncols": ncols},
        )
    row_ptr = parse_int_array(data["row_ptr"], field="row_ptr", stage=stage)
    if len(row_ptr) != nrows + 1:
        raise LedgerError(
            "E_FORMAT",
            "v1 row pointer field count does not match nrows + 1",
            stage=stage,
            details={"expected": nrows + 1, "actual": len(row_ptr)},
        )
    flat_table = data["flat_table"]
    if not isinstance(flat_table, list):
        raise LedgerError(
            "E_FORMAT",
            "v1 flat_table must be a list of four-field records",
            stage=stage,
            details={"actual_type": type(flat_table).__name__},
        )

    rows: list[int] = []
    cols: list[int] = []
    values: list[float] = []
    timestamps: list[int] = []
    for record_index, record in enumerate(flat_table):
        where = f"flat_table[{record_index}]"
        if not isinstance(record, list):
            raise LedgerError(
                "E_FORMAT",
                "v1 row-major record must be an array of exactly four fields",
                stage=stage,
                details={
                    "record_index": record_index,
                    "expected_fields": list(V1_RECORD_FIELDS),
                    "actual_type": type(record).__name__,
                },
            )
        if len(record) != len(V1_RECORD_FIELDS):
            raise LedgerError(
                "E_FORMAT",
                "v1 row field count does not match the four-field flat schema",
                stage=stage,
                details={
                    "record_index": record_index,
                    "expected_field_count": len(V1_RECORD_FIELDS),
                    "actual_field_count": len(record),
                },
            )
        row = parse_int(record[0], field=V1_RECORD_FIELDS[0], stage=stage, where=where)
        col = parse_int(record[1], field=V1_RECORD_FIELDS[1], stage=stage, where=where)
        value = parse_float(record[2], field=V1_RECORD_FIELDS[2], stage=stage, where=where)
        timestamp = parse_int(record[3], field=V1_RECORD_FIELDS[3], stage=stage, where=where)
        rows.append(row)
        cols.append(col)
        values.append(value)
        timestamps.append(timestamp)

    if row_ptr and int(row_ptr[0]) != 0:
        raise LedgerError("E_FORMAT", "v1 row_ptr must start at zero", stage=stage)
    if any(int(row_ptr[i]) > int(row_ptr[i + 1]) for i in range(len(row_ptr) - 1)):
        raise LedgerError("E_STRUCT", "v1 row_ptr is not monotone", stage=stage)
    if int(row_ptr[-1]) != len(flat_table):
        raise LedgerError(
            "E_FORMAT",
            "v1 row_ptr final NNZ does not match flat_table row count",
            stage=stage,
            details={"row_ptr_nnz": int(row_ptr[-1]), "flat_records": len(flat_table)},
        )

    for row in range(nrows):
        start, end = int(row_ptr[row]), int(row_ptr[row + 1])
        expected_rows = rows[start:end]
        if any(item != row for item in expected_rows):
            bad_position = next(
                offset for offset, item in enumerate(expected_rows) if item != row
            )
            raise LedgerError(
                "E_FORMAT",
                "v1 flat table is not aligned with row_ptr",
                stage=stage,
                details={"row": row, "entry_index": start + bad_position},
            )
        row_columns = cols[start:end]
        seen: set[int] = set()
        for col in row_columns:
            if col in seen:
                raise LedgerError(
                    "E_INDEX_DUP",
                    "duplicate column index in v1 row",
                    stage=stage,
                    details={"row": row, "col_idx": col},
                )
            seen.add(col)

    matrix = CSRMatrix(
        case_id=str(data.get("case_id", "unknown")),
        seed=parse_int(data.get("seed", 0), field="seed", stage=stage) if "seed" in data else 0,
        config=dict(data.get("config", {})),
        nrows=nrows,
        ncols=ncols,
        write_version=int(data.get("write_version", 0)),
        row_ptr=np.asarray(row_ptr, dtype=np.int64),
        col_idx=np.asarray(cols, dtype=np.int64),
        values=np.asarray(values, dtype=np.float64),
        timestamps=np.asarray(timestamps, dtype=np.int64),
    )
    validate_csr(matrix, stage=stage)
    info = {
        "version": 1,
        "format": data.get("format", "v1-row-major-flat-table"),
        "path": str(path),
        "record_count": len(flat_table),
        "declared_content_hash": data.get("content_hash"),
    }
    return matrix, info
