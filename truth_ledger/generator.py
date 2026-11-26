from __future__ import annotations

from typing import Any

import numpy as np

from .model import CSRMatrix
from .writes import ParsedWrite, replace_fragment


DEFAULT_CONFIG: dict[str, Any] = {
    "nrows": 12,
    "ncols": 16,
    "dense_k": 4,
    "sparse_q": 6,
    "interval_rows": 3,
    "shard_rows": 6,
    "block_cols": 4,
    "density": 0.30,
    "base_timestamp": 1_764_000_000,
}


def get_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if overrides:
        config.update(overrides)
    return config


def generate_csr(
    case_id: str,
    seed: int,
    config: dict[str, Any] | None = None,
    *,
    injection: str | None = None,
) -> CSRMatrix:
    cfg = get_config(config)
    rng = np.random.RandomState(int(seed))
    nrows = int(cfg["nrows"])
    ncols = int(cfg["ncols"])
    density = float(cfg["density"])
    max_per_row = max(2, int(round(ncols * density * 2.0)))

    row_ptr = [0]
    columns: list[int] = []
    values: list[float] = []
    timestamps: list[int] = []
    base_ts = int(cfg["base_timestamp"]) + (int(seed) % 100_000) * 1000

    for row in range(nrows):
        count = int(rng.randint(1, max_per_row + 1))
        row_cols = np.sort(rng.choice(ncols, size=count, replace=False))
        row_values = rng.uniform(-1.0, 1.0, size=count)
        for offset, col in enumerate(row_cols):
            columns.append(int(col))
            values.append(float(row_values[offset]))
            timestamps.append(base_ts + row * 997 + offset * 37)
        row_ptr.append(len(columns))

    matrix = CSRMatrix(
        case_id=case_id,
        seed=int(seed),
        config=cfg,
        nrows=nrows,
        ncols=ncols,
        row_ptr=np.asarray(row_ptr, dtype=np.int64),
        col_idx=np.asarray(columns, dtype=np.int64),
        values=np.asarray(values, dtype=np.float64),
        timestamps=np.asarray(timestamps, dtype=np.int64),
    )

    if injection == "nonmonotonic_row_ptr":
        matrix.row_ptr = matrix.row_ptr.copy()
        anchor = min(2, nrows - 1)
        matrix.row_ptr[anchor] = matrix.row_ptr[anchor + 1] + 1
    elif injection == "column_oob":
        matrix.col_idx = matrix.col_idx.copy()
        matrix.col_idx[0] = ncols + 7
    elif injection == "nnz_length":
        matrix.values = matrix.values[:-1].copy()
    return matrix


def generate_rhs(
    seed: int, config: dict[str, Any] | None = None, case_id: str = "rhs"
) -> tuple[np.ndarray, CSRMatrix]:
    cfg = get_config(config)
    ncols = int(cfg["ncols"])
    dense_k = int(cfg["dense_k"])
    sparse_q = int(cfg["sparse_q"])
    rng = np.random.RandomState(int(seed) + 7919)
    dense_b = (0.25 * rng.randn(ncols, dense_k)).astype(np.float64)

    sparse_rng = np.random.RandomState(int(seed) + 104729)
    row_ptr = [0]
    cols: list[int] = []
    vals: list[float] = []
    timestamps: list[int] = []
    for row in range(ncols):
        count = int(sparse_rng.randint(1, max(2, sparse_q // 2 + 1)))
        row_cols = np.sort(sparse_rng.choice(sparse_q, size=count, replace=False))
        row_values = sparse_rng.uniform(-1.0, 1.0, size=count)
        for offset, col in enumerate(row_cols):
            cols.append(int(col))
            vals.append(float(row_values[offset]))
            timestamps.append(1_764_000_000 + row * 131 + offset * 17)
        row_ptr.append(len(cols))
    sparse_b = CSRMatrix(
        case_id=f"{case_id}__sparse_rhs",
        seed=int(seed) + 104729,
        config={**cfg, "nrows": ncols, "ncols": sparse_q},
        nrows=ncols,
        ncols=sparse_q,
        row_ptr=np.asarray(row_ptr, dtype=np.int64),
        col_idx=np.asarray(cols, dtype=np.int64),
        values=np.asarray(vals, dtype=np.float64),
        timestamps=np.asarray(timestamps, dtype=np.int64),
    )
    return dense_b, sparse_b


def mutate_fragment_for_write(
    matrix: CSRMatrix,
    row_start: int,
    row_end: int,
    write_version: int,
    interval_index: int,
    *,
    structural: bool,
    seed: int,
) -> ParsedWrite:
    fragment = matrix.fragment(row_start, row_end)
    rng = np.random.RandomState(
        int(seed) + 1_000_003 * int(write_version) + 97 * int(interval_index)
    )
    rows: list[list[tuple[int, float, int]]] = []
    for local_row in range(fragment.nrows):
        start = int(fragment.row_ptr[local_row])
        end = int(fragment.row_ptr[local_row + 1])
        rows.append(
            [
                (
                    int(fragment.col_idx[p]),
                    float(fragment.values[p]),
                    int(fragment.timestamps[p]),
                )
                for p in range(start, end)
            ]
        )

    if structural:
        missing_candidates: list[int] = []
        removable_candidates: list[int] = []
        for local_row, entries in enumerate(rows):
            present = {col for col, _, _ in entries}
            if len(present) < matrix.ncols:
                missing_candidates.append(local_row)
            if len(entries) > 1:
                removable_candidates.append(local_row)
        if missing_candidates and (not removable_candidates or write_version % 2 == 1):
            local_row = int(rng.choice(missing_candidates))
            present = {col for col, _, _ in rows[local_row]}
            missing = [col for col in range(matrix.ncols) if col not in present]
            col = int(missing[int(rng.randint(len(missing)))])
            rows[local_row].append(
                (
                    col,
                    float(0.125 * write_version + 0.03125 * (interval_index + 1)),
                    int(matrix.config["base_timestamp"])
                    + write_version * 1_000_000
                    + local_row * 101,
                )
            )
            rows[local_row].sort(key=lambda item: item[0])
        elif removable_candidates:
            local_row = int(rng.choice(removable_candidates))
            remove_position = int(rng.randint(len(rows[local_row])))
            rows[local_row].pop(remove_position)

    new_rows: list[int] = [0]
    new_cols: list[int] = []
    new_values: list[float] = []
    new_timestamps: list[int] = []
    for local_row, entries in enumerate(rows):
        for position, (col, value, _old_timestamp) in enumerate(entries):
            updated_value = (
                float(value) * (1.0 + 0.0625 * write_version)
                + 0.03125 * (interval_index + local_row + position + 1)
            )
            new_cols.append(col)
            new_values.append(updated_value)
            new_timestamps.append(
                int(matrix.config["base_timestamp"])
                + write_version * 1_000_000
                + (row_start + local_row) * 997
                + position * 37
            )
        new_rows.append(len(new_cols))

    from .model import CSRFragment

    new_fragment = CSRFragment(
        row_start=row_start,
        row_end=row_end,
        row_ptr=np.asarray(new_rows, dtype=np.int64),
        col_idx=np.asarray(new_cols, dtype=np.int64),
        values=np.asarray(new_values, dtype=np.float64),
        timestamps=np.asarray(new_timestamps, dtype=np.int64),
    )
    raw = new_fragment.to_json_dict()
    return ParsedWrite(
        row_start=row_start,
        row_end=row_end,
        fragment=new_fragment,
        content_hash=new_fragment.digest,
        raw=raw,
    )


def build_deterministic_events(
    matrix: CSRMatrix, seed: int
) -> list[dict[str, Any]]:
    bounds = matrix.interval_bounds()
    if len(bounds) < 4:
        raise ValueError("default demo configuration requires at least four row intervals")
    plans = [
        (1, [1], {1: True}),
        (2, [3, 2], {2: True, 3: False}),
    ]
    current = matrix
    events: list[dict[str, Any]] = []
    for version, target_indices, structural_map in plans:
        writes: list[ParsedWrite] = []
        for interval_index in target_indices:
            start, end = bounds[interval_index]
            writes.append(
                mutate_fragment_for_write(
                    current,
                    start,
                    end,
                    version,
                    interval_index,
                    structural=structural_map.get(interval_index, False),
                    seed=seed,
                )
            )
        for write in sorted(writes, key=lambda item: item.row_start):
            current = replace_fragment(current, write, version)
        events.append(
            {
                "ledger": "csr-write-batch",
                "case_id": matrix.case_id,
                "write_version": version,
                "writes": [write.raw for write in writes],
            }
        )
    return events
