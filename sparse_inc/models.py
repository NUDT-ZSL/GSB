"""Immutable in-memory state used by writes, snapshots, and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import hash_array_f64, sha256_json
from .csr import CSRMatrix


@dataclass(frozen=True)
class RowState:
    row: int
    version: int
    active: bool
    col_indices: tuple[int, ...]
    values: tuple[float, ...]
    content_checksum: str

    @classmethod
    def from_csr_row(cls, matrix: CSRMatrix, row: int) -> "RowState":
        cols, values = matrix.row_entries(row)
        checksum = row_content_checksum(row, True, cols, values)
        return cls(row, 0, True, cols, values, checksum)

    @classmethod
    def replaced(
        cls,
        row: int,
        version: int,
        col_indices: tuple[int, ...],
        values: tuple[float, ...],
    ) -> "RowState":
        checksum = row_content_checksum(row, True, col_indices, values)
        return cls(row, version, True, col_indices, values, checksum)

    @classmethod
    def invalidated(cls, row: int, version: int) -> "RowState":
        return cls(row, version, False, (), (), row_content_checksum(row, False, (), ()))

    def owner_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "write_version": self.version,
            "active": self.active,
            "content_checksum": self.content_checksum,
        }

    def input_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "write_version": self.version,
            "active": self.active,
            "content_checksum": self.content_checksum,
        }


def row_content_checksum(
    row: int,
    active: bool,
    col_indices: tuple[int, ...],
    values: tuple[float, ...],
) -> str:
    return sha256_json(
        {
            "row": row,
            "active": active,
            "col_indices": list(col_indices),
            "values": list(values),
        }
    )


@dataclass(frozen=True)
class RowBlock:
    block_id: str
    row_start: int
    row_end: int
    result: np.ndarray
    digest: str
    input_digest: str
    last_write_version: int

    def __post_init__(self) -> None:
        array = np.ascontiguousarray(self.result, dtype=np.float64).copy()
        array.setflags(write=False)
        object.__setattr__(self, "result", array)

    @property
    def interval(self) -> tuple[int, int]:
        return (self.row_start, self.row_end)

    def interval_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "row_start": self.row_start,
            "row_end": self.row_end,
        }

    def summary_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "last_write_version": self.last_write_version,
            "input_digest": self.input_digest,
            "result_digest": self.digest,
        }


@dataclass(frozen=True)
class Ledger:
    version: int
    accepted: tuple[dict[str, Any], ...]
    row_owners: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "row_owners": list(self.row_owners),
        }

    @property
    def checksum(self) -> str:
        return sha256_json(self.payload())

    def accepted_ids(self) -> set[str]:
        return {
            str(item["batch"]["batch_id"])
            for item in self.accepted
            if isinstance(item.get("batch"), dict) and "batch_id" in item["batch"]
        }


@dataclass(frozen=True)
class RuntimeState:
    ledger: Ledger
    blocks: tuple[RowBlock, ...]
    row_states: tuple[RowState, ...]
    ledger_snapshot: dict[str, Any]
    result_snapshot: dict[str, Any]


@dataclass(frozen=True)
class IncrementalEvent:
    batch_id: str
    write_version: int
    row_interval: tuple[int, int]
    recompute_intervals: tuple[dict[str, Any], ...]
    reuse_intervals: tuple[dict[str, Any], ...]
    result_changed: bool
    degradation_categories: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "write_version": self.write_version,
            "trigger_write_version": self.write_version,
            "row_interval": {
                "row_start": self.row_interval[0],
                "row_end": self.row_interval[1],
            },
            "recompute_intervals": list(self.recompute_intervals),
            "reuse_intervals": list(self.reuse_intervals),
            "result_changed": self.result_changed,
            "degraded": bool(self.degradation_categories),
            "degradation_categories": list(self.degradation_categories),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IncrementalEvent":
        interval = raw["row_interval"]
        return cls(
            batch_id=str(raw["batch_id"]),
            write_version=int(raw["write_version"]),
            row_interval=(int(interval["row_start"]), int(interval["row_end"])),
            recompute_intervals=tuple(dict(item) for item in raw.get("recompute_intervals", [])),
            reuse_intervals=tuple(dict(item) for item in raw.get("reuse_intervals", [])),
            result_changed=bool(raw.get("result_changed", True)),
            degradation_categories=tuple(str(item) for item in raw.get("degradation_categories", [])),
        )


def result_checksum(result: np.ndarray) -> str:
    return hash_array_f64(result)
