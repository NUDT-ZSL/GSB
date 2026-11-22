from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    primary_key: bool = False
    not_null: bool = False
    generated_expression: str | None = None
    generated_stored: bool = True

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.sql_type,
            "primary_key": self.primary_key,
            "not_null": self.not_null,
        }
        if self.generated_expression is not None:
            result["generated"] = {
                "expression": self.generated_expression,
                "stored": self.generated_stored,
            }
        return result


@dataclass(frozen=True)
class LogicalTable:
    name: str
    shard_key: str
    rule_type: str
    shard_count: int
    generated_columns_enabled: bool
    columns: tuple[Column, ...]
    key_min: int | None = None
    key_max: int | None = None
    range_boundaries: tuple[int, ...] | None = None

    @property
    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}

    @property
    def non_generated_columns(self) -> tuple[Column, ...]:
        return tuple(column for column in self.columns if column.generated_expression is None)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "logical_table": self.name,
            "shard_key": self.shard_key,
            "rule_type": self.rule_type,
            "shard_count": self.shard_count,
            "generated_columns_enabled": self.generated_columns_enabled,
            "columns": [column.to_dict() for column in self.columns],
        }
        if self.key_min is not None:
            result["shard_key_min"] = self.key_min
        if self.key_max is not None:
            result["shard_key_max"] = self.key_max
        if self.range_boundaries is not None:
            result["range_boundaries"] = list(self.range_boundaries)
        return result


@dataclass(frozen=True)
class PhysicalMapping:
    logical_table: str
    shard_no: int
    database: Path
    database_ref: str
    physical_table: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_no": self.shard_no,
            "database": self.database_ref,
            "database_absolute": str(self.database),
            "physical_table": self.physical_table,
            "role": self.role,
        }


@dataclass(frozen=True)
class Template:
    template_id: str
    logical_table: str
    kind: str
    logical_sql: str
    parameters: tuple[str, ...]
    shard_key_params: tuple[str, ...] = ()
    shard_key_param: str | None = None
    order_by: tuple[str, ...] = ()
    aggregate_count: bool = True
    aggregate_sum: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.template_id,
            "logical_table": self.logical_table,
            "kind": self.kind,
            "logical_sql": self.logical_sql,
            "parameters": list(self.parameters),
            "shard_key_param": self.shard_key_param,
            "shard_key_params": list(self.shard_key_params),
            "order_by": list(self.order_by),
            "aggregate": {
                "count": self.aggregate_count,
                "sum": self.aggregate_sum,
            },
            "description": self.description,
        }


@dataclass(frozen=True)
class RoutingRule:
    rule_type: str
    shard_count: int
    boundaries: tuple[int, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.rule_type,
            "shard_count": self.shard_count,
        }
        if self.boundaries is not None:
            result["boundaries"] = list(self.boundaries)
        return result


@dataclass
class RuleCandidate:
    version: str
    source: str
    source_priority: int
    effective_clock_ms: int
    generation: int
    digest: str
    rollback_target: str | None
    routing: dict[str, RoutingRule]
    source_file: str
    conversion: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[int, int, int, str]:
        return (
            self.effective_clock_ms,
            self.generation,
            self.source_priority,
            self.version,
        )


@dataclass
class MergedRule:
    version: str
    effective_clock_ms: int
    generation: int
    digest: str
    source: str
    source_priority: int
    rollback_target: str | None
    routing: dict[str, RoutingRule]
    decision: dict[str, Any]

    def sort_key(self) -> tuple[int, int, int, str]:
        return (
            self.effective_clock_ms,
            self.generation,
            self.source_priority,
            self.version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "effective_clock_ms": self.effective_clock_ms,
            "generation": self.generation,
            "digest": self.digest,
            "source": self.source,
            "source_priority": self.source_priority,
            "rollback_target": self.rollback_target,
            "routing": {
                table: routing.to_dict()
                for table, routing in sorted(self.routing.items())
            },
            "decision": self.decision,
        }


@dataclass
class LoadedSample:
    data_dir: Path
    tables: dict[str, LogicalTable]
    mappings: dict[str, tuple[PhysicalMapping, ...]]
    templates: dict[str, Template]
    merged_rules: list[MergedRule]
    v1_versions: list[MergedRule]
    v2_versions: list[MergedRule]
    injections: list[dict[str, Any]]
    initial_clock_ms: int
    seed_rows: dict[str, list[dict[str, Any]]]
