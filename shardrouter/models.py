"""Immutable domain models for configurations, predicates, and routes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence, Union


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str
    nullable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "nullable": self.nullable, "type": self.type}


@dataclass(frozen=True)
class ModAlgorithm:
    modulus: int
    type: str = "mod"

    def to_dict(self) -> dict[str, Any]:
        return {"modulus": self.modulus, "type": self.type}


@dataclass(frozen=True)
class RangeAlgorithm:
    boundaries: tuple[int | float, ...]
    boundary_policy: str = "lower_inclusive_upper_exclusive"
    type: str = "range"

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundaries": list(self.boundaries),
            "boundary_policy": self.boundary_policy,
            "type": self.type,
        }


@dataclass(frozen=True)
class RingNode:
    database: int
    table: int
    weight: int

    def to_dict(self) -> dict[str, Any]:
        return {"database": self.database, "table": self.table, "weight": self.weight}


@dataclass(frozen=True)
class ConsistentHashAlgorithm:
    ring_version: int
    nodes: tuple[RingNode, ...]
    type: str = "consistent_hash"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "ring_version": self.ring_version,
            "type": self.type,
        }

    def with_ring_version(self, version: int) -> "ConsistentHashAlgorithm":
        return replace(self, ring_version=version)


ShardAlgorithm = Union[ModAlgorithm, RangeAlgorithm, ConsistentHashAlgorithm]


@dataclass(frozen=True)
class TableConfig:
    name: str
    columns: tuple[ColumnDef, ...]
    shard_keys: tuple[str, ...]
    database_count: int
    tables_per_database: int
    algorithm: ShardAlgorithm
    version: int

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def total_shards(self) -> int:
        return self.database_count * self.tables_per_database

    @property
    def ring_version(self) -> int | None:
        if isinstance(self.algorithm, ConsistentHashAlgorithm):
            return self.algorithm.ring_version
        return None

    @property
    def parameter_version(self) -> str:
        return f"rule={self.version};ring={self.ring_version if self.ring_version is not None else 0}"

    def database_name(self, database_index: int) -> str:
        return f"db_{database_index:04d}"

    def physical_table_name(self, table_index: int) -> str:
        return f"{self.name}_{table_index:04d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm.to_dict(),
            "columns": [column.to_dict() for column in self.columns],
            "database_count": self.database_count,
            "name": self.name,
            "shard_keys": list(self.shard_keys),
            "tables_per_database": self.tables_per_database,
            "version": self.version,
        }

    def with_version(self, version: int) -> "TableConfig":
        return replace(self, version=version)

    def with_algorithm(self, algorithm: ShardAlgorithm) -> "TableConfig":
        return replace(self, algorithm=algorithm)


@dataclass(frozen=True)
class RouteUnit:
    name: str
    table: str | None
    predicate: str | None
    depends_on: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "depends_on": list(self.depends_on),
            "name": self.name,
        }
        if self.table is not None:
            result["table"] = self.table
        if self.predicate is not None:
            result["predicate"] = self.predicate
        return result


@dataclass(frozen=True)
class RouterConfig:
    format_version: int
    config_version: int
    metadata_version: int
    tables: tuple[TableConfig, ...]
    route_units: tuple[RouteUnit, ...]

    def table_map(self) -> dict[str, TableConfig]:
        return {table.name: table for table in self.tables}

    def unit_map(self) -> dict[str, RouteUnit]:
        return {unit.name: unit for unit in self.route_units}

    def with_updates(
        self,
        *,
        config_version: int | None = None,
        tables: Sequence[TableConfig] | None = None,
        route_units: Sequence[RouteUnit] | None = None,
    ) -> "RouterConfig":
        return replace(
            self,
            config_version=self.config_version if config_version is None else config_version,
            tables=self.tables if tables is None else tuple(tables),
            route_units=self.route_units if route_units is None else tuple(route_units),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "format_version": self.format_version,
            "metadata_version": self.metadata_version,
            "route_units": [unit.to_dict() for unit in self.route_units],
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass(frozen=True)
class Bound:
    value: int | float | str
    inclusive: bool

    def to_dict(self) -> dict[str, Any]:
        return {"inclusive": self.inclusive, "value": self.value}


@dataclass(frozen=True)
class ColumnConstraint:
    points: tuple[int | float | str | bool | None, ...] = ()
    lower: Bound | None = None
    upper: Bound | None = None

    @property
    def is_point(self) -> bool:
        return bool(self.points) and self.lower is None and self.upper is None


@dataclass(frozen=True)
class PredicateInfo:
    table_name: str
    original: str
    normalized: str
    routing_id: str
    referenced_columns: tuple[str, ...]
    constraints: Mapping[str, ColumnConstraint]
    has_complete_shard_key: bool
    missing_shard_keys: tuple[str, ...]
    column_positions: Mapping[str, int]

    def point_tuples(self, shard_keys: Sequence[str]) -> tuple[tuple[Any, ...], ...]:
        """Return the sorted Cartesian product of point constraints.

        An empty tuple means at least one shard key is not constrained to a
        finite point set.
        """

        if not self.has_complete_shard_key:
            return ()
        columns: list[tuple[Any, ...]] = []
        for key in shard_keys:
            constraint = self.constraints.get(key)
            if constraint is None or not constraint.points:
                return ()
            columns.append(tuple(constraint.points))
        if not columns:
            return ()
        product: list[tuple[Any, ...]] = [()]
        for values in columns:
            product = [prefix + (value,) for prefix in product for value in values]
        return tuple(sorted(product, key=canonical_value_key))

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_complete_shard_key": self.has_complete_shard_key,
            "missing_shard_keys": list(self.missing_shard_keys),
            "normalized": self.normalized,
            "referenced_columns": list(self.referenced_columns),
            "routing_id": self.routing_id,
        }


def canonical_value_key(value: Any) -> tuple[str, str]:
    """Type-aware, deterministic ordering and identity key for SQL literals."""

    if value is None:
        return ("0:null", "null")
    if isinstance(value, bool):
        return ("1:bool", "1" if value else "0")
    if isinstance(value, int):
        return ("2:int", f"{value:+030d}")
    if isinstance(value, float):
        return ("3:float", repr(value))
    if isinstance(value, str):
        return ("4:str", value)
    return (f"9:{type(value).__name__}", repr(value))
