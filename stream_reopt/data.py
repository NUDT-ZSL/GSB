"""Deterministic columnar batch generation and content digests."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import digest_value
from .models import BatchSpec, ColumnSpec, Plan, Scenario, TableSpec


@dataclass(frozen=True)
class TableBatch:
    name: str
    batch_id: str
    rows: int
    columns: Mapping[str, np.ndarray]
    digest: str

    @property
    def row_count(self) -> int:
        return self.rows

    def input_summary(self) -> dict[str, Any]:
        column_digests = {
            name: _array_digest(self.columns[name], self.columns[name].dtype.kind)
            for name in sorted(self.columns)
        }
        return {
            "table": self.name,
            "rows": self.rows,
            "digest": self.digest,
            "columns": column_digests,
        }


def _seed_for(seed: int, *parts: str | int) -> int:
    material = "\x1f".join(str(part) for part in (seed, *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


def _array_digest(array: np.ndarray, kind: str) -> str:
    h = hashlib.sha256()
    h.update(kind.encode("ascii"))
    if kind in {"i", "u"}:
        h.update(np.asarray(array, dtype="<i8").tobytes(order="C"))
    elif kind == "f":
        h.update(np.asarray(array, dtype="<f8").tobytes(order="C"))
    else:
        for value in array.tolist():
            encoded = str(value).encode("utf-8")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
    return h.hexdigest()


def _hot_count(column: ColumnSpec) -> int:
    if column.skew is None:
        return 0
    if column.skew.hot_values:
        return min(column.ndv, len(column.skew.hot_values))
    return max(1, min(column.ndv - 1, round(column.ndv * column.skew.hot_fraction)))


def _value_for_domain_index(column: ColumnSpec, index: int) -> int | str | float:
    if column.skew is not None and column.skew.hot_values and index < len(column.skew.hot_values):
        return column.skew.hot_values[index]
    if column.type_class == "string":
        return f"v{index:012d}"
    if column.type_class == "float":
        return float(index)
    return int(index)


def _random_hot_index(rng: random.Random, column: ColumnSpec) -> int:
    if column.skew is not None and column.skew.hot_values:
        return rng.randrange(0, len(column.skew.hot_values))
    hot = _hot_count(column)
    if hot <= 1:
        return 0
    return rng.randrange(0, hot)


def _random_cold_index(rng: random.Random, column: ColumnSpec) -> int:
    hot = _hot_count(column)
    cold_start = max(hot, len(column.skew.hot_values) if column.skew and column.skew.hot_values else hot)
    if cold_start >= column.ndv:
        return rng.randrange(0, column.ndv)
    return rng.randrange(cold_start, column.ndv)


def _generate_column(
    plan: Plan,
    batch: BatchSpec,
    table: TableSpec,
    column: ColumnSpec,
    progress: float,
) -> np.ndarray:
    rng = random.Random(_seed_for(plan.seed, batch.batch_id, table.name, column.name))
    row_count = batch.row_map[table.name]
    values: list[int | str | float] = []
    skew_ratio = column.skew.ratio_for_progress(progress) if column.skew else 0.0
    for _ in range(row_count):
        if column.skew is not None and rng.random() < skew_ratio:
            domain_index = _random_hot_index(rng, column)
        else:
            domain_index = _random_cold_index(rng, column)
        values.append(_value_for_domain_index(column, domain_index))
    if column.type_class == "string":
        return np.asarray(values, dtype=str)
    if column.type_class == "float":
        return np.asarray(values, dtype=np.float64)
    return np.asarray(values, dtype=np.int64)


def _table_batch(
    plan: Plan,
    batch: BatchSpec,
    table: TableSpec,
    progress: float,
) -> TableBatch:
    columns = {
        column.name: _generate_column(plan, batch, table, column, progress)
        for column in table.columns
    }
    rows = batch.row_map[table.name]
    column_digests = {
        name: _array_digest(columns[name], columns[name].dtype.kind)
        for name in sorted(columns)
    }
    table_digest = digest_value(
        {
            "batch_id": batch.batch_id,
            "table": table.name,
            "rows": rows,
            "columns": column_digests,
        }
    )
    return TableBatch(name=table.name, batch_id=batch.batch_id, rows=rows, columns=columns, digest=table_digest)


def generate_batch(scenario: Scenario, batch: BatchSpec) -> dict[str, TableBatch]:
    """Generate one complete logical batch.

    A logical batch contains one chunk for every table. Generated rows are a
    pure function of the plan seed, batch identity, table/column names and the
    batch progress. No state outside the scenario is consulted.
    """

    if len(scenario.plan.batches) > 1:
        progress = batch.arrival_seq / (len(scenario.plan.batches) - 1)
    else:
        progress = 0.0
    return {
        table.name: _table_batch(scenario.plan, batch, table, progress)
        for table in scenario.tables
    }


def input_digest(batch: BatchSpec, table_batches: Mapping[str, TableBatch]) -> str:
    return digest_value(
        {
            "batch_id": batch.batch_id,
            "arrival_seq": batch.arrival_seq,
            "tables": {
                name: {
                    "rows": table_batch.rows,
                    "digest": table_batch.digest,
                }
                for name, table_batch in sorted(table_batches.items())
            },
        }
    )


def _canonical_rows(columns: Mapping[str, np.ndarray], row_count: int) -> list[tuple[Any, ...]]:
    if row_count == 0:
        return []
    names = sorted(columns)
    if not names:
        return [() for _ in range(row_count)]
    arrays = [columns[name].tolist() for name in names]
    return sorted(zip(*arrays))


def result_digest(columns: Mapping[str, np.ndarray], row_count: int) -> str:
    """Digest a result as a row multiset, independent of physical row order."""

    h = hashlib.sha256()
    for row in _canonical_rows(columns, row_count):
        h.update(digest_value(row).encode("ascii"))
    return h.hexdigest()

