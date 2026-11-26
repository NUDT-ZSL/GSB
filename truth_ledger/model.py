from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .hashing import arrays_digest, canonical_hash


@dataclass
class CSRFragment:
    row_start: int
    row_end: int
    row_ptr: np.ndarray
    col_idx: np.ndarray
    values: np.ndarray
    timestamps: np.ndarray

    def __post_init__(self) -> None:
        self.row_start = int(self.row_start)
        self.row_end = int(self.row_end)
        self.row_ptr = np.asarray(self.row_ptr, dtype=np.int64)
        self.col_idx = np.asarray(self.col_idx, dtype=np.int64)
        self.values = np.asarray(self.values, dtype=np.float64)
        self.timestamps = np.asarray(self.timestamps, dtype=np.int64)

    @property
    def nrows(self) -> int:
        return self.row_end - self.row_start

    @property
    def nnz(self) -> int:
        return int(self.col_idx.size)

    @property
    def digest(self) -> str:
        return arrays_digest(
            f"CSR-FRAGMENT/v1/{self.row_start}/{self.row_end}",
            row_ptr=self.row_ptr,
            col_idx=self.col_idx,
            values=self.values,
            timestamps=self.timestamps,
        )

    def column_range(self) -> tuple[int | None, int | None]:
        if self.col_idx.size == 0:
            return None, None
        return int(self.col_idx.min()), int(self.col_idx.max())

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "row_start": self.row_start,
            "row_end": self.row_end,
            "row_ptr": self.row_ptr.tolist(),
            "col_idx": self.col_idx.tolist(),
            "values": self.values.tolist(),
            "timestamps": self.timestamps.tolist(),
            "content_hash": self.digest,
        }


@dataclass
class CSRMatrix:
    case_id: str
    seed: int
    nrows: int
    ncols: int
    row_ptr: np.ndarray
    col_idx: np.ndarray
    values: np.ndarray
    timestamps: np.ndarray
    config: dict[str, Any] = field(default_factory=dict)
    write_version: int = 0

    def __post_init__(self) -> None:
        self.seed = int(self.seed)
        self.nrows = int(self.nrows)
        self.ncols = int(self.ncols)
        self.write_version = int(self.write_version)
        self.row_ptr = np.asarray(self.row_ptr, dtype=np.int64)
        self.col_idx = np.asarray(self.col_idx, dtype=np.int64)
        self.values = np.asarray(self.values, dtype=np.float64)
        self.timestamps = np.asarray(self.timestamps, dtype=np.int64)

    @property
    def nnz(self) -> int:
        return int(self.col_idx.size)

    @property
    def digest(self) -> str:
        return arrays_digest(
            f"CSR-MATRIX/v1/{self.case_id}/{self.seed}/{self.nrows}/{self.ncols}",
            row_ptr=self.row_ptr,
            col_idx=self.col_idx,
            values=self.values,
            timestamps=self.timestamps,
        )

    def interval_bounds(self) -> list[tuple[int, int]]:
        interval_rows = int(self.config.get("interval_rows", 3))
        bounds = list(range(0, self.nrows, interval_rows))
        if not bounds or bounds[-1] != self.nrows:
            bounds.append(self.nrows)
        return [(int(bounds[i]), int(bounds[i + 1])) for i in range(len(bounds) - 1)]

    def interval_id(self, row_start: int, row_end: int) -> str:
        bounds = self.interval_bounds()
        for index, (start, end) in enumerate(bounds):
            if start == row_start and end == row_end:
                return f"R{index:02d}:{start}-{end}"
        return f"X:{row_start}-{row_end}"

    def fragment(self, row_start: int, row_end: int) -> CSRFragment:
        if not (0 <= row_start <= row_end <= self.nrows):
            raise IndexError(f"fragment out of matrix rows: [{row_start},{row_end})")
        entry_start = int(self.row_ptr[row_start])
        entry_end = int(self.row_ptr[row_end])
        relative_ptr = self.row_ptr[row_start : row_end + 1].copy() - entry_start
        return CSRFragment(
            row_start=row_start,
            row_end=row_end,
            row_ptr=relative_ptr,
            col_idx=self.col_idx[entry_start:entry_end].copy(),
            values=self.values[entry_start:entry_end].copy(),
            timestamps=self.timestamps[entry_start:entry_end].copy(),
        )

    def fragments(self) -> list[CSRFragment]:
        return [self.fragment(start, end) for start, end in self.interval_bounds()]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "seed": self.seed,
            "config": self.config,
            "nrows": self.nrows,
            "ncols": self.ncols,
            "write_version": self.write_version,
            "row_ptr": self.row_ptr.tolist(),
            "col_idx": self.col_idx.tolist(),
            "values": self.values.tolist(),
            "timestamps": self.timestamps.tolist(),
            "content_hash": self.digest,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "CSRMatrix":
        return cls(
            case_id=str(data["case_id"]),
            seed=int(data["seed"]),
            config=dict(data.get("config", {})),
            nrows=int(data["nrows"]),
            ncols=int(data["ncols"]),
            write_version=int(data.get("write_version", 0)),
            row_ptr=data["row_ptr"],
            col_idx=data["col_idx"],
            values=data["values"],
            timestamps=data.get("timestamps", [0] * len(data["col_idx"])),
        )


def rhs_digests(dense_b: np.ndarray, sparse_b: CSRMatrix) -> dict[str, Any]:
    dense = np.ascontiguousarray(dense_b, dtype="<f8")
    dense_digest = arrays_digest(
        "DENSE-RHS/v1",
        values=dense,
    )
    sparse_digest = sparse_b.digest
    row_digests = {
        str(row): sparse_b.fragment(row, row + 1).digest
        for row in range(sparse_b.nrows)
    }
    return {
        "dense_b_digest": dense_digest,
        "sparse_b_digest": sparse_digest,
        "sparse_row_digests": row_digests,
        "config_digest": canonical_hash(
            {"dense_shape": list(dense.shape), "sparse_shape": [sparse_b.nrows, sparse_b.ncols]}
        ),
    }
