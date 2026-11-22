from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_TYPES = {"int64", "float64", "string"}
SUPPORTED_ALGORITHMS = {"hash", "sort_merge", "nested_loop"}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    width_bytes: int
    ndv: int
    join_key: bool = False


@dataclass(frozen=True)
class SkewSpec:
    enabled: bool = False
    column: str | None = None
    hot_key: int | str | None = None
    hot_fraction: float = 0.0


@dataclass(frozen=True)
class TableSpec:
    name: str
    row_count: int
    columns: tuple[ColumnSpec, ...]
    skew: SkewSpec = field(default_factory=SkewSpec)

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        return {column.name: column for column in self.columns}


@dataclass(frozen=True)
class JoinPredicate:
    left_table: str
    right_table: str
    left_column: str
    right_column: str

    @property
    def edge_key(self) -> tuple[str, str, str, str]:
        return (self.left_table, self.left_column, self.right_table, self.right_column)


@dataclass(frozen=True)
class Shape:
    join_order: tuple[str, ...]
    algorithms: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"join_order": list(self.join_order), "algorithms": list(self.algorithms)}

    def canonical(self) -> str:
        return "|".join(self.join_order) + "::" + "|".join(self.algorithms)


@dataclass(frozen=True)
class BatchSpec:
    arrival: int
    table: str
    row_count: int


@dataclass(frozen=True)
class PlanSpec:
    query_id: str
    initial_shape: Shape
    joins: tuple[JoinPredicate, ...]
    batches: tuple[BatchSpec, ...]


@dataclass(frozen=True)
class ColumnStats:
    ndv: int
    selectivity: float
    skew_fraction: float | None = None


@dataclass(frozen=True)
class TableStats:
    row_count: int
    columns: dict[str, ColumnStats]


@dataclass(frozen=True)
class JoinSelectivity:
    predicate: JoinPredicate
    selectivity: float


@dataclass(frozen=True)
class StatsSnapshot:
    tables: dict[str, TableStats]
    join_selectivities: dict[tuple[str, str, str, str], float]


@dataclass(frozen=True)
class ReoptConfig:
    drift_threshold: float
    cooldown_ms: int
    clock_step_ms: int
    switch_cost_budget: float
    float_tolerance: float
    error_bound_budget: float
    algorithm_candidates: tuple[str, ...] = ("hash", "sort_merge", "nested_loop")


@dataclass(frozen=True)
class SampleSpec:
    sample_id: str
    global_seed: int
    tables: tuple[TableSpec, ...]
    plan: PlanSpec
    statistics: StatsSnapshot
    config: ReoptConfig
    source_path: str = ""

    @property
    def table_map(self) -> dict[str, TableSpec]:
        return {table.name: table for table in self.tables}

    def plan_hash(self) -> str:
        import hashlib
        import json

        payload = {
            "sample_id": self.sample_id,
            "global_seed": self.global_seed,
            "tables": [
                {
                    "name": table.name,
                    "row_count": table.row_count,
                    "columns": [
                        {
                            "name": column.name,
                            "type": column.type,
                            "width_bytes": column.width_bytes,
                            "ndv": column.ndv,
                            "join_key": column.join_key,
                        }
                        for column in table.columns
                    ],
                    "skew": {
                        "enabled": table.skew.enabled,
                        "column": table.skew.column,
                        "hot_key": table.skew.hot_key,
                        "hot_fraction": table.skew.hot_fraction,
                    },
                }
                for table in self.tables
            ],
            "plan": {
                "query_id": self.plan.query_id,
                "initial_shape": self.plan.initial_shape.to_dict(),
                "joins": [predicate.__dict__ for predicate in self.plan.joins],
                "batches": [batch.__dict__ for batch in self.plan.batches],
            },
            "statistics": {
                "tables": {
                    name: {
                        "row_count": table.row_count,
                        "columns": {
                            column: {
                                "ndv": stats.ndv,
                                "selectivity": stats.selectivity,
                                "skew_fraction": stats.skew_fraction,
                            }
                            for column, stats in table.columns.items()
                        },
                    }
                    for name, table in self.statistics.tables.items()
                },
                "join_selectivities": [
                    {"predicate": list(edge), "selectivity": selectivity}
                    for edge, selectivity in self.statistics.join_selectivities.items()
                ],
            },
            "config": {
                "drift_threshold": self.config.drift_threshold,
                "cooldown_ms": self.config.cooldown_ms,
                "clock_step_ms": self.config.clock_step_ms,
                "switch_cost_budget": self.config.switch_cost_budget,
                "float_tolerance": self.config.float_tolerance,
                "error_bound_budget": self.config.error_bound_budget,
                "algorithm_candidates": list(self.config.algorithm_candidates),
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
