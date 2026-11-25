from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping


COLUMN_TYPES = frozenset(
    {
        "int",
        "bigint",
        "smallint",
        "boolean",
        "double",
        "float",
        "decimal",
        "string",
        "timestamp",
    }
)

OPERATORS = frozenset({"hash", "sort"})
AGG_FUNCTIONS = frozenset({"count", "sum", "avg", "min", "max"})


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    ndv: int
    decimal_scale: int = 2


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    global_seed: int
    skew_column: str | None = None
    skew_ratio: Fraction | None = None
    window_params: Mapping[str, int] = field(default_factory=dict)

    def column(self, name: str) -> ColumnSpec:
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(name)


@dataclass(frozen=True)
class AggregationSpec:
    op: str
    column: str | None
    alias: str


@dataclass(frozen=True)
class CostEstimate:
    input_rows: int
    output_cardinality: int
    elapsed_ms: Fraction


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    table: str
    group_by: tuple[str, ...]
    aggregations: tuple[AggregationSpec, ...]
    operator: str
    cost_estimate: CostEstimate
    size_ms: int | None = None
    slide_ms: int | None = None


@dataclass(frozen=True)
class FormSpec:
    form_id: str
    window_order: tuple[str, ...]
    operators: Mapping[str, str]
    group_orders: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    switch_cost_ms: Fraction | None = None
    setup_cost_ms: Fraction | None = None
    projected_runtime_ms: Fraction | None = None
    target_version: int | None = None

    def operator_for(self, window_id: str, default: str = "hash") -> str:
        return self.operators.get(window_id, default)


@dataclass(frozen=True)
class PlanSpec:
    plan_id: str
    table: str
    window_order: tuple[str, ...]
    windows: tuple[WindowSpec, ...]
    initial_form: FormSpec
    candidates: tuple[FormSpec, ...]

    def window(self, window_id: str) -> WindowSpec:
        for window in self.windows:
            if window.window_id == window_id:
                return window
        raise KeyError(window_id)


@dataclass(frozen=True)
class BatchSpec:
    seq: int
    table: str
    window_id: str
    arrival_ms: int
    row_count: int
    window_end: bool = False


@dataclass(frozen=True)
class ArrivalScript:
    global_seed: int
    batches: tuple[BatchSpec, ...]


@dataclass(frozen=True)
class RuntimeConfig:
    drift_threshold: Fraction
    cooldown_ms: int
    switch_cost_budget_ms: Fraction
    max_parallel_tasks: int
    logical_time_budget_per_round_ms: Fraction
    fixed_point_scale: int
    memory_budget_bytes: int = 64 * 1024 * 1024
    cost_tolerance_ms: Fraction = Fraction(1, 1000)
    min_observation_batches: int = 1
    rounding_mode: str = "floor"


@dataclass(frozen=True)
class Bundle:
    tables: tuple[TableSpec, ...]
    plan: PlanSpec
    arrivals: ArrivalScript
    config: RuntimeConfig

    def table(self, name: str) -> TableSpec:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)


def fraction_to_json(value: Fraction | None) -> Any:
    if value is None:
        return None
    if value.denominator == 1:
        return value.numerator
    return {"num": value.numerator, "den": value.denominator}


def fraction_from_json(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, Mapping):
        return Fraction(int(value["num"]), int(value["den"]))
    raise ValueError(f"invalid fraction JSON: {value!r}")
