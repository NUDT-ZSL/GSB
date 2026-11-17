from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    min_value: int | None = None
    max_value: int | None = None


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    rows: tuple[dict[str, Any], ...]
    null_bitmap: dict[str, tuple[int, ...]]
    source_path: str

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class Dataset:
    tables: tuple[Table, ...]
    source_path: str

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)


@dataclass(frozen=True)
class Predicate:
    side: str
    column: str
    op: str
    value: int | float


@dataclass(frozen=True)
class Projection:
    name: str
    side: str
    column: str


@dataclass(frozen=True)
class AggregateSpec:
    side: str
    column: str


@dataclass(frozen=True)
class Query:
    left_table: str
    right_table: str
    join_key_left: str
    join_key_right: str
    build_side: str
    predicates: dict[str, tuple[Predicate, ...]]
    projection: tuple[Projection, ...]
    aggregate: AggregateSpec
    join_type: str
    source_path: str


@dataclass(frozen=True)
class JoinKeyMode:
    domain: str
    uses_float: bool
    left_error_bound: int
    right_error_bound: int
