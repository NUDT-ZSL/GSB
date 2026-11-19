"""Validated rule snapshots, shard arithmetic, and query templates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import BizError, canonical_json, digest


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RULE_TYPES = {"mod", "range"}
ROUTE_KINDS = {"eq", "range", "in", "all"}
OPERATIONS = {"select", "sum_count"}
INJECTION_STAGES = {"route", "rewrite", "execute", "merge"}


def require_identifier(value: Any, field: str, code: str = "E_BAD_RULE") -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise BizError(code, f"{field} must be a SQL identifier", details={"field": field})
    return value


def require_int(value: Any, field: str, code: str = "E_BAD_RULE", minimum: int | None = None) -> int:
    if type(value) is not int:
        raise BizError(code, f"{field} must be an integer", details={"field": field})
    if minimum is not None and value < minimum:
        raise BizError(
            code,
            f"{field} must be >= {minimum}",
            details={"field": field, "minimum": minimum, "value": value},
        )
    return value


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type_name: str
    generated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"generated": self.generated, "name": self.name, "type": self.type_name}


@dataclass(frozen=True)
class LogicalTable:
    name: str
    shard_key: str
    rule_type: str
    shard_count: int
    boundaries: tuple[int, ...] | None
    generates_columns: bool
    columns: tuple[ColumnDef, ...]

    @property
    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}

    @classmethod
    def from_dict(cls, raw: Any) -> "LogicalTable":
        if not isinstance(raw, dict):
            raise BizError("E_BAD_RULE", "logical table definition must be an object")
        name = require_identifier(raw.get("name"), "logical_table.name")
        shard_key = require_identifier(raw.get("shard_key"), f"{name}.shard_key")
        rule_type = raw.get("rule_type")
        if rule_type not in RULE_TYPES:
            raise BizError(
                "E_BAD_RULE",
                "rule_type must be 'mod' or 'range'",
                details={"logical_table": name, "rule_type": rule_type},
            )
        shard_count = require_int(raw.get("shard_count"), f"{name}.shard_count", minimum=1)
        raw_columns = raw.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise BizError("E_BAD_RULE", "columns must be a non-empty array", details={"logical_table": name})
        columns: list[ColumnDef] = []
        seen_columns: set[str] = set()
        for index, item in enumerate(raw_columns):
            if not isinstance(item, dict):
                raise BizError(
                    "E_BAD_RULE",
                    "column definitions must be objects",
                    details={"logical_table": name, "index": index},
                )
            column_name = require_identifier(item.get("name"), f"{name}.columns[{index}].name")
            if column_name in seen_columns:
                raise BizError(
                    "E_BAD_RULE",
                    "duplicate column",
                    details={"column": column_name, "logical_table": name},
                )
            seen_columns.add(column_name)
            type_name = item.get("type", "TEXT")
            if not isinstance(type_name, str) or not type_name:
                raise BizError(
                    "E_BAD_RULE",
                    "column type must be a non-empty string",
                    details={"column": column_name, "logical_table": name},
                )
            generated = item.get("generated", False)
            if type(generated) is not bool:
                raise BizError(
                    "E_BAD_RULE",
                    "column generated flag must be boolean",
                    details={"column": column_name, "logical_table": name},
                )
            columns.append(ColumnDef(column_name, type_name, generated))
        if shard_key not in seen_columns:
            raise BizError(
                "E_BAD_RULE",
                "shard key column is not present in table structure",
                details={"logical_table": name, "shard_key": shard_key},
            )
        generates_columns = raw.get("generates_columns", False)
        if type(generates_columns) is not bool:
            raise BizError(
                "E_BAD_RULE",
                "generates_columns must be boolean",
                details={"logical_table": name},
            )
        generated_columns = [column.name for column in columns if column.generated]
        if generates_columns and not generated_columns:
            raise BizError(
                "E_BAD_RULE",
                "generated columns are enabled but no generated column is declared",
                details={"logical_table": name},
            )
        if not generates_columns and generated_columns:
            raise BizError(
                "E_BAD_RULE",
                "generated column declared while generated columns are disabled",
                details={"logical_table": name},
            )
        boundaries: tuple[int, ...] | None = None
        if rule_type == "range":
            raw_boundaries = raw.get("boundaries")
            if not isinstance(raw_boundaries, list) or len(raw_boundaries) != shard_count + 1:
                raise BizError(
                    "E_BAD_RULE",
                    "range boundaries must contain shard_count + 1 entries",
                    details={"logical_table": name, "shard_count": shard_count},
                )
            parsed = tuple(
                require_int(item, f"{name}.boundaries[{index}]", minimum=0)
                for index, item in enumerate(raw_boundaries)
            )
            if any(left >= right for left, right in zip(parsed, parsed[1:])):
                raise BizError(
                    "E_BAD_RULE",
                    "range boundaries must be strictly increasing",
                    details={"boundaries": list(parsed), "logical_table": name},
                )
            boundaries = parsed
        return cls(
            name=name,
            shard_key=shard_key,
            rule_type=rule_type,
            shard_count=shard_count,
            boundaries=boundaries,
            generates_columns=generates_columns,
            columns=tuple(columns),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "columns": [column.to_dict() for column in self.columns],
            "generates_columns": self.generates_columns,
            "name": self.name,
            "rule_type": self.rule_type,
            "shard_count": self.shard_count,
            "shard_key": self.shard_key,
        }
        if self.boundaries is not None:
            result["boundaries"] = list(self.boundaries)
        return result

    def shard_for_value(self, value: int) -> int:
        value = require_int(
            value,
            self.shard_key,
            code="E_SHARD_KEY_OUT_OF_RANGE",
            minimum=0,
        )
        if self.rule_type == "mod":
            return value % self.shard_count
        assert self.boundaries is not None
        lower = self.boundaries[0]
        upper = self.boundaries[-1]
        if value < lower or value >= upper:
            raise BizError(
                "E_SHARD_KEY_OUT_OF_RANGE",
                "shard key is outside the configured range",
                details={
                    "allowed": {"lower": lower, "upper_exclusive": upper},
                    "logical_table": self.name,
                    "shard_key": self.shard_key,
                    "value": value,
                },
            )
        for shard_id in range(self.shard_count):
            if self.boundaries[shard_id] <= value < self.boundaries[shard_id + 1]:
                return shard_id
        raise AssertionError("validated range did not resolve to a shard")


@dataclass(frozen=True)
class MappingEntry:
    database: str
    table: str

    @classmethod
    def from_dict(cls, raw: Any, logical_table: str, shard_id: int) -> "MappingEntry":
        if not isinstance(raw, dict):
            raise BizError("E_BAD_MAPPING", "mapping entry must be an object")
        database = raw.get("database")
        table = require_identifier(raw.get("table"), "physical_mappings.table", code="E_BAD_MAPPING")
        if not isinstance(database, str) or not database or database.startswith("/") or ".." in database.split("/"):
            raise BizError(
                "E_BAD_MAPPING",
                "mapping database must be a sample-relative path without '..'",
                details={
                    "database": database,
                    "logical_table": logical_table,
                    "shard_id": shard_id,
                },
            )
        return cls(database=database, table=table)

    def to_dict(self) -> dict[str, str]:
        return {"database": self.database, "table": self.table}


@dataclass(frozen=True)
class ShardMapping:
    shard_id: int
    primary: MappingEntry
    standby: MappingEntry | None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"primary": self.primary.to_dict(), "shard_id": self.shard_id}
        if self.standby is not None:
            result["standby"] = self.standby.to_dict()
        return result


@dataclass(frozen=True)
class RuleSnapshot:
    logical_tables: dict[str, LogicalTable]
    mappings: dict[str, dict[int, ShardMapping]]

    @classmethod
    def from_dict(cls, raw: Any) -> "RuleSnapshot":
        if not isinstance(raw, dict):
            raise BizError("E_BAD_RULE", "rule snapshot must be an object")
        raw_tables = raw.get("logical_tables")
        raw_mappings = raw.get("physical_mappings")
        if not isinstance(raw_tables, list) or not raw_tables:
            raise BizError("E_BAD_RULE", "logical_tables must be a non-empty array")
        if not isinstance(raw_mappings, list) or not raw_mappings:
            raise BizError("E_BAD_MAPPING", "physical_mappings must be a non-empty array")
        tables: dict[str, LogicalTable] = {}
        for item in raw_tables:
            table = LogicalTable.from_dict(item)
            if table.name in tables:
                raise BizError(
                    "E_BAD_RULE",
                    "logical table names must be unique",
                    details={"logical_table": table.name},
                )
            tables[table.name] = table
        mappings: dict[str, dict[int, ShardMapping]] = {name: {} for name in tables}
        pending: dict[tuple[str, int], dict[str, MappingEntry]] = {}
        for item in raw_mappings:
            if not isinstance(item, dict):
                raise BizError("E_BAD_MAPPING", "physical mapping entry must be an object")
            logical_name = item.get("logical_table")
            if logical_name not in tables:
                raise BizError(
                    "E_BAD_MAPPING",
                    "physical mapping refers to an unknown logical table",
                    details={"logical_table": logical_name},
                )
            shard_id = require_int(
                item.get("shard_id"),
                "physical_mappings.shard_id",
                code="E_BAD_MAPPING",
                minimum=0,
            )
            table = tables[logical_name]
            if shard_id >= table.shard_count:
                raise BizError(
                    "E_BAD_MAPPING",
                    "physical mapping shard_id is outside the logical table range",
                    details={
                        "logical_table": logical_name,
                        "shard_count": table.shard_count,
                        "shard_id": shard_id,
                    },
                )
            role = item.get("role")
            if role not in {"primary", "standby"}:
                raise BizError(
                    "E_BAD_MAPPING",
                    "physical mapping role must be primary or standby",
                    details={"role": role},
                )
            entry = MappingEntry.from_dict(item, logical_name, shard_id)
            key = (logical_name, shard_id)
            slot = pending.setdefault(key, {})
            if role in slot:
                raise BizError(
                    "E_BAD_MAPPING",
                    "duplicate physical mapping role for shard",
                    details={"logical_table": logical_name, "role": role, "shard_id": shard_id},
                )
            slot[role] = entry
        for logical_name, table in tables.items():
            for shard_id in range(table.shard_count):
                slot = pending.get((logical_name, shard_id), {})
                if "primary" not in slot:
                    raise BizError(
                        "E_BAD_MAPPING",
                        "physical mappings do not cover every shard with a primary",
                        details={"logical_table": logical_name, "shard_id": shard_id},
                    )
                mappings[logical_name][shard_id] = ShardMapping(
                    shard_id=shard_id,
                    primary=slot["primary"],
                    standby=slot.get("standby"),
                )
        return cls(logical_tables=tables, mappings=mappings)

    def to_dict(self) -> dict[str, Any]:
        mapping_rows: list[dict[str, Any]] = []
        for logical_name in sorted(self.mappings):
            for shard_id in sorted(self.mappings[logical_name]):
                mapping = self.mappings[logical_name][shard_id]
                mapping_rows.append(
                    {
                        "logical_table": logical_name,
                        "role": "primary",
                        "shard_id": shard_id,
                        **mapping.primary.to_dict(),
                    }
                )
                if mapping.standby is not None:
                    mapping_rows.append(
                        {
                            "logical_table": logical_name,
                            "role": "standby",
                            "shard_id": shard_id,
                            **mapping.standby.to_dict(),
                        }
                    )
        return {
            "logical_tables": [self.logical_tables[name].to_dict() for name in sorted(self.logical_tables)],
            "physical_mappings": mapping_rows,
        }

    def content_digest(self) -> str:
        return digest(self.to_dict())

    def mapping_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for logical_name in sorted(self.mappings):
            shards: dict[str, Any] = {}
            for shard_id in sorted(self.mappings[logical_name]):
                shards[str(shard_id)] = self.mappings[logical_name][shard_id].to_dict()
            summary[logical_name] = shards
        return summary


@dataclass(frozen=True)
class QueryTemplate:
    id: str
    logical_table: str
    route_kind: str
    operation: str
    sql: str
    default_params: dict[str, Any]
    order_by: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> "QueryTemplate":
        if not isinstance(raw, dict):
            raise BizError("E_BAD_TEMPLATE", "query template must be an object")
        template_id = require_identifier(raw.get("id"), "query_template.id", code="E_BAD_TEMPLATE")
        logical_table = require_identifier(
            raw.get("logical_table"), "query_template.logical_table", code="E_BAD_TEMPLATE"
        )
        route_kind = raw.get("route_kind")
        if route_kind not in ROUTE_KINDS:
            raise BizError(
                "E_BAD_TEMPLATE",
                "route_kind must be eq, range, in, or all",
                details={"query_template": template_id},
            )
        operation = raw.get("operation")
        if operation not in OPERATIONS:
            raise BizError(
                "E_BAD_TEMPLATE",
                "operation must be select or sum_count",
                details={"query_template": template_id},
            )
        sql = raw.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise BizError("E_BAD_TEMPLATE", "sql must be a non-empty string")
        normalized_sql = sql.strip()
        if ";" in normalized_sql:
            raise BizError("E_BAD_TEMPLATE", "multi-statement SQL is not allowed")
        if not re.match(r"^(SELECT|WITH)\b", normalized_sql, flags=re.IGNORECASE):
            raise BizError("E_BAD_TEMPLATE", "query SQL must start with SELECT or WITH")
        if not re.search(rf"\b{re.escape(logical_table)}\b", normalized_sql):
            raise BizError(
                "E_BAD_TEMPLATE",
                "query SQL does not reference its logical table",
                details={"logical_table": logical_table, "query_template": template_id},
            )
        marker_count = normalized_sql.count("{{shard_predicate}}")
        has_marker = marker_count > 0
        if marker_count > 1:
            raise BizError(
                "E_BAD_TEMPLATE",
                "query SQL may contain only one {{shard_predicate}} marker",
                details={"query_template": template_id},
            )
        if route_kind == "all" and has_marker:
            raise BizError("E_BAD_TEMPLATE", "all-route templates must not contain a shard predicate marker")
        if route_kind != "all" and not has_marker:
            raise BizError("E_BAD_TEMPLATE", "routed templates must contain {{shard_predicate}}")
        raw_defaults = raw.get("default_params", {})
        if not isinstance(raw_defaults, dict):
            raise BizError("E_BAD_TEMPLATE", "default_params must be an object")
        raw_order_by = raw.get("order_by", [])
        if not isinstance(raw_order_by, list):
            raise BizError("E_BAD_TEMPLATE", "order_by must be an array")
        order_by = tuple(require_identifier(item, "order_by", code="E_BAD_TEMPLATE") for item in raw_order_by)
        return cls(
            id=template_id,
            logical_table=logical_table,
            route_kind=route_kind,
            operation=operation,
            sql=normalized_sql,
            default_params=dict(raw_defaults),
            order_by=order_by,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_params": self.default_params,
            "id": self.id,
            "logical_table": self.logical_table,
            "operation": self.operation,
            "order_by": list(self.order_by),
            "route_kind": self.route_kind,
            "sql": self.sql,
        }


def validate_query_against_snapshot(template: QueryTemplate, snapshot: RuleSnapshot) -> None:
    table = snapshot.logical_tables.get(template.logical_table)
    if table is None:
        raise BizError(
            "E_BAD_TEMPLATE",
            "query template refers to an unknown logical table",
            query_template=template.id,
            details={"logical_table": template.logical_table},
        )
    for column in template.order_by:
        if column not in table.column_names:
            raise BizError(
                "E_BAD_TEMPLATE",
                "order_by refers to an unknown column",
                query_template=template.id,
                details={"column": column, "logical_table": table.name},
            )


def stable_json(value: Any) -> str:
    return canonical_json(value)

