from __future__ import annotations

import base64
import binascii
import math
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from .errors import LedgerError
from .formats_common import ensure_reader_version, load_json_document
from .hashing import canonical_json_bytes, sha256_bytes, write_json_atomic
from .ingest import validate_csr
from .model import CSRMatrix
from .parsing import parse_float, parse_int, parse_int_array, require_dict, require_fields


CODEC = "zlib-json-base64-v1"


def _block_boundaries(ncols: int, block_cols: int) -> list[tuple[int, int]]:
    if block_cols <= 0:
        raise ValueError("block_cols must be positive")
    boundaries: list[tuple[int, int]] = []
    for start in range(0, ncols, block_cols):
        boundaries.append((start, min(ncols, start + block_cols)))
    return boundaries


def _block_bitmap(nonempty_blocks: list[bool]) -> str:
    value = 0
    for index, nonempty in enumerate(nonempty_blocks):
        if nonempty:
            value |= 1 << index
    byte_length = max(1, math.ceil(len(nonempty_blocks) / 8))
    return value.to_bytes(byte_length, byteorder="little", signed=False).hex()


def _decode_payload(block: dict[str, Any], *, stage: str, where: str) -> dict[str, Any]:
    require_fields(block, ("codec", "payload", "payload_digest"), stage=stage, where=where)
    if block["codec"] != CODEC:
        raise LedgerError(
            "E_FORMAT",
            "unsupported v3 column-block compression codec",
            stage=stage,
            details={"where": where, "codec": block["codec"], "supported": CODEC},
        )
    try:
        compressed = base64.b64decode(block["payload"], validate=True)
        raw = zlib.decompress(compressed)
    except (binascii.Error, ValueError, zlib.error) as exc:
        raise LedgerError(
            "E_PARSE",
            "v3 column block payload is not valid base64/zlib data",
            stage=stage,
            details={"where": where},
        ) from exc
    digest = sha256_bytes(raw)
    if digest != block["payload_digest"]:
        raise LedgerError(
            "E_PARSE",
            "v3 column block payload digest mismatch",
            stage=stage,
            details={"where": where, "expected": block["payload_digest"], "actual": digest},
        )
    try:
        import json

        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(
            "E_PARSE",
            "v3 column block payload is not valid JSON",
            stage=stage,
            details={"where": where},
        ) from exc
    return require_dict(payload, stage=stage, what=f"{where} payload")


def to_v3_dict(matrix: CSRMatrix, *, block_cols: int | None = None) -> dict[str, Any]:
    block_cols = int(block_cols or matrix.config.get("block_cols", 4))
    boundaries = _block_boundaries(matrix.ncols, block_cols)
    blocks: list[dict[str, Any]] = []
    nonempty: list[bool] = []

    for block_id, (col_start, col_end) in enumerate(boundaries):
        rows: list[int] = []
        entry_indices: list[int] = []
        for row in range(matrix.nrows):
            start = int(matrix.row_ptr[row])
            end = int(matrix.row_ptr[row + 1])
            for entry in range(start, end):
                if col_start <= int(matrix.col_idx[entry]) < col_end:
                    rows.append(row)
                    entry_indices.append(entry)
        index_array = np.asarray(entry_indices, dtype=np.int64)
        global_cols = matrix.col_idx[index_array] if entry_indices else np.asarray([], dtype=np.int64)
        payload = {
            "row_idx": [int(row) for row in rows],
            "local_col_idx": (global_cols - col_start).tolist(),
            "values": matrix.values[index_array].tolist() if entry_indices else [],
            "timestamps": matrix.timestamps[index_array].tolist() if entry_indices else [],
        }
        raw = canonical_json_bytes(payload)
        compressed = zlib.compress(raw, level=9)
        encoded = base64.b64encode(compressed).decode("ascii")
        nonempty.append(len(entry_indices) > 0)
        blocks.append(
            {
                "block_id": block_id,
                "col_start": col_start,
                "col_end": col_end,
                "codec": CODEC,
                "column_mapping": {
                    "local_column_field": "local_col_idx",
                    "global_formula": "col_idx = local_col_idx + col_start",
                    "col_start": col_start,
                },
                "payload": encoded,
                "payload_digest": sha256_bytes(raw),
                "compressed_digest": sha256_bytes(compressed),
                "entry_count": len(entry_indices),
            }
        )

    bitmap_hex = _block_bitmap(nonempty)
    return {
        "ledger": "csr-truth-ledger",
        "format": "v3-column-block-compressed-descriptor",
        "version": 3,
        "case_id": matrix.case_id,
        "seed": matrix.seed,
        "config": matrix.config,
        "nrows": matrix.nrows,
        "ncols": matrix.ncols,
        "write_version": matrix.write_version,
        "block_cols": block_cols,
        "structural_algebra": {
            "addition": "float64_ordered_sum",
            "multiplication": "float64_product",
            "additive_zero": 0.0,
            "accumulation_order": "row_then_ascending_col_then_upstream_row",
        },
        "block_bitmap_hex": bitmap_hex,
        "block_bitmap_digest": sha256_bytes(bitmap_hex.encode("ascii")),
        "bitmap_validation": {
            "scheme": "little-endian-nonempty-column-blocks",
            "digest_field": "block_bitmap_digest",
            "verified_by_reader": True,
        },
        "blocks": blocks,
        "content_hash": matrix.digest,
    }


def write_v3(path: str | Path, matrix: CSRMatrix, *, block_cols: int | None = None) -> Path:
    target = Path(path)
    write_json_atomic(target, to_v3_dict(matrix, block_cols=block_cols))
    return target


def read_v3(path: str | Path) -> tuple[CSRMatrix, dict[str, Any]]:
    stage = "format_v3"
    data = load_json_document(path, stage=stage)
    data = require_dict(data, stage=stage)
    ensure_reader_version(data, 3, stage=stage)
    require_fields(
        data,
        ("case_id", "seed", "nrows", "ncols", "block_cols", "blocks"),
        stage=stage,
        where="v3 top-level",
    )
    nrows = parse_int(data["nrows"], field="nrows", stage=stage)
    ncols = parse_int(data["ncols"], field="ncols", stage=stage)
    block_cols = parse_int(data["block_cols"], field="block_cols", stage=stage)
    if block_cols <= 0:
        raise LedgerError("E_FORMAT", "v3 block_cols must be positive", stage=stage)
    blocks_value = data["blocks"]
    if not isinstance(blocks_value, list):
        raise LedgerError("E_FORMAT", "v3 blocks must be an array", stage=stage)

    expected_boundaries = _block_boundaries(ncols, block_cols)
    if len(blocks_value) != len(expected_boundaries):
        raise LedgerError(
            "E_FORMAT",
            "v3 block count does not match ncols and block_cols",
            stage=stage,
            details={"expected": len(expected_boundaries), "actual": len(blocks_value)},
        )

    all_rows: list[int] = []
    all_cols: list[int] = []
    all_values: list[float] = []
    all_timestamps: list[int] = []
    seen_pairs: set[tuple[int, int]] = set()
    nonempty: list[bool] = []

    for block_index, raw_block in enumerate(blocks_value):
        where = f"blocks[{block_index}]"
        block = require_dict(raw_block, stage=stage, what=where)
        require_fields(
            block,
            ("block_id", "col_start", "col_end", "column_mapping"),
            stage=stage,
            where=where,
        )
        block_id = parse_int(block["block_id"], field="block_id", stage=stage, where=where)
        col_start = parse_int(block["col_start"], field="col_start", stage=stage, where=where)
        col_end = parse_int(block["col_end"], field="col_end", stage=stage, where=where)
        expected_start, expected_end = expected_boundaries[block_index]
        if (
            block_id != block_index
            or col_start != expected_start
            or col_end != expected_end
            or col_start < 0
            or col_end > ncols
            or col_start >= col_end
        ):
            raise LedgerError(
                "E_FORMAT",
                "v3 column block mapping or boundary is invalid",
                stage=stage,
                details={
                    "where": where,
                    "expected": [expected_start, expected_end],
                    "actual": [col_start, col_end],
                },
            )
        mapping = require_dict(
            block["column_mapping"], stage=stage, what=f"{where}.column_mapping"
        )
        if mapping.get("global_formula") != "col_idx = local_col_idx + col_start":
            raise LedgerError(
                "E_FORMAT",
                "v3 column mapping formula is missing or unsupported",
                stage=stage,
                details={"where": where},
            )
        payload = _decode_payload(block, stage=stage, where=where)
        require_fields(
            payload,
            ("row_idx", "local_col_idx", "values"),
            stage=stage,
            where=f"{where}.payload",
        )
        rows = parse_int_array(payload["row_idx"], field="row_idx", stage=stage, where=where)
        local_cols = parse_int_array(
            payload["local_col_idx"], field="local_col_idx", stage=stage, where=where
        )
        if not isinstance(payload["values"], list):
            raise LedgerError("E_PARSE", "payload values must be an array", stage=stage)
        values = [
            parse_float(value, field="values", stage=stage, where=f"{where}.payload")
            for value in payload["values"]
        ]
        timestamps_value = payload.get("timestamps", [0] * len(rows))
        timestamps = parse_int_array(
            timestamps_value, field="timestamps", stage=stage, where=f"{where}.payload"
        )
        if not (len(rows) == len(local_cols) == len(values) == len(timestamps)):
            raise LedgerError(
                "E_FORMAT",
                "v3 payload array lengths disagree",
                stage=stage,
                details={"where": where},
            )
        nonempty.append(len(rows) > 0)
        for row, local_col in zip(rows, local_cols):
            global_col = local_col + col_start
            pair = (row, global_col)
            if pair in seen_pairs:
                raise LedgerError(
                    "E_INDEX_DUP",
                    "duplicate row/column pair in v3 column blocks",
                    stage=stage,
                    details={"row": row, "col_idx": global_col, "block_id": block_id},
                )
            seen_pairs.add(pair)
            all_rows.append(row)
            all_cols.append(global_col)
        all_values.extend(values)
        all_timestamps.extend(timestamps)

    expected_bitmap = _block_bitmap(nonempty)
    if data.get("block_bitmap_hex") != expected_bitmap:
        raise LedgerError(
            "E_BITMAP_COVER",
            "v3 column-block bitmap does not match nonempty blocks",
            stage=stage,
            details={"expected": expected_bitmap, "actual": data.get("block_bitmap_hex")},
        )
    if data.get("block_bitmap_digest") != sha256_bytes(expected_bitmap.encode("ascii")):
        raise LedgerError(
            "E_BITMAP_COVER",
            "v3 column-block bitmap digest is invalid",
            stage=stage,
        )

    order = np.lexsort((np.asarray(all_cols, dtype=np.int64), np.asarray(all_rows, dtype=np.int64)))
    sorted_rows = np.asarray(all_rows, dtype=np.int64)[order]
    col_idx = np.asarray(all_cols, dtype=np.int64)[order]
    values = np.asarray(all_values, dtype=np.float64)[order]
    timestamps = np.asarray(all_timestamps, dtype=np.int64)[order]
    row_ptr = np.zeros(nrows + 1, dtype=np.int64)
    for row in sorted_rows:
        row_ptr[int(row) + 1] += 1
    row_ptr = np.cumsum(row_ptr, dtype=np.int64)

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
        "version": 3,
        "format": data.get("format", "v3-column-block-compressed-descriptor"),
        "path": str(path),
        "block_count": len(blocks_value),
        "declared_content_hash": data.get("content_hash"),
        "structural_algebra": data.get("structural_algebra"),
    }
    return matrix, info
