from __future__ import annotations

import re
from typing import Any

from .errors import fail
from .models import LogicalTable, MergedRule, PhysicalMapping, RoutingRule, Template
from .util import digest, quote_identifier


def _key_value(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        fail("E_SHARD_KEY_OUT_OF_RANGE", f"{label} must be an integer shard key")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    fail(
        "E_SHARD_KEY_OUT_OF_RANGE",
        f"{label} must be an integer shard key",
        actual_type=type(value).__name__,
        actual=value,
    )


def _validate_key(
    table: LogicalTable,
    routing: RoutingRule,
    value: int,
) -> None:
    if routing.rule_type == "range":
        assert routing.boundaries is not None
        minimum = routing.boundaries[0]
        maximum = routing.boundaries[-1]
        if table.key_min is not None:
            minimum = max(minimum, table.key_min)
        if table.key_max is not None:
            maximum = min(maximum, table.key_max)
        if value < minimum or value >= maximum:
            fail(
                "E_SHARD_KEY_OUT_OF_RANGE",
                "shard key is outside the range routing domain",
                table=table.name,
                shard_key=table.shard_key,
                value=value,
                minimum=minimum,
                maximum_exclusive=maximum,
            )


def route_key(table: LogicalTable, routing: RoutingRule, value: int) -> int:
    if routing.rule_type == "modulo":
        return value % routing.shard_count
    _validate_key(table, routing, value)
    assert routing.boundaries is not None
    boundaries = routing.boundaries
    for shard_no in range(routing.shard_count):
        if boundaries[shard_no] <= value < boundaries[shard_no + 1]:
            return shard_no
    fail(
        "E_SHARD_KEY_OUT_OF_RANGE",
        "shard key did not match any range shard",
        table=table.name,
        value=value,
    )


def _validate_template_params(template: Template, params: dict[str, Any]) -> None:
    missing = sorted(set(template.parameters) - set(params))
    if missing:
        fail(
            "E_TEMPLATE_INVALID",
            "query parameters are missing",
            template=template.template_id,
            missing=missing,
        )
    extra = sorted(set(params) - set(template.parameters))
    if extra:
        fail(
            "E_TEMPLATE_INVALID",
            "query contains undeclared parameters",
            template=template.template_id,
            extra=extra,
        )


def _range_targets(
    table: LogicalTable,
    routing: RoutingRule,
    low: int,
    high: int,
) -> list[int]:
    if low > high:
        fail(
            "E_SHARD_KEY_OUT_OF_RANGE",
            "range lower bound exceeds upper bound",
            table=table.name,
            low=low,
            high=high,
        )
    if routing.rule_type == "modulo":
        result = []
        for shard_no in range(routing.shard_count):
            first = low + ((shard_no - low) % routing.shard_count)
            if first <= high:
                result.append(shard_no)
        return result
    assert routing.boundaries is not None
    boundaries = routing.boundaries
    domain_min = boundaries[0]
    domain_max = boundaries[-1]
    if table.key_min is not None:
        domain_min = max(domain_min, table.key_min)
    if table.key_max is not None:
        domain_max = min(domain_max, table.key_max)
    if low < domain_min or low >= domain_max or high < domain_min or high > domain_max:
        fail(
            "E_SHARD_KEY_OUT_OF_RANGE",
            "range predicate is outside the range routing domain",
            table=table.name,
            low=low,
            high=high,
            minimum=domain_min,
            maximum=domain_max,
        )
    first_shard = route_key(table, routing, low)
    if high == domain_max:
        last_shard = routing.shard_count - 1
    else:
        last_shard = route_key(table, routing, high)
    return list(range(first_shard, last_shard + 1))


def target_shards(
    table: LogicalTable,
    routing: RoutingRule,
    template: Template,
    params: dict[str, Any],
) -> dict[str, Any]:
    _validate_template_params(template, params)
    if template.kind == "all":
        return {
            "shards": list(range(table.shard_count)),
            "all_route": True,
            "mode": "all",
            "reason": "template has no shard-key predicate",
        }
    if template.kind == "equal":
        assert template.shard_key_param is not None
        value = _key_value(
            params[template.shard_key_param],
            label=f"{template.template_id}.{template.shard_key_param}",
        )
        return {
            "shards": [route_key(table, routing, value)],
            "all_route": False,
            "mode": "equal",
            "reason": "single shard-key equality",
            "key_values": [value],
        }
    if template.kind == "range":
        low = _key_value(
            params[template.shard_key_params[0]],
            label=f"{template.template_id}.{template.shard_key_params[0]}",
        )
        high = _key_value(
            params[template.shard_key_params[1]],
            label=f"{template.template_id}.{template.shard_key_params[1]}",
        )
        return {
            "shards": _range_targets(table, routing, low, high),
            "all_route": False,
            "mode": "range",
            "reason": "inclusive shard-key range",
            "key_range": [low, high],
        }
    if template.kind == "in":
        values = [
            _key_value(
                params[name],
                label=f"{template.template_id}.{name}",
            )
            for name in template.shard_key_params
        ]
        unique_values = sorted(set(values))
        shards = sorted({route_key(table, routing, value) for value in unique_values})
        return {
            "shards": shards,
            "all_route": False,
            "mode": "in",
            "reason": "shard-key IN list",
            "key_values": unique_values,
        }
    fail(
        "E_TEMPLATE_INVALID",
        "unsupported template kind",
        template=template.template_id,
        kind=template.kind,
    )


def _replace_logical_table(sql: str, logical_table: str, physical_table: str) -> str:
    pattern = re.compile(rf"\b{re.escape(logical_table)}\b", flags=re.IGNORECASE)
    if not pattern.search(sql):
        fail(
            "E_TEMPLATE_INVALID",
            "logical table token is missing from the query",
            logical_table=logical_table,
        )
    return pattern.sub(quote_identifier(physical_table), sql)


def _predicate_patterns(template: Template, table: LogicalTable) -> tuple[re.Pattern[str], str]:
    column = re.escape(table.shard_key)
    if template.kind == "equal":
        assert template.shard_key_param is not None
        param = re.escape(template.shard_key_param)
        pattern = re.compile(
            rf"(?P<predicate>\b{column}\s*=\s*:{param}\b)",
            flags=re.IGNORECASE,
        )
    elif template.kind == "range":
        first, second = template.shard_key_params
        pattern = re.compile(
            rf"(?P<predicate>\b{column}\s+BETWEEN\s+:{re.escape(first)}\s+AND\s+:{re.escape(second)}\b)",
            flags=re.IGNORECASE,
        )
    elif template.kind == "in":
        pattern = re.compile(
            rf"(?P<predicate>\b{column}\s+IN\s*\((?P<values>[^()]*)\))",
            flags=re.IGNORECASE,
        )
    else:
        pattern = re.compile(r"$^")
    return pattern, "predicate"


def _in_params_from_body(body: str) -> list[str]:
    names = []
    for raw in body.split(","):
        token = raw.strip()
        match = re.fullmatch(r":([A-Za-z_][A-Za-z0-9_]*)", token)
        if not match:
            fail(
                "E_TEMPLATE_INVALID",
                "IN predicate must contain only named parameters",
                fragment=body,
            )
        names.append(match.group(1))
    return names


def _rewrite_shard_sql(
    *,
    template: Template,
    table: LogicalTable,
    routing: RoutingRule,
    shard_no: int,
    physical_table: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    sql = _replace_logical_table(
        template.logical_sql,
        table.name,
        physical_table,
    )
    physical_params: dict[str, Any] = {}
    for name in template.parameters:
        physical_params[name] = params[name]
    predicate_before = ""
    predicate_after = ""
    pruned = False
    prune_reason = "all-route template"

    if template.kind != "all":
        pattern, _ = _predicate_patterns(template, table)
        match = pattern.search(sql)
        if match is None:
            fail(
                "E_TEMPLATE_INVALID",
                "template shard-key predicate does not match the declared kind",
                template=template.template_id,
                kind=template.kind,
                table=table.name,
            )
        predicate_before = match.group("predicate")
        if template.kind == "equal":
            predicate_after = predicate_before
            prune_reason = "equality predicate retained inside its single target shard"
        elif template.kind == "range":
            low = _key_value(
                params[template.shard_key_params[0]],
                label=f"{template.template_id}.{template.shard_key_params[0]}",
            )
            high = _key_value(
                params[template.shard_key_params[1]],
                label=f"{template.template_id}.{template.shard_key_params[1]}",
            )
            if routing.rule_type == "range":
                assert routing.boundaries is not None
                shard_low = max(low, routing.boundaries[shard_no])
                shard_high = min(high, routing.boundaries[shard_no + 1] - 1)
                low_name = f"__sr_{shard_no}_low"
                high_name = f"__sr_{shard_no}_high"
                physical_params[low_name] = shard_low
                physical_params[high_name] = shard_high
                predicate_after = (
                    f"{table.shard_key} BETWEEN :{low_name} AND :{high_name}"
                )
                sql = sql[: match.start()] + predicate_after + sql[match.end() :]
                pruned = shard_low != low or shard_high != high
                prune_reason = (
                    "range predicate clipped to the shard boundary"
                    if pruned
                    else "range predicate already confined to one shard"
                )
            else:
                predicate_after = predicate_before
                prune_reason = (
                    "modulo routing retains the range predicate because residue "
                    "classes are non-contiguous"
                )
        elif template.kind == "in":
            assert match.group("values") is not None
            body_names = _in_params_from_body(match.group("values"))
            if set(body_names) != set(template.shard_key_params):
                fail(
                    "E_TEMPLATE_INVALID",
                    "IN predicate parameters do not match the template declaration",
                    template=template.template_id,
                )
            selected = [
                name
                for name in body_names
                if route_key(
                    table,
                    routing,
                    _key_value(
                        params[name],
                        label=f"{template.template_id}.{name}",
                    ),
                )
                == shard_no
            ]
            if not selected:
                fail(
                    "E_INTERNAL",
                    "IN rewrite requested a shard with no matching values",
                    shard_no=shard_no,
                )
            new_names: list[str] = []
            for index, name in enumerate(selected):
                new_name = f"__sr_{shard_no}_in_{index}"
                physical_params[new_name] = params[name]
                new_names.append(new_name)
            predicate_after = (
                f"{table.shard_key} IN ("
                + ", ".join(f":{name}" for name in new_names)
                + ")"
            )
            sql = sql[: match.start()] + predicate_after + sql[match.end() :]
            pruned = len(selected) != len(body_names)
            prune_reason = (
                "IN list clipped to values routed to this shard"
                if pruned
                else "IN list contains only values routed to this shard"
            )
    statement_digest = digest(
        {
            "shard_no": shard_no,
            "physical_table": physical_table,
            "sql": sql,
            "params": physical_params,
        }
    )
    return {
        "sql": sql,
        "params": physical_params,
        "predicate_before": predicate_before,
        "predicate_after": predicate_after,
        "pruned": pruned,
        "prune_reason": prune_reason,
        "statement_digest": statement_digest,
    }


def build_plan(
    *,
    template: Template,
    table: LogicalTable,
    rule: MergedRule,
    mappings: tuple[PhysicalMapping, ...],
    params: dict[str, Any],
) -> dict[str, Any]:
    if template.logical_table != table.name:
        fail(
            "E_INTERNAL",
            "template/table mismatch during planning",
            template=template.template_id,
            table=table.name,
        )
    if template.logical_table not in rule.routing:
        fail(
            "E_RULE_ROUTING_MISSING",
            "the bound rule has no route for the template table",
            table=template.logical_table,
            rule_version=rule.version,
        )
    routing = rule.routing[table.name]
    route = target_shards(table, routing, template, params)
    shard_plans: list[dict[str, Any]] = []
    for shard_no in route["shards"]:
        mapping = mappings[shard_no]
        rewrite = _rewrite_shard_sql(
            template=template,
            table=table,
            routing=routing,
            shard_no=shard_no,
            physical_table=mapping.physical_table,
            params=params,
        )
        shard_plans.append(
            {
                "shard_no": shard_no,
                "database": mapping.database_ref,
                "database_absolute": str(mapping.database),
                "physical_table": mapping.physical_table,
                "role": mapping.role,
                "rule_version": rule.version,
                **rewrite,
            }
        )
    result: dict[str, Any] = {
        "template_id": template.template_id,
        "logical_table": table.name,
        "kind": template.kind,
        "parameters": params,
        "all_route": route["all_route"],
        "route": route,
        "rule_version": rule.version,
        "rule_digest": rule.digest,
        "rule_source": rule.source,
        "rule_source_priority": rule.source_priority,
        "rule_effective_clock_ms": rule.effective_clock_ms,
        "rule_generation": rule.generation,
        "routing_model": routing.to_dict(),
        "target_shards": route["shards"],
        "shards": shard_plans,
        "aggregate": {
            "count": template.aggregate_count,
            "sum": template.aggregate_sum,
        },
        "order_by": list(template.order_by),
    }
    result["plan_digest"] = plan_digest_without_self(result)
    return result


def plan_digest_without_self(plan: dict[str, Any]) -> str:
    copy = dict(plan)
    copy.pop("plan_digest", None)
    copy["shards"] = [
        {
            key: value
            for key, value in shard.items()
            if key != "database_absolute"
        }
        for shard in copy.get("shards", [])
    ]
    return digest(copy)
