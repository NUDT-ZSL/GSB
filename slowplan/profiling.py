from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import BusinessError, E_MISSING_STAT, E_ZERO_ESTIMATE
from .stats import Catalog
from .util import rounded_decimal, rounded_number


INDEX_OVERHEAD = Decimal("2.0")
RANGE_SELECTIVITY = Decimal("0.333333")
IN_CANONICAL_CARDINALITY = Decimal("3")


def _decimal_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _stats_item(item_key: str, kind: str, table: str, column: str | None, index: str | None, value: Any) -> dict[str, Any]:
    return {
        "stats_item": item_key,
        "kind": kind,
        "table": table,
        "column": column,
        "index": index,
        "value": value,
    }


def _predicate_factor(predicate: dict[str, Any], ndv: int) -> tuple[Decimal, dict[str, Any], list[str]]:
    column = predicate["column"]
    operator = predicate["op"]
    if operator in {"=", "IN"}:
        numerator = IN_CANONICAL_CARDINALITY if operator == "IN" else Decimal(1)
        factor = min(Decimal(1), numerator / Decimal(ndv))
        item = _stats_item(
            f"column:{predicate['_table']}.{column}:ndv",
            "column_ndv",
            predicate["_table"],
            column,
            None,
            ndv,
        )
        step = f"{column} {operator} selectivity = {numerator}/{ndv} = {factor}"
    else:
        factor = RANGE_SELECTIVITY
        item = _stats_item(
            f"column:{predicate['_table']}.{column}:ndv",
            "column_ndv",
            predicate["_table"],
            column,
            None,
            ndv,
        )
        step = f"{column} {operator} range selectivity = {factor}"
    return factor, item, [step]


def _candidate_paths(
    table: str,
    row_count: int,
    predicates: list[dict[str, Any]],
    column_ndv: dict[str, int],
    index_stats: dict[str, dict[str, Any]],
    catalog_table: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    for predicate in predicates:
        column = predicate["column"]
        if column not in column_ndv:
            raise BusinessError(
                E_MISSING_STAT,
                "query predicate column has no NDV statistic",
                {"table": table, "column": column},
            )
    predicate_info: list[dict[str, Any]] = []
    for predicate in predicates:
        ndv = column_ndv[predicate["column"]]
        factor, item, steps = _predicate_factor(predicate, ndv)
        predicate_info.append(
            {
                "column": predicate["column"],
                "operator": predicate["op"],
                "ndv": ndv,
                "factor": factor,
                "source": item,
                "steps": steps,
            }
        )
    full_factor = Decimal(1)
    for info in predicate_info:
        full_factor *= info["factor"]
    full_estimate = Decimal(row_count) * full_factor
    full_sources = [
        _stats_item(f"table:{table}:row_count", "table_row_count", table, None, None, row_count)
    ] + [info["source"] for info in predicate_info]
    full_steps = [f"base_rows = {row_count}"]
    for info in predicate_info:
        full_steps.extend(info["steps"])
    full_steps.append(f"estimated_rows = round({row_count} * {full_factor}) = {_decimal_int(full_estimate)}")
    full_steps.append(f"estimated_cost = table_scan({row_count})")
    full_scan = {
        "path_id": "full_scan",
        "path_type": "full_table_scan",
        "table": table,
        "index": None,
        "columns": [],
        "selectivity": rounded_number(full_factor),
        "estimated_rows": _decimal_int(full_estimate),
        "estimated_cost": rounded_number(Decimal(row_count)),
        "sources": full_sources,
        "steps": full_steps,
    }
    candidates = [full_scan]
    for index in sorted(catalog_table["indexes"], key=lambda item: item["name"]):
        index_name = index["name"]
        columns = index["columns"]
        if not columns:
            continue
        leading = columns[0]
        leading_predicates = [
            predicate for predicate in predicates if predicate["column"] == leading and predicate["op"] in {"=", "IN"}
        ]
        if not leading_predicates:
            continue
        index_stat = index_stats[index_name]
        prefix = Decimal(str(index_stat["prefix_selectivity"]))
        extra_factor = Decimal(1)
        extra_sources: list[dict[str, Any]] = []
        extra_steps: list[str] = []
        excluded_columns = {leading}
        for predicate in predicates:
            if predicate["column"] in excluded_columns:
                continue
            ndv = column_ndv[predicate["column"]]
            factor, source, steps = _predicate_factor(predicate, ndv)
            extra_factor *= factor
            extra_sources.append(source)
            extra_steps.extend(steps)
        estimate_decimal = Decimal(row_count) * prefix * extra_factor
        estimated_rows = _decimal_int(estimate_decimal)
        cost = Decimal(estimated_rows) + INDEX_OVERHEAD
        sources = [
            _stats_item(f"table:{table}:row_count", "table_row_count", table, None, None, row_count),
            _stats_item(
                f"index:{index_name}:prefix_selectivity",
                "index_prefix_selectivity",
                table,
                None,
                index_name,
                index_stat["prefix_selectivity"],
            ),
        ] + extra_sources
        steps = [
            f"base_rows = {row_count}",
            f"index_prefix_selectivity({index_name}) = {prefix}",
            f"prefix_rows = round({row_count} * {prefix}) = {_decimal_int(Decimal(row_count) * prefix)}",
        ]
        steps.extend(extra_steps)
        steps.append(
            f"estimated_rows = round({row_count} * {prefix} * {extra_factor}) = {estimated_rows}"
        )
        steps.append(f"estimated_cost = estimated_rows + {INDEX_OVERHEAD} = {cost}")
        candidates.append(
            {
                "path_id": f"index:{index_name}",
                "path_type": "index_scan",
                "table": table,
                "index": index_name,
                "columns": columns,
                "selectivity": rounded_number(prefix * extra_factor),
                "estimated_rows": estimated_rows,
                "estimated_cost": rounded_number(cost),
                "sources": sources,
                "steps": steps,
            }
        )
    candidates.sort(key=lambda item: item["path_id"])
    return candidates, predicate_info


def build_profile(
    template_id: str,
    template_sql: str,
    structure: dict[str, Any],
    aggregate: dict[str, Any],
    snapshot: dict[str, Any],
    catalog: Catalog,
    tolerance: float,
) -> dict[str, Any]:
    table = structure["table"]
    if table not in snapshot["tables"]:
        raise BusinessError(E_MISSING_STAT, "query table has no statistics", {"table": table})
    table_stats = snapshot["tables"][table]
    predicates = []
    for predicate in structure["predicates"]:
        copied = dict(predicate)
        copied["_table"] = table
        predicates.append(copied)
    column_ndv = {
        column: table_stats["columns"][column]["ndv"]
        for column in table_stats["columns"]
    }
    candidates, predicate_info = _candidate_paths(
        table,
        table_stats["row_count"],
        predicates,
        column_ndv,
        table_stats["indexes"],
        catalog.tables[table],
    )
    selected = min(candidates, key=lambda item: (Decimal(str(item["estimated_cost"])), item["path_id"]))
    others = [candidate for candidate in candidates if candidate["path_id"] != selected["path_id"]]
    best_other = min(others, key=lambda item: (Decimal(str(item["estimated_cost"])), item["path_id"])) if others else None
    tolerance_decimal = Decimal(str(tolerance))
    precision_sensitive = False
    cost_delta: Decimal | None = None
    error_source_chain: list[dict[str, Any]] = []
    if best_other is not None:
        cost_delta = abs(Decimal(str(selected["estimated_cost"])) - Decimal(str(best_other["estimated_cost"])))
        precision_sensitive = cost_delta < tolerance_decimal
    if precision_sensitive:
        for candidate in sorted([selected, best_other], key=lambda item: item["path_id"]):
            error_source_chain.append(
                {
                    "path_id": candidate["path_id"],
                    "sources": candidate["sources"],
                    "conversion_steps": candidate["steps"],
                }
            )
    measured = Decimal(str(aggregate["scanned_rows"]["mean"]))
    estimated = Decimal(selected["estimated_rows"])
    if estimated == 0 and measured > 0:
        raise BusinessError(
            E_ZERO_ESTIMATE,
            "selected plan estimates zero rows while measured scans are positive",
            {
                "template_id": template_id,
                "table": table,
                "selected_path": selected["path_id"],
                "estimated_rows": 0,
                "measured_scanned_rows_mean": float(measured),
                "stats_version": snapshot["version"],
                "error_source_chain": {
                    "sources": selected["sources"],
                    "conversion_steps": selected["steps"],
                },
            },
        )
    ratio = (measured / estimated) if estimated > 0 else None
    if ratio is None:
        direction = "unknown"
    elif rounded_decimal(ratio) > Decimal("1.000000"):
        direction = "underestimate"
    elif rounded_decimal(ratio) < Decimal("0.999999"):
        direction = "overestimate"
    else:
        direction = "aligned"
    conclusion = {
        "template_id": template_id,
        "template_sql": template_sql,
        "structure": {
            "table": table,
            "predicates": [
                {"column": item["column"], "op": item["op"]} for item in structure["predicates"]
            ],
        },
        "stats_version": snapshot["version"],
        "stats_content_digest": snapshot["content_digest"],
        "database_digest": snapshot["database_digest"],
        "aggregate": aggregate,
        "candidate_paths": candidates,
        "selected_path": selected["path_id"],
        "selected_estimate": {
            "estimated_rows": selected["estimated_rows"],
            "estimated_cost": selected["estimated_cost"],
            "selectivity": selected["selectivity"],
        },
        "measured_scanned_rows_mean": float(measured),
        "deviation_ratio": rounded_number(ratio) if ratio is not None else None,
        "deviation_direction": direction,
        "precision_sensitive": precision_sensitive,
        "tolerance": rounded_number(tolerance_decimal),
        "cost_delta": rounded_number(cost_delta) if cost_delta is not None else None,
        "error_source_chain": error_source_chain,
        "selection_rule": "minimum estimated_cost, ties resolved by lexicographic path_id",
    }
    return conclusion
