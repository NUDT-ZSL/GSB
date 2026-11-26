from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .bitmap import full_row_bitmap, validate_row_bitmap
from .errors import LedgerError
from .formats_common import ensure_reader_version, load_json_document
from .hashing import sha256_bytes, write_json_atomic
from .ingest import validate_csr
from .model import CSRMatrix
from .parsing import parse_float, parse_int, parse_int_array, require_dict, require_fields


MANIFEST_NAME = "v2_manifest.json"
BITMAP_DB_NAME = "bitmap_index.sqlite"


def _boundaries(nrows: int, shard_rows: int) -> list[tuple[int, int]]:
    if shard_rows <= 0:
        raise ValueError("shard_rows must be positive")
    starts = list(range(0, nrows, shard_rows))
    if not starts or starts[-1] != nrows:
        starts.append(nrows)
    return [(starts[i], starts[i + 1]) for i in range(len(starts) - 1)]


def write_v2(
    directory: str | Path,
    matrix: CSRMatrix,
    *,
    shard_rows: int | None = None,
) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    shard_rows = int(shard_rows or matrix.config.get("shard_rows", 6))
    boundaries = _boundaries(matrix.nrows, shard_rows)

    db_path = root / BITMAP_DB_NAME
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE bitmap_index (
                case_id TEXT NOT NULL,
                shard_id INTEGER PRIMARY KEY,
                row_start INTEGER NOT NULL,
                row_end INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                bitmap BLOB NOT NULL,
                bitmap_digest TEXT NOT NULL
            )
            """
        )
        shard_descriptors: list[dict[str, Any]] = []
        for shard_id, (start, end) in enumerate(boundaries):
            fragment = matrix.fragment(start, end)
            shard_name = f"shard_{shard_id:04d}.json"
            shard_doc = {
                "ledger": "csr-truth-ledger",
                "version": 2,
                "case_id": matrix.case_id,
                "shard_id": shard_id,
                "row_start": start,
                "row_end": end,
                "row_ptr": fragment.row_ptr.tolist(),
                "col_idx": fragment.col_idx.tolist(),
                "values": fragment.values.tolist(),
                "timestamps": fragment.timestamps.tolist(),
                "content_hash": fragment.digest,
            }
            write_json_atomic(root / shard_name, shard_doc)
            bitmap = full_row_bitmap(end - start)
            bitmap_digest = sha256_bytes(bitmap)
            connection.execute(
                """
                INSERT INTO bitmap_index
                (case_id, shard_id, row_start, row_end, row_count, bitmap, bitmap_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    matrix.case_id,
                    shard_id,
                    start,
                    end,
                    end - start,
                    sqlite3.Binary(bitmap),
                    bitmap_digest,
                ),
            )
            shard_descriptors.append(
                {
                    "shard_id": shard_id,
                    "row_start": start,
                    "row_end": end,
                    "path": shard_name,
                    "content_hash": fragment.digest,
                    "bitmap_digest": bitmap_digest,
                }
            )
        connection.commit()
    finally:
        connection.close()

    manifest = {
        "ledger": "csr-truth-ledger",
        "format": "v2-sharded-json-sqlite-bitmap",
        "version": 2,
        "case_id": matrix.case_id,
        "seed": matrix.seed,
        "config": matrix.config,
        "nrows": matrix.nrows,
        "ncols": matrix.ncols,
        "write_version": matrix.write_version,
        "shard_rows": shard_rows,
        "bitmap_db": BITMAP_DB_NAME,
        "shards": shard_descriptors,
        "content_hash": matrix.digest,
    }
    manifest_path = root / MANIFEST_NAME
    write_json_atomic(manifest_path, manifest)
    return manifest_path


def _open_bitmap_db(path: Path, *, stage: str) -> sqlite3.Connection:
    if not path.exists():
        raise LedgerError(
            "E_BITMAP_COVER",
            "v2 SQLite bitmap index is missing",
            stage=stage,
            details={"path": str(path)},
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM bitmap_index LIMIT 1")
        return connection
    except sqlite3.Error as exc:
        raise LedgerError(
            "E_BITMAP_COVER",
            f"v2 bitmap index cannot be opened or queried: {exc}",
            stage=stage,
            details={"path": str(path)},
        ) from exc


def read_v2(directory: str | Path) -> tuple[CSRMatrix, dict[str, Any]]:
    stage = "format_v2"
    root = Path(directory)
    manifest_path = root / MANIFEST_NAME
    data = load_json_document(manifest_path, stage=stage)
    data = require_dict(data, stage=stage, what="v2 manifest")
    ensure_reader_version(data, 2, stage=stage)
    require_fields(
        data,
        ("case_id", "seed", "nrows", "ncols", "bitmap_db", "shards"),
        stage=stage,
        where="v2 manifest",
    )
    nrows = parse_int(data["nrows"], field="nrows", stage=stage)
    ncols = parse_int(data["ncols"], field="ncols", stage=stage)
    shards_value = data["shards"]
    if not isinstance(shards_value, list) or not shards_value:
        raise LedgerError("E_SHARD_BOUND", "v2 manifest must contain shards", stage=stage)

    shards: list[dict[str, Any]] = []
    for index, raw_shard in enumerate(shards_value):
        shard = require_dict(
            raw_shard, stage=stage, what=f"shards[{index}] descriptor"
        )
        require_fields(
            shard,
            ("shard_id", "row_start", "row_end", "path"),
            stage=stage,
            where=f"shards[{index}]",
        )
        shard_id = parse_int(shard["shard_id"], field="shard_id", stage=stage)
        start = parse_int(shard["row_start"], field="row_start", stage=stage)
        end = parse_int(shard["row_end"], field="row_end", stage=stage)
        if start < 0 or end > nrows or start >= end:
            raise LedgerError(
                "E_SHARD_BOUND",
                "v2 shard boundary is outside matrix rows or malformed",
                stage=stage,
                details={
                    "shard_id": shard_id,
                    "row_start": start,
                    "row_end": end,
                    "nrows": nrows,
                },
            )
        shards.append(
            {
                "shard_id": shard_id,
                "row_start": start,
                "row_end": end,
                "path": str(shard["path"]),
                "descriptor": shard,
            }
        )

    shards.sort(key=lambda item: (item["row_start"], item["row_end"], item["shard_id"]))
    expected_start = 0
    for shard in shards:
        if shard["row_start"] != expected_start:
            raise LedgerError(
                "E_SHARD_BOUND",
                "v2 shard boundaries have a gap or overlap",
                stage=stage,
                details={
                    "shard_id": shard["shard_id"],
                    "expected_start": expected_start,
                    "actual_start": shard["row_start"],
                },
            )
        expected_start = shard["row_end"]
    if expected_start != nrows:
        raise LedgerError(
            "E_SHARD_BOUND",
            "v2 shard boundaries do not reach nrows",
            stage=stage,
            details={"expected_end": nrows, "actual_end": expected_start},
        )

    db_value = str(data["bitmap_db"])
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = root / db_path
    connection = _open_bitmap_db(db_path, stage=stage)

    row_ptr_parts: list[np.ndarray] = []
    col_parts: list[np.ndarray] = []
    value_parts: list[np.ndarray] = []
    timestamp_parts: list[np.ndarray] = []
    bitmap_rows_checked = 0
    try:
        global_offset = 0
        for shard in shards:
            shard_id = shard["shard_id"]
            start = shard["row_start"]
            end = shard["row_end"]
            where = f"shard_{shard_id:04d}"
            try:
                cursor = connection.execute(
                    """
                    SELECT row_start, row_end, bitmap, bitmap_digest
                    FROM bitmap_index WHERE shard_id = ?
                    """,
                    (shard_id,),
                )
                row = cursor.fetchone()
            except sqlite3.Error as exc:
                raise LedgerError(
                    "E_BITMAP_COVER",
                    f"bitmap query failed for shard {shard_id}: {exc}",
                    stage=stage,
                ) from exc
            if row is None:
                raise LedgerError(
                    "E_BITMAP_COVER",
                    "SQLite bitmap index has no row for shard",
                    stage=stage,
                    details={"shard_id": shard_id},
                )
            db_start, db_end, bitmap, bitmap_digest = row
            if int(db_start) != start or int(db_end) != end:
                raise LedgerError(
                    "E_BITMAP_COVER",
                    "bitmap index boundary does not match shard manifest",
                    stage=stage,
                    details={
                        "shard_id": shard_id,
                        "manifest": [start, end],
                        "sqlite": [int(db_start), int(db_end)],
                    },
                )
            validate_row_bitmap(
                bytes(bitmap), end - start, stage=stage, where=where
            )
            expected_bitmap_digest = sha256_bytes(bytes(bitmap))
            if bitmap_digest != expected_bitmap_digest:
                raise LedgerError(
                    "E_BITMAP_COVER",
                    "bitmap digest does not match stored BLOB",
                    stage=stage,
                    details={"shard_id": shard_id},
                )
            bitmap_rows_checked += end - start

            shard_path = root / shard["path"]
            shard_data = load_json_document(shard_path, stage=stage)
            shard_data = require_dict(shard_data, stage=stage, what=where)
            ensure_reader_version(shard_data, 2, stage=stage)
            require_fields(
                shard_data,
                ("shard_id", "row_start", "row_end", "row_ptr", "col_idx", "values"),
                stage=stage,
                where=where,
            )
            if parse_int(shard_data["shard_id"], field="shard_id", stage=stage) != shard_id:
                raise LedgerError("E_SHARD_BOUND", "shard id mismatch", stage=stage)
            if (
                parse_int(shard_data["row_start"], field="row_start", stage=stage) != start
                or parse_int(shard_data["row_end"], field="row_end", stage=stage) != end
            ):
                raise LedgerError(
                    "E_SHARD_BOUND",
                    "shard JSON boundary does not match manifest",
                    stage=stage,
                    details={"shard_id": shard_id},
                )
            local_ptr = parse_int_array(
                shard_data["row_ptr"], field="row_ptr", stage=stage, where=where
            )
            if len(local_ptr) != end - start + 1:
                raise LedgerError(
                    "E_FORMAT",
                    "v2 shard row_ptr length does not match shard row count",
                    stage=stage,
                    details={"shard_id": shard_id},
                )
            cols = parse_int_array(
                shard_data["col_idx"], field="col_idx", stage=stage, where=where
            )
            values_value = shard_data["values"]
            if not isinstance(values_value, list):
                raise LedgerError("E_PARSE", "values must be an array", stage=stage)
            vals = [
                parse_float(value, field="values", stage=stage, where=where)
                for value in values_value
            ]
            timestamps_value = shard_data.get("timestamps", [0] * len(cols))
            timestamps = parse_int_array(
                timestamps_value, field="timestamps", stage=stage, where=where
            )
            if not (len(cols) == len(vals) == len(timestamps) == int(local_ptr[-1])):
                raise LedgerError(
                    "E_FORMAT",
                    "v2 shard arrays or row_ptr NNZ lengths disagree",
                    stage=stage,
                    details={"shard_id": shard_id},
                )
            for row_offset in range(end - start):
                row_columns = cols[int(local_ptr[row_offset]) : int(local_ptr[row_offset + 1])]
                seen: set[int] = set()
                for col in row_columns:
                    if col in seen:
                        raise LedgerError(
                            "E_INDEX_DUP",
                            "duplicate column index in v2 shard row",
                            stage=stage,
                            details={"row": start + row_offset, "col_idx": col},
                        )
                    seen.add(col)

            local_ptr_array = np.asarray(local_ptr, dtype=np.int64)
            if global_offset == 0:
                row_ptr_parts.append(local_ptr_array)
            else:
                row_ptr_parts.append(global_offset + local_ptr_array[1:])
            col_parts.append(np.asarray(cols, dtype=np.int64))
            value_parts.append(np.asarray(vals, dtype=np.float64))
            timestamp_parts.append(np.asarray(timestamps, dtype=np.int64))
            global_offset += int(local_ptr[-1])
    finally:
        connection.close()

    row_ptr = np.concatenate(row_ptr_parts) if row_ptr_parts else np.asarray([0], dtype=np.int64)
    col_idx = np.concatenate(col_parts) if col_parts else np.asarray([], dtype=np.int64)
    values = np.concatenate(value_parts) if value_parts else np.asarray([], dtype=np.float64)
    timestamps = (
        np.concatenate(timestamp_parts)
        if timestamp_parts
        else np.asarray([], dtype=np.int64)
    )
    matrix = CSRMatrix(
        case_id=str(data["case_id"]),
        seed=parse_int(data["seed"], field="seed", stage=stage),
        config=dict(data.get("config", {})),
        nrows=nrows,
        ncols=ncols,
        write_version=int(data.get("write_version", 0)),
        row_ptr=row_ptr,
        col_idx=col_idx,
        values=values,
        timestamps=timestamps,
    )
    validate_csr(matrix, stage=stage)
    info = {
        "version": 2,
        "format": data.get("format", "v2-sharded-json-sqlite-bitmap"),
        "path": str(root),
        "shard_count": len(shards),
        "bitmap_rows_checked": bitmap_rows_checked,
        "declared_content_hash": data.get("content_hash"),
    }
    return matrix, info
