from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


SUPPORTED_NODE_TYPES = {"scan", "filter", "project", "aggregate", "window"}
STATEFUL_NODE_TYPES = {"aggregate", "window"}
AGGREGATE_OPS = {"count", "sum", "sumsq", "avg", "min", "max", "fixed_sum"}


@dataclass
class Batch:
    batch_id: int
    columns: dict[str, np.ndarray]
    rows: int


@dataclass
class Dataset:
    path: Path
    source_digest: str
    batches: list[Batch]
    columns: dict[str, np.ndarray]
    rows: int
    column_types: dict[str, str]

    def slice(self, start: int, end: int) -> "Relation":
        return Relation(
            columns={name: values[start:end].copy() for name, values in self.columns.items()},
            row_count=end - start,
            global_start=start,
        )


@dataclass
class Relation:
    columns: dict[str, np.ndarray]
    row_count: int
    global_start: int = 0
    error_bounds: dict[str, np.ndarray] = field(default_factory=dict)
    error_sources: dict[str, list[str]] = field(default_factory=dict)
    source_shards: list[int] = field(default_factory=list)

    def copy(self) -> "Relation":
        return Relation(
            columns={name: array.copy() for name, array in self.columns.items()},
            row_count=self.row_count,
            global_start=self.global_start,
            error_bounds={name: array.copy() for name, array in self.error_bounds.items()},
            error_sources={name: list(values) for name, values in self.error_sources.items()},
            source_shards=list(self.source_shards),
        )


@dataclass
class ExecutionConfig:
    vector_width: int
    max_parallel_tasks: int
    scan_memory_budget_mib: float
    global_memory_budget_mib: float
    round_time_budget_ms: float
    logical_clock_step_ms: float
    fixed_scale: int
    float_tolerance: float
    operator_buffer_budget_mib: float
    verify_rewrite: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    root: str
    nodes: dict[str, dict[str, Any]]
    chain: list[str]
    schemas: dict[str, dict[str, str]]
    raw: dict[str, Any]

    def node(self, node_id: str) -> dict[str, Any]:
        return self.nodes[node_id]


@dataclass
class RewriteReport:
    before_columns: list[str]
    after_columns: list[str]
    removed_columns: list[str]
    pushed_predicates: list[dict[str, Any]]
    rewritten_plan: dict[str, Any]
    final_output_columns_before: list[str] = field(default_factory=list)
    final_output_columns_after: list[str] = field(default_factory=list)
    original_root: str = ""
    rewritten_root: str = ""
    removed_filter_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_columns": self.before_columns,
            "after_columns": self.after_columns,
            "removed_columns": self.removed_columns,
            "pushed_predicates": self.pushed_predicates,
            "final_output_columns_before": self.final_output_columns_before,
            "final_output_columns_after": self.final_output_columns_after,
            "original_root": self.original_root,
            "rewritten_root": self.rewritten_root,
            "removed_filter_nodes": self.removed_filter_nodes,
        }

