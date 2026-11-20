from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AggregateSpec:
    name: str
    op: str
    column: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "op": self.op, "column": self.column}


@dataclass(frozen=True)
class WindowSpec:
    length: int
    step: int

    def to_dict(self) -> dict[str, Any]:
        return {"length": self.length, "step": self.step}


@dataclass(frozen=True)
class FilterSpec:
    column: str
    op: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"column": self.column, "op": self.op, "value": self.value}


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    group_key: str
    aggregates: tuple[AggregateSpec, ...]
    window: WindowSpec | None = None
    filter: FilterSpec | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query_id": self.query_id,
            "group_key": self.group_key,
            "aggregates": [agg.to_dict() for agg in self.aggregates],
        }
        if self.window is not None:
            payload["window"] = self.window.to_dict()
        if self.filter is not None:
            payload["filter"] = self.filter.to_dict()
        return payload


@dataclass(frozen=True)
class EngineConfig:
    time_budget_ms: float
    max_batch_rows: int
    hot_key_min_rows: int
    skew_ratio: float
    memory_limit_mib: float
    fixed_scale: int | float
    float_tolerance: float
    logical_clock_step_ms: float
    exact_cost_ms_per_1k_rows: float
    accelerated_cost_ms_per_1k_rows: float
    switch_overhead_ms: float
    digest_algorithm: str = "sha256"
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def memory_limit_bytes(self) -> int:
        return max(1, int(self.memory_limit_mib * 1024 * 1024))

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_budget_ms": self.time_budget_ms,
            "max_batch_rows": self.max_batch_rows,
            "hot_key_min_rows": self.hot_key_min_rows,
            "skew_ratio": self.skew_ratio,
            "memory_limit_mib": self.memory_limit_mib,
            "memory_limit_bytes": self.memory_limit_bytes,
            "fixed_scale": self.fixed_scale,
            "float_tolerance": self.float_tolerance,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "exact_cost_ms_per_1k_rows": self.exact_cost_ms_per_1k_rows,
            "accelerated_cost_ms_per_1k_rows": self.accelerated_cost_ms_per_1k_rows,
            "switch_overhead_ms": self.switch_overhead_ms,
            "digest_algorithm": self.digest_algorithm,
        }


@dataclass(frozen=True)
class ColumnBatch:
    ordinal: int
    batch_index: int
    row_count: int
    columns: dict[str, list[Any]]
    digest: str
    source_path: str
    actual_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "batch_index": self.batch_index,
            "row_count": self.row_count,
            "columns": self.columns,
            "digest": self.digest,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class Sample:
    root: Path
    config: EngineConfig
    queries: tuple[QuerySpec, ...]
    batches: tuple[ColumnBatch, ...]
    fingerprint: str
    config_path: Path
    query_paths: tuple[Path, ...]
    batch_paths: tuple[Path, ...]

    def query(self, query_id: str) -> QuerySpec:
        for query in self.queries:
            if query.query_id == query_id:
                return query
        raise KeyError(query_id)

