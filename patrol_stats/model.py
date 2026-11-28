from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    region_id: int
    band_id: int
    start_ts: str
    end_ts: str
    deviation_threshold: float
    workload_class: str


@dataclass(frozen=True)
class JoinSpec:
    node_id: str
    table_name: str
    fact_column: str
    right_column: str
    label: str
    right_filter: str | None = None


JOIN_SPECS: tuple[JoinSpec, ...] = (
    JoinSpec("J01", "regions", "region_id", "region_id", "区域维表"),
    JoinSpec("J02", "bands", "band_id", "band_id", "频段维表"),
    JoinSpec("J03", "base_stations", "station_id", "station_id", "基站维表"),
    JoinSpec(
        "J04",
        "terminals",
        "terminal_id",
        "terminal_id",
        "终端维表",
        "active = 1",
    ),
    JoinSpec("J05", "routes", "route_id", "route_id", "巡护路线维表"),
    JoinSpec("J06", "rangers", "ranger_id", "ranger_id", "巡护员维表"),
)

JOIN_BY_TABLE = {join.table_name: join for join in JOIN_SPECS}
CANONICAL_JOIN_ORDER = tuple(join.table_name for join in JOIN_SPECS)


@dataclass(frozen=True)
class PredicateEstimate:
    predicate: str
    column: str
    operator: str
    literal: Any
    histogram_selectivity: float
    sample_selectivity: float
    final_selectivity: float
    sample_size: int
    total_rows: int
    sample_weight: float
    hit_buckets: list[dict[str, Any]] = field(default_factory=list)
    formula: str = ""


@dataclass(frozen=True)
class SummaryIssue:
    table_name: str
    column_name: str | None
    reason: str
    expected: Any
    actual: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "column_name": self.column_name,
            "reason": self.reason,
            "expected": self.expected,
            "actual": self.actual,
        }
