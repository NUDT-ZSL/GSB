"""Typed unified view specifications and deterministic index helpers."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import fail
from .expressions import FilterExpression
from .util import digest_json, require_dict, require_int, require_list, require_str


SUPPORTED_AGGREGATES = ("count", "sum", "sum_sq")


@dataclass(frozen=True)
class AggregateRule:
    function: str
    column: str

    def as_dict(self) -> dict[str, str]:
        return {"column": self.column, "function": self.function}

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        code: str = "E_SCHEMA_INVALID",
    ) -> "AggregateRule":
        function = require_str(value.get("function"), "aggregate.function", code=code)
        column = require_str(value.get("column"), "aggregate.column", code=code)
        if function not in SUPPORTED_AGGREGATES:
            raise fail(
                "E_V2_BAD_AGGREGATE",
                "unsupported aggregate function",
                function=function,
            )
        return cls(function=function, column=column)


@dataclass(frozen=True)
class ViewSpec:
    name: str
    base_tables: tuple[str, ...]
    filter_text: str
    aggregate_rules: tuple[AggregateRule, ...]
    upstream_views: tuple[str, ...]
    generation: int
    logical_clock_ms: int
    absolute_refresh_ms: int
    refresh_origin: dict[str, Any]
    status: str
    source_format: str
    source_ref: str
    migration: dict[str, Any] | None = None
    tombstone_reason: str | None = None
    tombstoned_at_ms: int | None = None

    def __post_init__(self) -> None:
        FilterExpression(self.filter_text, error_code="E_V2_BAD_FILTER")
        if self.status not in {"active", "tombstoned"}:
            raise fail("E_SCHEMA_INVALID", "invalid view status", status=self.status)
        if len(set(self.base_tables)) != len(self.base_tables):
            raise fail("E_SCHEMA_INVALID", "duplicate base table", view=self.name)
        if len(set(self.upstream_views)) != len(self.upstream_views):
            raise fail("E_SCHEMA_INVALID", "duplicate upstream dependency", view=self.name)
        if self.status == "active":
            if not self.base_tables:
                raise fail(
                    "E_SCHEMA_INVALID",
                    "active view must declare at least one base table",
                    view=self.name,
                )
            functions = [rule.function for rule in self.aggregate_rules]
            if sorted(functions) != sorted(SUPPORTED_AGGREGATES):
                raise fail(
                    "E_SCHEMA_INVALID",
                    "active view aggregate rules must contain count, sum, and sum_sq exactly once",
                    view=self.name,
                    functions=functions,
                )
        if self.status == "tombstoned":
            if not self.tombstone_reason or self.tombstoned_at_ms is None:
                raise fail(
                    "E_TOMBSTONE_INCONSISTENT",
                    "tombstoned view requires a reason and timestamp",
                    view=self.name,
                )
        elif self.tombstone_reason is not None or self.tombstoned_at_ms is not None:
            raise fail(
                "E_TOMBSTONE_INCONSISTENT",
                "active view cannot carry tombstone fields",
                view=self.name,
            )

    @property
    def state_digest(self) -> str:
        return digest_json(
            {
                "aggregate_rules": [
                    rule.as_dict()
                    for rule in sorted(self.aggregate_rules, key=lambda item: item.function)
                ],
                "base_tables": sorted(self.base_tables),
                "filter": self.filter_text,
                "upstream_views": sorted(self.upstream_views),
                "view_name": self.name,
            }
        )

    @property
    def spec_digest(self) -> str:
        return digest_json(self.as_index_dict(include_digest=False))

    def as_index_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "absolute_refresh_ms": self.absolute_refresh_ms,
            "aggregate_rules": [rule.as_dict() for rule in self.aggregate_rules],
            "base_tables": list(self.base_tables),
            "filter": self.filter_text,
            "generation": self.generation,
            "logical_clock_ms": self.logical_clock_ms,
            "migration": self.migration,
            "name": self.name,
            "refresh_origin": self.refresh_origin,
            "source_format": self.source_format,
            "source_ref": self.source_ref,
            "status": self.status,
            "tombstone_reason": self.tombstone_reason,
            "tombstoned_at_ms": self.tombstoned_at_ms,
            "upstream_views": list(self.upstream_views),
        }
        if include_digest:
            value["spec_digest"] = self.spec_digest
        return value

    @classmethod
    def from_index_dict(cls, value: dict[str, Any]) -> "ViewSpec":
        rules = tuple(
            AggregateRule.from_dict(require_dict(rule, "aggregate_rules[]"))
            for rule in require_list(value.get("aggregate_rules"), "aggregate_rules")
        )
        return cls(
            name=require_str(value.get("name"), "name"),
            base_tables=tuple(require_str(item, "base_tables[]") for item in require_list(value.get("base_tables"), "base_tables")),
            filter_text=require_str(value.get("filter"), "filter", allow_empty=True),
            aggregate_rules=rules,
            upstream_views=tuple(
                require_str(item, "upstream_views[]")
                for item in require_list(value.get("upstream_views"), "upstream_views")
            ),
            generation=require_int(value.get("generation"), "generation", minimum=0),
            logical_clock_ms=require_int(value.get("logical_clock_ms"), "logical_clock_ms", minimum=0),
            absolute_refresh_ms=require_int(value.get("absolute_refresh_ms"), "absolute_refresh_ms", minimum=0),
            refresh_origin=require_dict(value.get("refresh_origin"), "refresh_origin"),
            status=require_str(value.get("status"), "status"),
            source_format=require_str(value.get("source_format"), "source_format"),
            source_ref=require_str(value.get("source_ref"), "source_ref"),
            migration=value.get("migration"),
            tombstone_reason=value.get("tombstone_reason"),
            tombstoned_at_ms=value.get("tombstoned_at_ms"),
        )


@dataclass
class UnifiedIndex:
    payload: dict[str, Any]

    @property
    def index_digest(self) -> str:
        return self.payload["index_digest"]

    @property
    def definition_digest(self) -> str:
        return self.payload["definition_digest"]

    @property
    def base_schema(self) -> dict[str, dict[str, Any]]:
        return self.payload["base_schema"]

    @property
    def migrations(self) -> list[dict[str, Any]]:
        return self.payload["migrations"]

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return self.payload["decisions"]

    @property
    def views(self) -> dict[str, ViewSpec]:
        return {
            name: ViewSpec.from_index_dict(value)
            for name, value in self.payload["views"].items()
        }

    def active_views(self) -> dict[str, ViewSpec]:
        return {
            name: spec
            for name, spec in self.views.items()
            if spec.status == "active"
        }

    def topo_order(self) -> list[str]:
        """Return a stable dependency-first order for active views."""

        views = self.active_views()
        indegree = {name: 0 for name in views}
        downstream: dict[str, list[str]] = {name: [] for name in views}
        for name, spec in views.items():
            for upstream in spec.upstream_views:
                if upstream not in views:
                    raise fail(
                        "E_DEPENDENCY_UNDEFINED",
                        "active view depends on a missing or tombstoned view",
                        view=name,
                        upstream=upstream,
                    )
                indegree[name] += 1
                downstream[upstream].append(name)
        ready = [name for name, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            name = heapq.heappop(ready)
            order.append(name)
            for child in sorted(downstream[name]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        if len(order) != len(views):
            raise fail("E_DEPENDENCY_CYCLE", "active view dependency graph contains a cycle")
        return order

    def downstream_closure(self, names: Iterable[str]) -> list[str]:
        active = self.active_views()
        reverse: dict[str, list[str]] = {name: [] for name in active}
        for name, spec in active.items():
            for upstream in spec.upstream_views:
                reverse.setdefault(upstream, []).append(name)
        seen = set(names)
        stack = list(names)
        while stack:
            item = stack.pop()
            for child in reverse.get(item, []):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return [name for name in self.topo_order() if name in seen]

    def active_specs_payload(self) -> dict[str, Any]:
        return {
            name: spec.as_index_dict()
            for name, spec in sorted(self.active_views().items())
        }


def build_index_payload(
    *,
    views: dict[str, ViewSpec],
    base_schema: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    migrations: list[dict[str, Any]],
    source_digests: dict[str, str],
    logical_clock_ms: int,
) -> UnifiedIndex:
    view_payload = {
        name: views[name].as_index_dict()
        for name in sorted(views)
    }
    sorted_decisions = sorted(decisions, key=lambda item: item["view_name"])
    sorted_migrations = sorted(migrations, key=lambda item: item["view_name"])
    base = {
        "base_schema": {name: base_schema[name] for name in sorted(base_schema)},
        "decisions": sorted_decisions,
        "logical_clock_ms": logical_clock_ms,
        "migrations": sorted_migrations,
        "source_digests": {name: source_digests[name] for name in sorted(source_digests)},
        "views": view_payload,
    }
    definition_digest = digest_json(base)
    payload = dict(base)
    payload["definition_digest"] = definition_digest
    payload["index_digest"] = digest_json(payload)
    index = UnifiedIndex(payload=payload)
    index.topo_order()
    return index

