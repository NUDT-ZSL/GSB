from __future__ import annotations

import itertools
import math
import sqlite3
from typing import Any

from .model import CANONICAL_JOIN_ORDER, JOIN_BY_TABLE, PredicateEstimate, QuerySpec


def _row_counts(conn: sqlite3.Connection, version: str) -> dict[str, int]:
    return {
        str(row["table_name"]): int(row["row_count"])
        for row in conn.execute(
            "SELECT table_name, row_count FROM stat_row_counts WHERE version = ?",
            (version,),
        )
    }


def _domains(
    conn: sqlite3.Connection, version: str
) -> dict[tuple[str, str], sqlite3.Row]:
    return {
        (str(row["left_table"]), str(row["left_column"])): row
        for row in conn.execute(
            """
            SELECT * FROM stat_join_domains
            WHERE version = ? AND left_table = 'patrol_logs'
            """,
            (version,),
        )
    }


def _filter_selectivity_by_table(predicates: dict[str, PredicateEstimate]) -> dict[str, float]:
    return {
        "regions": predicates["region"].final_selectivity,
        "bands": predicates["band"].final_selectivity,
        "base_stations": predicates["region"].final_selectivity,
        "terminals": predicates["band"].final_selectivity,
        "routes": predicates["region"].final_selectivity,
        "rangers": predicates["region"].final_selectivity,
    }


def _order_cost(
    order: tuple[str, ...],
    *,
    fact_rows: int,
    filtered_rows: float,
    row_counts: dict[str, int],
    domains: dict[tuple[str, str], sqlite3.Row],
    table_selectivity: dict[str, float],
) -> float:
    if not order:
        return filtered_rows
    current = max(0.0, filtered_rows)
    cost = 5.0
    first = order[0]
    if first == "__fact__":
        cost += filtered_rows * 0.42 + 2.0
        remaining = order[1:]
    else:
        first_rows = float(row_counts.get(first, 1))
        first_selectivity = max(0.001, table_selectivity.get(first, 1.0))
        cost += first_rows * 0.16
        cost += fact_rows * max(0.0001, first_selectivity) * 0.55
        cost += filtered_rows * 0.10
        remaining = order[1:]

    for table in remaining:
        spec = JOIN_BY_TABLE[table]
        domain = domains.get(("patrol_logs", spec.fact_column))
        containment = float(domain["containment"]) if domain else 1.0
        multiplicity = float(domain["right_multiplicity"]) if domain else 1.0
        right_rows = float(row_counts.get(table, 1))
        cost += right_rows * 0.07
        cost += current * 0.035
        cost += current * max(0.0, 1.0 - containment) * 0.20
        current *= max(0.0, containment) * max(0.0, multiplicity)
    return cost + max(0.0, current) * 0.001


def recommend_join_order(
    conn: sqlite3.Connection,
    version: str,
    query: QuerySpec,
    predicates: dict[str, PredicateEstimate],
    selected_base: float,
) -> dict[str, Any]:
    row_counts = _row_counts(conn, version)
    domains = _domains(conn, version)
    fact_rows = row_counts.get("patrol_logs", 0)
    table_selectivity = _filter_selectivity_by_table(predicates)

    candidates: list[tuple[str, ...]] = []
    candidates.append(("__fact__",) + CANONICAL_JOIN_ORDER)
    # The minimum-cost dimension first is determined by the same cost model;
    # enumerate all orders to avoid baking in a particular workload.
    for permutation in itertools.permutations(CANONICAL_JOIN_ORDER):
        candidates.append(permutation)

    scored: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for order in candidates:
        if order in seen:
            continue
        seen.add(order)
        cost = _order_cost(
            order,
            fact_rows=fact_rows,
            filtered_rows=selected_base,
            row_counts=row_counts,
            domains=domains,
            table_selectivity=table_selectivity,
        )
        scored.append(
            {
                "order": list(order),
                "cost": round(cost, 4),
            }
        )
    scored.sort(key=lambda item: (item["cost"], item["order"]))
    best = scored[0]
    baseline = next(
        item
        for item in scored
        if item["order"] == ["__fact__"] + list(CANONICAL_JOIN_ORDER)
    )
    first_dimension = best["order"][0]
    if first_dimension == "__fact__":
        reason = (
            f"过滤后预计仅 {selected_base:.1f} 行，先扫描 patrol_logs 再探测维表"
            f" 比维表驱动更便宜；代价 {best['cost']:.2f}"
        )
    else:
        reason = (
            f"{first_dimension} 维表行数小且区域/频段选择性较高，先做维表过滤"
            f" 可将探测行数压低；代价 {best['cost']:.2f}"
        )
    return {
        "query_id": query.query_id,
        "recommended_order": best["order"],
        "baseline_order": baseline["order"],
        "recommended_cost": best["cost"],
        "baseline_cost": baseline["cost"],
        "reason": reason,
        "top_candidates": scored[:5],
    }


def recommend_indexes(
    conn: sqlite3.Connection,
    version: str,
    query: QuerySpec,
    predicates: dict[str, PredicateEstimate],
    selected_base: float,
) -> list[dict[str, Any]]:
    row_counts = _row_counts(conn, version)
    fact_rows = max(1, row_counts.get("patrol_logs", 0))
    region_sel = max(predicates["region"].final_selectivity, 0.000001)
    band_sel = max(predicates["band"].final_selectivity, 0.000001)
    time_sel = max(predicates["time"].final_selectivity, 0.000001)
    filtered_ratio = max(0.0, min(1.0, selected_base / fact_rows))
    full_scan_cost = fact_rows * 0.8 + 8.0
    composite_scan_cost = (
        math.log2(fact_rows + 2) * 0.8 + max(1.0, selected_base) * 0.42 + 12.0
    )
    composite_benefit = full_scan_cost - composite_scan_cost

    leading_equal = "band_id" if band_sel < region_sel else "region_id"
    second_equal = "region_id" if leading_equal == "band_id" else "band_id"
    composite_columns = [leading_equal, second_equal, "logged_at"]

    decisions: list[dict[str, Any]] = []
    if composite_benefit > 0 and filtered_ratio < 0.65:
        decisions.append(
            {
                "index_name": "idx_patrol_logs_filter_window",
                "table_name": "patrol_logs",
                "columns": composite_columns,
                "decision": "accept",
                "reason": (
                    f"覆盖区域、频段与时间范围，预计只扫描 {filtered_ratio:.1%} "
                    f"的 patrol_logs（约 {selected_base:.1f} 行），"
                    f"相对全表扫描节省 {composite_benefit:.1f} 代价单位"
                ),
                "benefit": round(composite_benefit, 4),
                "cost": round(composite_scan_cost, 4),
            }
        )
        alternative_columns = [second_equal, leading_equal, "logged_at"]
        decisions.append(
            {
                "index_name": "idx_patrol_logs_filter_window_alt",
                "table_name": "patrol_logs",
                "columns": alternative_columns,
                "decision": "reject",
                "reason": (
                    "与已选复合索引包含相同等值前导列和时间范围列；"
                    "两个等值列顺序对当前两类谓词不产生额外收益，保留会重复维护"
                ),
                "benefit": 0.0,
                "cost": round(composite_scan_cost + 3.0, 4),
            }
        )
    else:
        decisions.append(
            {
                "index_name": "idx_patrol_logs_filter_window",
                "table_name": "patrol_logs",
                "columns": composite_columns,
                "decision": "reject",
                "reason": (
                    f"过滤后预计仍占基表 {filtered_ratio:.1%}，索引随机读与维护成本"
                    "可能高于顺序扫描"
                ),
                "benefit": round(composite_benefit, 4),
                "cost": round(composite_scan_cost, 4),
            }
        )

    decisions.append(
        {
            "index_name": "idx_patrol_logs_logged_at",
            "table_name": "patrol_logs",
            "columns": ["logged_at"],
            "decision": "reject",
            "reason": (
                "时间范围只出现于组合查询，单列时间索引不能同时收敛区域和频段；"
                f"现有多列候选已把 logged_at 作为范围列（时间选择率 {time_sel:.2%}）"
            ),
            "benefit": 0.0,
            "cost": round(filtered_ratio * full_scan_cost, 4),
        }
    )

    for column in ("terminal_id", "ranger_id", "route_id", "station_id"):
        decisions.append(
            {
                "index_name": f"idx_patrol_logs_{column}",
                "table_name": "patrol_logs",
                "columns": [column],
                "decision": "reject",
                "reason": (
                    f"{column} 在事实表向外连接维表，维表主键已可 O(logN) 探测；"
                    "为事实表再建该列索引通常不会被本查询计划选中"
                ),
                "benefit": 0.0,
                "cost": 0.0,
            }
        )

    decisions.append(
        {
            "index_name": "idx_terminals_band_region",
            "table_name": "terminals",
            "columns": ["band_id", "home_region_id"],
            "decision": "reject",
            "reason": (
                "当前工作负载把频段谓词落在 patrol_logs.band_id，维表按主键探测，"
                "不存在按终端频段过滤维表的计划分支"
            ),
            "benefit": 0.0,
            "cost": 0.0,
        }
    )
    return decisions
