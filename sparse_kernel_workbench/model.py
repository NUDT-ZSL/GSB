"""Shared immutable and report-facing data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RawEntry:
    row: int
    column: int
    value: Decimal
    line: int
    element: str


@dataclass(frozen=True)
class RawMatrix:
    rows: int
    columns: int
    entries: tuple[RawEntry, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalEntry:
    row: int
    column: int
    value: Decimal
    source_element: str
    source_line: int
    source_elements: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[int, int]:
        return self.row, self.column


@dataclass
class VectorData:
    values: tuple[float, ...]
    raw_ids: tuple[int, ...]
    index_base: int
    source_path: Path
    ids_field: str
    values_field: str
    corrections: list[dict[str, Any]]


@dataclass
class NormalizedMatrix:
    version: str
    format: str
    rows: int
    columns: int
    entries: tuple[CanonicalEntry, ...]
    vector: VectorData
    index_base: int
    weight_factor: Decimal
    weight_unit: str
    weight_field: str
    highest_row_index: int
    highest_column_index: int
    raw_rows: int
    raw_columns: int
    raw_nonzero_count: int
    duplicate_count: int
    source_path: Path
    spec_path: Path
    fields: dict[str, str]
    corrections: list[dict[str, Any]]
    dimension_rule: str = "highest_declared_index - index_base + 1"

    @property
    def nonzero_count(self) -> int:
        return len(self.entries)

    def row_counts(self) -> list[int]:
        counts = [0] * self.rows
        for entry in self.entries:
            counts[entry.row] += 1
        return counts

    def column_counts(self) -> list[int]:
        counts = [0] * self.columns
        for entry in self.entries:
            counts[entry.column] += 1
        return counts


def decimal_text(value: Decimal) -> str:
    """Return a canonical, non-scientific representation of a Decimal."""

    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")
