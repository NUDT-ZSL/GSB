"""Pure and deterministic routing plus physical SQL rewriting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import BizError, canonical_json, digest
from .rules import LogicalTable, QueryTemplate, RuleSnapshot, require_int


@dataclass(frozen=True)
class TargetShard:
    shard_id: int
    database: str
    table: str
    sql: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "shard_id": self.shard_id,
            "sql": self.sql,
            "sql_digest": digest(self.sql),
            "table": self.table,
        }


@dataclass(frozen=True)
class RewriteResult:
    query_template: str
    rule_version: str
    route_kind: str
    operation: str
    full_route: bool
    params: dict[str, Any]
    target_shards: tuple[int, ...]
    shards: tuple[TargetShard, ...]
    logical_sql: str
    rewritten_digest: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "full_route": self.full_route,
            "logical_sql": self.logical_sql,
            "operation": self.operation,
            "params": self.params,
            "query_template": self.query_template,
            "rewritten_digest": self.rewritten_digest,
            "route_kind": self.route_kind,
            "rule_version": self.rule_version,
            "shards": [shard.public_dict() for shard in self.shards],
            "target_shards": list(self.target_shards),
        }


def _copy_params(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_params(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_params(item) for item in value]
    return value


def merge_params(template: QueryTemplate, overrides: dict[str, Any] | None) -> dict[str, Any]:
    if overrides is not None and not isinstance(overrides, dict):
        raise BizError(
            "E_PARAM_INVALID",
            "query params must be an object",
            query_template=template.id,
        )
    merged = _copy_params(template.default_params)
    if overrides:
        merged.update(_copy_params(overrides))
    if template.route_kind == "eq":
        if "shard_key" not in merged:
            raise BizError(
                "E_PARAM_INVALID",
                "eq routing requires shard_key",
                query_template=template.id,
            )
        require_int(merged["shard_key"], "shard_key", code="E_PARAM_INVALID")
        allowed = {"shard_key"}
    elif template.route_kind == "range":
        for key in ("start", "end"):
            if key not in merged:
                raise BizError(
                    "E_PARAM_INVALID",
                    f"range routing requires {key}",
                    query_template=template.id,
                )
            require_int(merged[key], key, code="E_PARAM_INVALID")
        if merged["start"] > merged["end"]:
            raise BizError(
                "E_PARAM_INVALID",
                "range start must be <= end",
                query_template=template.id,
                details={"end": merged["end"], "start": merged["start"]},
            )
        allowed = {"start", "end"}
    elif template.route_kind == "in":
        values = merged.get("values")
        if not isinstance(values, list) or not values:
            raise BizError(
                "E_PARAM_INVALID",
                "in routing requires a non-empty values array",
                query_template=template.id,
            )
        for index, value in enumerate(values):
            require_int(value, f"values[{index}]", code="E_PARAM_INVALID")
        allowed = {"values"}
    else:
        allowed = set()
    unknown = sorted(set(merged) - allowed)
    if unknown:
        raise BizError(
            "E_PARAM_INVALID",
            "unknown query params",
            query_template=template.id,
            details={"unknown_params": unknown},
        )
    return merged


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _replace_logical_table(sql: str, logical_table: str, physical_table: str) -> str:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(logical_table)}(?![A-Za-z0-9_])")
    replaced, count = pattern.subn(_quote_identifier(physical_table), sql)
    if count == 0:
        raise BizError("E_REWRITE_FAILED", "logical table was not found in query SQL")
    return replaced


def _mod_range_bounds(
    start: int,
    end: int,
    shard_id: int,
    shard_count: int,
) -> tuple[int, int] | None:
    residue = shard_id % shard_count
    first = start + ((residue - start) % shard_count)
    if first > end:
        return None
    last = end - ((end - residue) % shard_count)
    return first, last


def _range_shard_intersection(
    table: LogicalTable,
    shard_id: int,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    assert table.boundaries is not None
    lower = max(start, table.boundaries[shard_id])
    upper = min(end, table.boundaries[shard_id + 1] - 1)
    if lower > upper:
        return None
    return lower, upper


def _range_predicate(column: str, lower: int, upper: int) -> str:
    if lower == upper:
        return f"{column} = {lower}"
    return f"{column} >= {lower} AND {column} <= {upper}"


def build_rewrite(
    snapshot: RuleSnapshot,
    template: QueryTemplate,
    params: dict[str, Any] | None,
    rule_version: str,
) -> RewriteResult:
    merged = merge_params(template, params)
    try:
        table = snapshot.logical_tables[template.logical_table]
    except KeyError as exc:
        raise BizError(
            "E_BAD_RULE",
            "query template logical table is absent from the bound rule snapshot",
            stage="route",
            query_template=template.id,
            rule_version=rule_version,
            details={"logical_table": template.logical_table},
        ) from exc
    column = table.shard_key
    predicates: dict[int, str] = {}
    try:
        if template.route_kind == "eq":
            value = merged["shard_key"]
            shard_id = table.shard_for_value(value)
            predicates[shard_id] = f"{column} = {value}"
        elif template.route_kind == "in":
            for value in sorted(set(merged["values"])):
                table.shard_for_value(value)
            by_shard: dict[int, list[int]] = {}
            for value in sorted(set(merged["values"])):
                by_shard.setdefault(table.shard_for_value(value), []).append(value)
            predicates = {
                shard_id: f"{column} IN ({', '.join(str(value) for value in values)})"
                for shard_id, values in by_shard.items()
            }
        elif template.route_kind == "range":
            start = merged["start"]
            end = merged["end"]
            if start < 0 or end < 0:
                raise BizError(
                    "E_SHARD_KEY_OUT_OF_RANGE",
                    "range shard keys must be non-negative",
                    details={"end": end, "shard_key": column, "start": start},
                )
            if table.rule_type == "mod":
                for shard_id in range(table.shard_count):
                    bounds = _mod_range_bounds(start, end, shard_id, table.shard_count)
                    if bounds is None:
                        continue
                    lower, upper = bounds
                    predicates[shard_id] = (
                        f"{column} >= {lower} AND {column} <= {upper} "
                        f"AND {column} % {table.shard_count} = {shard_id}"
                    )
                if not predicates:
                    raise BizError(
                        "E_SHARD_KEY_OUT_OF_RANGE",
                        "range does not intersect any modulo shard",
                        details={"end": end, "shard_key": column, "start": start},
                    )
            else:
                assert table.boundaries is not None
                domain_lower = table.boundaries[0]
                domain_upper_exclusive = table.boundaries[-1]
                if end < domain_lower or start >= domain_upper_exclusive:
                    raise BizError(
                        "E_SHARD_KEY_OUT_OF_RANGE",
                        "range does not intersect the configured shard domain",
                        details={
                            "allowed": {
                                "lower": domain_lower,
                                "upper_exclusive": domain_upper_exclusive,
                            },
                            "end": end,
                            "shard_key": column,
                            "start": start,
                        },
                    )
                clipped_start = max(start, domain_lower)
                clipped_end = min(end, domain_upper_exclusive - 1)
                for shard_id in range(table.shard_count):
                    intersection = _range_shard_intersection(
                        table, shard_id, clipped_start, clipped_end
                    )
                    if intersection is None:
                        continue
                    predicates[shard_id] = _range_predicate(column, *intersection)
        else:
            predicates = {shard_id: "" for shard_id in range(table.shard_count)}
    except BizError as exc:
        raise exc.with_context(
            stage="route",
            query_template=template.id,
            rule_version=rule_version,
            details={"logical_table": table.name, "route_kind": template.route_kind},
        )
    target_shards = tuple(sorted(predicates))
    if not target_shards:
        raise BizError(
            "E_ROUTE_EMPTY",
            "routing produced no target shards",
            stage="route",
            query_template=template.id,
            rule_version=rule_version,
        )
    shards: list[TargetShard] = []
    for shard_id in target_shards:
        mapping = snapshot.mappings[table.name][shard_id]
        sql = _replace_logical_table(template.sql, table.name, mapping.primary.table)
        if "{{shard_predicate}}" in sql:
            sql = sql.replace("{{shard_predicate}}", predicates[shard_id])
        if "{{" in sql or "}}" in sql:
            raise BizError(
                "E_REWRITE_FAILED",
                "unresolved rewrite marker remains in physical SQL",
                stage="rewrite",
                query_template=template.id,
                target_shards=list(target_shards),
                rule_version=rule_version,
            )
        shards.append(
            TargetShard(
                shard_id=shard_id,
                database=mapping.primary.database,
                table=mapping.primary.table,
                sql=sql,
            )
        )
    digest_material = {
        "full_route": template.route_kind == "all",
        "operation": template.operation,
        "params": merged,
        "query_template": template.id,
        "rule_version": rule_version,
        "shards": [
            {
                "database": shard.database,
                "shard_id": shard.shard_id,
                "sql": shard.sql,
                "table": shard.table,
            }
            for shard in shards
        ],
        "target_shards": list(target_shards),
    }
    return RewriteResult(
        query_template=template.id,
        rule_version=rule_version,
        route_kind=template.route_kind,
        operation=template.operation,
        full_route=template.route_kind == "all",
        params=merged,
        target_shards=target_shards,
        shards=tuple(shards),
        logical_sql=template.sql,
        rewritten_digest=digest(digest_material),
    )


def route_probe(
    snapshot: RuleSnapshot,
    template: QueryTemplate,
    params: dict[str, Any] | None,
    rule_version: str,
) -> dict[str, Any]:
    """Return only routing targets, useful for diagnostics and tests."""
    rewrite = build_rewrite(snapshot, template, params, rule_version)
    return {
        "full_route": rewrite.full_route,
        "params": rewrite.params,
        "query_template": rewrite.query_template,
        "route_kind": rewrite.route_kind,
        "rule_version": rewrite.rule_version,
        "target_shards": list(rewrite.target_shards),
    }


def route_response(rewrite: RewriteResult) -> dict[str, Any]:
    return rewrite.public_dict()


def canonical_route(rewrite: RewriteResult) -> str:
    return canonical_json(rewrite.public_dict())

