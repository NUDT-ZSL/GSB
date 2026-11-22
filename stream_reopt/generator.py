from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from .models import BatchSpec, SampleSpec, TableSpec


@dataclass(frozen=True)
class Batch:
    arrival: int
    table: str
    row_count: int
    columns: dict[str, np.ndarray]
    digest: str
    column_digests: dict[str, str]

    def summary(self) -> dict[str, Any]:
        return {
            "arrival": self.arrival,
            "table": self.table,
            "row_count": self.row_count,
            "digest": self.digest,
            "column_digests": dict(sorted(self.column_digests.items())),
        }


def _table_seed(global_seed: int, table_name: str) -> int:
    payload = f"stream-reopt-v1|{global_seed}|{table_name}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:16], "big", signed=False)


def _generator(seed: int, purpose: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{seed}|{purpose}".encode("utf-8")).digest()
    return np.random.Generator(np.random.PCG64(int.from_bytes(digest[:16], "big", signed=False)))


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def _array_digest(array: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"SRC1")
    digest.update(array.dtype.str.encode("ascii"))
    digest.update(struct.pack(">I", array.ndim))
    digest.update(struct.pack(">" + "Q" * array.ndim, *array.shape))
    if array.dtype.kind in {"O", "S", "U"}:
        for value in array.reshape(-1):
            encoded = str(value).encode("utf-8")
            digest.update(struct.pack(">I", len(encoded)))
            digest.update(encoded)
    else:
        digest.update(np.ascontiguousarray(array).tobytes(order="C"))
    return digest.hexdigest()


def _batch_digest(table: str, arrival: int, columns: dict[str, np.ndarray], column_digests: dict[str, str]) -> str:
    digest = hashlib.sha256()
    digest.update(b"SRB1")
    digest.update(_encode_string(table))
    digest.update(struct.pack(">Q", arrival))
    digest.update(struct.pack(">I", len(columns)))
    for name in sorted(columns):
        digest.update(_encode_string(name))
        digest.update(columns[name].dtype.str.encode("ascii"))
        digest.update(_encode_string(column_digests[name]))
    return digest.hexdigest()


class DeterministicGenerator:
    def __init__(self, sample: SampleSpec):
        self.sample = sample
        self.table_map = sample.table_map
        self.batch_specs = sorted(sample.plan.batches, key=lambda batch: batch.arrival)
        self.offset_by_arrival = self._build_offsets()
        self._table_cache: dict[str, dict[str, np.ndarray]] = {}

    def _build_offsets(self) -> dict[int, int]:
        offsets: dict[int, int] = {}
        running = {name: 0 for name in self.table_map}
        for batch in self.batch_specs:
            offsets[batch.arrival] = running[batch.table]
            running[batch.table] += batch.row_count
        return offsets

    def _generate_table(self, table: TableSpec) -> dict[str, np.ndarray]:
        cached = self._table_cache.get(table.name)
        if cached is not None:
            return cached
        seed = _table_seed(self.sample.global_seed, table.name)
        rng = _generator(seed, "table")
        rows = table.row_count
        columns: dict[str, np.ndarray] = {}
        for column in table.columns:
            column_rng = _generator(seed, f"column:{column.name}")
            is_skew_column = table.skew.enabled and table.skew.column == column.name
            if is_skew_column:
                hot_rows = int(round(rows * table.skew.hot_fraction))
                hot_rows = min(max(hot_rows, 1), rows)
                hot_positions = column_rng.choice(rows, size=hot_rows, replace=False)
                hot_mask = np.zeros(rows, dtype=bool)
                hot_mask[hot_positions] = True
                if column.type == "int64":
                    hot_key = int(table.skew.hot_key)
                    if column.ndv == 1:
                        values = np.zeros(rows, dtype=np.int64)
                    else:
                        shifted = column_rng.integers(0, column.ndv - 1, size=rows, dtype=np.int64)
                        values = shifted + (shifted >= hot_key)
                    values[hot_mask] = hot_key
                    columns[column.name] = values.astype(np.int64, copy=False)
                else:
                    values = np.empty(rows, dtype=object)
                    other_keys = [f"k{index:08d}" for index in range(column.ndv) if f"k{index:08d}" != table.skew.hot_key]
                    if not other_keys:
                        values[:] = str(table.skew.hot_key)
                    else:
                        indices = column_rng.integers(0, len(other_keys), size=rows)
                        values[:] = np.asarray(other_keys, dtype=object)[indices]
                        values[hot_mask] = str(table.skew.hot_key)
                    columns[column.name] = values
                continue
            if column.type == "int64":
                columns[column.name] = column_rng.integers(0, column.ndv, size=rows, dtype=np.int64)
            elif column.type == "float64":
                base = column_rng.random(rows, dtype=np.float64)
                columns[column.name] = np.floor(base * column.ndv) / float(column.ndv)
            else:
                indices = column_rng.integers(0, column.ndv, size=rows)
                keys = np.asarray([f"k{index:08d}" for index in range(column.ndv)], dtype=object)
                columns[column.name] = keys[indices]
        result = {name: np.ascontiguousarray(values) for name, values in columns.items()}
        self._table_cache[table.name] = result
        return result

    def batch(self, spec: BatchSpec) -> Batch:
        table = self.table_map[spec.table]
        offset = self.offset_by_arrival[spec.arrival]
        full_table = self._generate_table(table)
        columns = {
            name: np.ascontiguousarray(values[offset : offset + spec.row_count])
            for name, values in full_table.items()
        }
        column_digests = {name: _array_digest(values) for name, values in columns.items()}
        digest = _batch_digest(spec.table, spec.arrival, columns, column_digests)
        return Batch(
            arrival=spec.arrival,
            table=spec.table,
            row_count=spec.row_count,
            columns=columns,
            digest=digest,
            column_digests=column_digests,
        )

    def batch_by_arrival(self, arrival: int) -> Batch:
        for spec in self.batch_specs:
            if spec.arrival == arrival:
                return self.batch(spec)
        raise KeyError(arrival)

    def manifest(self) -> dict[str, Any]:
        batches = [self.batch(spec).summary() for spec in self.batch_specs]
        tables = {
            table.name: {
                "row_count": table.row_count,
                "columns": {
                    name: _array_digest(values)
                    for name, values in sorted(self._generate_table(table).items())
                },
            }
            for table in self.sample.tables
        }
        payload = {
            "sample_id": self.sample.sample_id,
            "global_seed": self.sample.global_seed,
            "tables": tables,
            "batches": batches,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            **payload,
            "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        }
