#!/usr/bin/env python3
"""Generate the deterministic sparse fragments and their declarations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROWS = 48
COLUMNS = 24
BATCH_BUDGET = 12
CHUNK_SIZE = 4
DENSE_VECTOR = [
    1.0,
    0.5,
    2.0,
    1.5,
    0.25,
    3.0,
    1.25,
    0.75,
    2.5,
    0.4,
    1.8,
    0.9,
    1.1,
    2.2,
    0.6,
    1.4,
    3.1,
    0.35,
    1.9,
    0.8,
    2.7,
    1.3,
    0.45,
    2.4,
]


def canonical_entries() -> list[tuple[int, int, int]]:
    entries: dict[tuple[int, int], int] = {}
    # Row 0 is the single extremely dense row: every product is present.
    for column in range(COLUMNS):
        entries[(0, column)] = 1 + ((column * 3 + 5) % 7)

    # Column 0 is the single extremely dense column. It already includes row 0.
    column_zero_rows = (
        list(range(1, 12))
        + list(range(13, 17))
        + list(range(18, 22))
        + list(range(23, 27))
    )
    for row in column_zero_rows:
        entries[(row, 0)] = 1 + ((row * 5 + 2) % 9)

    # One additional edge on many non-empty rows creates a skewed but
    # recomputable remainder distribution. Rows 12, 17, 22, 29, 32, 33 stay empty.
    extra_rows = (
        column_zero_rows
        + [27, 28, 30, 31]
        + list(range(34, 48))
    )
    for position, row in enumerate(extra_rows):
        column = 1 + ((position * 7) % (COLUMNS - 1))
        entries[(row, column)] = 1 + ((row * 7 + column * 11) % 13)

    return [
        (row, column, value)
        for (row, column), value in sorted(entries.items())
    ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_matrix_market(path: Path, entries: list[tuple[int, int, int]]) -> None:
    lines = [
        "%%MatrixMarket matrix coordinate integer general",
        "% fields: row_id col_id click_purchase_count",
        "% index_base: 1",
        "% weight_unit: count",
        f"{ROWS} {COLUMNS} {len(entries)}",
    ]
    lines.extend(
        f"{row + 1} {column + 1} {value}"
        for row, column, value in entries
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row_ptr(entries: list[tuple[int, int, int]]) -> list[int]:
    pointers = [0]
    cursor = 0
    for row in range(ROWS):
        while cursor < len(entries) and entries[cursor][0] == row:
            cursor += 1
        pointers.append(cursor)
    return pointers


def _column_major(entries: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    return sorted(entries, key=lambda item: (item[1], item[0]))


def _column_ptr(entries: list[tuple[int, int, int]]) -> list[int]:
    pointers = [0]
    cursor = 0
    for column in range(COLUMNS):
        while cursor < len(entries) and entries[cursor][1] == column:
            cursor += 1
        pointers.append(cursor)
    return pointers


def _write_csr(path: Path, entries: list[tuple[int, int, int]]) -> None:
    pointers = _row_ptr(entries)
    lines = [
        "format csr",
        "% 0-based CSR text fragment; weights are permille of canonical counts",
        f"user_count {ROWS}",
        f"item_count {COLUMNS}",
        f"interaction_count {len(entries)}",
        "user_row_ptr " + " ".join(str(value) for value in pointers),
        "user_ids " + " ".join(str(value) for value in range(ROWS)),
        "item_indices " + " ".join(str(column) for _, column, _ in entries),
        "weight_permille "
        + " ".join(str(value * 1000) for _, _, value in entries),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_csc(path: Path, entries: list[tuple[int, int, int]]) -> None:
    ordered = _column_major(entries)
    pointers = _column_ptr(ordered)
    lines = [
        "format csc",
        "% 1-based CSC text fragment; weights are basis points of canonical counts",
        f"user_count {ROWS}",
        f"product_count {COLUMNS}",
        f"interaction_count {len(entries)}",
        "product_col_ptr " + " ".join(str(value) for value in pointers),
        "product_ids " + " ".join(str(value) for value in range(1, COLUMNS + 1)),
        "user_row_indices " + " ".join(str(row + 1) for row, _, _ in ordered),
        "engagement_basis_points "
        + " ".join(str(value * 10000) for _, _, value in ordered),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _version_specs() -> None:
    specs = [
        {
            "schema_version": 1,
            "version": "v1-matrix-market-1based-count",
            "format": "matrix_market",
            "matrix_file": "matrix.mtx",
            "index_base": 1,
            "highest_index": {"row": 48, "column": 24},
            "fields": {
                "row": "row_id",
                "column": "col_id",
                "value": "click_purchase_count",
                "rows": "rows",
                "columns": "columns",
                "nonzero_count": "nnz",
            },
            "weight": {
                "raw_field": "click_purchase_count",
                "unit": "count",
                "canonical_unit": "count",
                "factor": "1",
            },
            "dense_vector": {
                "path": "dense_interest.json",
                "fields": {"ids": "item_ids", "values": "interest"},
                "index_base": 1,
                "expected_length": COLUMNS,
            },
        },
        {
            "schema_version": 1,
            "version": "v2-csr-0based-permille",
            "format": "csr_text",
            "matrix_file": "matrix.csr",
            "index_base": 0,
            "highest_index": {"row": 47, "column": 23},
            "fields": {
                "rows": "user_count",
                "columns": "item_count",
                "nonzero_count": "interaction_count",
                "row_ptr": "user_row_ptr",
                "row_ids": "user_ids",
                "column_indices": "item_indices",
                "values": "weight_permille",
                "row": "user_index",
                "column": "item_index",
                "value": "weight_permille",
            },
            "weight": {
                "raw_field": "weight_permille",
                "unit": "permille",
                "canonical_unit": "count",
                "factor": "0.001",
            },
            "dense_vector": {
                "path": "dense_interest.json",
                "fields": {"ids": "product_indices", "values": "interest_scores"},
                "index_base": 0,
                "expected_length": COLUMNS,
            },
        },
        {
            "schema_version": 1,
            "version": "v3-csc-1based-basis-points",
            "format": "csc_text",
            "matrix_file": "matrix.csc",
            "index_base": 1,
            "highest_index": {"row": 48, "column": 24},
            "fields": {
                "rows": "user_count",
                "columns": "product_count",
                "nonzero_count": "interaction_count",
                "column_ptr": "product_col_ptr",
                "column_ids": "product_ids",
                "row_indices": "user_row_indices",
                "values": "engagement_basis_points",
                "row": "user_key",
                "column": "product_key",
                "value": "engagement_basis_points",
            },
            "weight": {
                "raw_field": "engagement_basis_points",
                "unit": "basis_point",
                "canonical_unit": "count",
                "factor": "0.0001",
            },
            "dense_vector": {
                "path": "dense_interest.json",
                "fields": {"ids": "sku_keys", "values": "interest_scores"},
                "index_base": 1,
                "expected_length": COLUMNS,
            },
        },
    ]
    directories = [
        "v1_matrix_market_1based",
        "v2_csr_0based_permille",
        "v3_csc_1based_basis_points",
    ]
    for directory, spec in zip(directories, specs, strict=True):
        _write_json(DATA / "versions" / directory / "version.json", spec)


def _operation_spec() -> None:
    _write_json(
        DATA / "operation_spec.json",
        {
            "schema_version": 1,
            "operation_id": "interest-diffusion-spmv-v1",
            "direction": "row_accumulate",
            "batch_nonzero_budget": BATCH_BUDGET,
            "dense_vector_chunk_size": CHUNK_SIZE,
            "hotspot_multiplier": "1.5",
            "hotspot_average_basis": "per_batch",
            "tolerance": "1e-9",
            "strategies": ["csr", "csc"],
        },
    )


def main() -> None:
    entries = canonical_entries()
    v1 = DATA / "versions" / "v1_matrix_market_1based"
    v2 = DATA / "versions" / "v2_csr_0based_permille"
    v3 = DATA / "versions" / "v3_csc_1based_basis_points"
    v1.mkdir(parents=True, exist_ok=True)
    v2.mkdir(parents=True, exist_ok=True)
    v3.mkdir(parents=True, exist_ok=True)
    _write_matrix_market(v1 / "matrix.mtx", entries)
    _write_csr(v2 / "matrix.csr", entries)
    _write_csc(v3 / "matrix.csc", entries)
    _write_json(
        v1 / "dense_interest.json",
        {
            "item_ids": list(range(1, COLUMNS + 1)),
            "interest": DENSE_VECTOR,
        },
    )
    _write_json(
        v2 / "dense_interest.json",
        {
            "product_indices": list(range(COLUMNS)),
            "interest_scores": DENSE_VECTOR,
        },
    )
    _write_json(
        v3 / "dense_interest.json",
        {
            "sku_keys": list(range(1, COLUMNS + 1)),
            "interest_scores": DENSE_VECTOR,
        },
    )
    _version_specs()
    _operation_spec()
    print(
        f"generated {len(entries)} canonical nonzeros across "
        f"{len(list((DATA / 'versions').glob('*/version.json')))} versions"
    )


if __name__ == "__main__":
    main()
