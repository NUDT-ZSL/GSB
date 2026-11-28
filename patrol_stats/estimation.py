from __future__ import annotations

import sqlite3
from typing import Any

from .db import json_dumps
from .generator import load_queries
from .model import (
    CANONICAL_JOIN_ORDER,
    JOIN_BY_TABLE,
    PredicateEstimate,
    QuerySpec,
)
from .statistics import estimate_combo_selectivity, estimate_predicate


def relative_error(estimate: float, actual: int | float) -> float:
    denominator = max(abs(float(actual)), 1.0)
    return abs(float(estimate) - float(actual)) / denominator


def predicate_bundle(
    conn: sqlite3.Connection, version: str, query: QuerySpec
) -> dict[str, PredicateEstimate]:
    return {
        "region": estimate_predicate(
            conn, version, "region", value=query.region_id
        ),
        "band": estimate_predicate(
            conn, version, "band", value=query.band_id
        ),
        "time": estimate_predicate(
            conn,
            version,
            "time",
            start_ts=query.start_ts,
            end_ts=query.end_ts,
        ),
    }


def base_estimate(
    conn: sqlite3.Connection,
    version: str,
    query: QuerySpec,
    predicates: dict[str, PredicateEstimate] | None = None,
) -> dict[str, Any]:
    predicates = predicates or predicate_bundle(conn, version, query)
    row = conn.execute(
        """
        SELECT row_count FROM stat_row_counts
        WHERE version = ? AND table_name = 'patrol_logs'
        """,
        (version,),
    ).fetchone()
    if row is None:
        raise LookupError(f"missing patrol_logs row count for {version}")
    fact_rows = float(row["row_count"])
    region_band_independence = (
        predicates["region"].final_selectivity
        * predicates["band"].final_selectivity
    )
    independence_selectivity = (
        region_band_independence * predicates["time"].final_selectivity
    )
    combo = estimate_combo_selectivity(
        conn,
        version,
        query.region_id,
        query.band_id,
        region_band_independence,
    )
    combo_selectivity = combo["selectivity"] * predicates["time"].final_selectivity
    return {
        "fact_rows": fact_rows,
        "predicates": predicates,
        "independence_selectivity": independence_selectivity,
        "combo_selectivity": combo_selectivity,
        "independence_base": fact_rows * independence_selectivity,
        "combo_base": fact_rows * combo_selectivity,
        "combo": combo,
        "selected_base": fact_rows * combo_selectivity,
        "selected_scope": str(combo["scope"]),
    }


def _join_domain(
    conn: sqlite3.Connection, version: str, table: str
) -> sqlite3.Row | None:
    spec = JOIN_BY_TABLE[table]
    return conn.execute(
        """
        SELECT * FROM stat_join_domains
        WHERE version = ? AND left_table = 'patrol_logs'
          AND left_column = ? AND right_table = ? AND right_column = ?
        """,
        (version, spec.fact_column, table, spec.right_column),
    ).fetchone()


def scan_actual_query(
    conn: sqlite3.Connection,
    query: QuerySpec,
    join_order: tuple[str, ...] = CANONICAL_JOIN_ORDER,
) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT log_id, ranger_id, terminal_id, route_id, region_id,
               band_id, station_id, logged_at
        FROM patrol_logs
        WHERE region_id = ? AND band_id = ?
          AND logged_at >= ? AND logged_at <= ?
        ORDER BY log_id
        """,
        (query.region_id, query.band_id, query.start_ts, query.end_ts),
    )
    current: list[tuple[tuple[Any, ...], int]] = [
        (tuple(row), 1) for row in cursor.fetchall()
    ]
    node_actuals: list[int] = [sum(multiplicity for _, multiplicity in current)]
    for table in join_order:
        spec = JOIN_BY_TABLE[table]
        where_sql = f" WHERE {spec.right_filter}" if spec.right_filter else ""
        key_counts = {
            row[0]: int(row[1])
            for row in conn.execute(
                f"SELECT {spec.right_column}, COUNT(*) FROM {table} "
                f"{where_sql} GROUP BY {spec.right_column}"
            )
        }
        fact_index = {
            "ranger_id": 1,
            "terminal_id": 2,
            "route_id": 3,
            "region_id": 4,
            "band_id": 5,
            "station_id": 6,
        }[spec.fact_column]
        next_rows: list[tuple[tuple[Any, ...], int]] = []
        for row, multiplicity in current:
            matches = key_counts.get(row[fact_index], 0)
            if matches:
                next_rows.append((row, multiplicity * matches))
        current = next_rows
        node_actuals.append(sum(multiplicity for _, multiplicity in current))

    region_actual = int(
        conn.execute(
            "SELECT COUNT(*) FROM patrol_logs WHERE region_id = ?",
            (query.region_id,),
        ).fetchone()[0]
    )
    band_actual = int(
        conn.execute(
            "SELECT COUNT(*) FROM patrol_logs WHERE band_id = ?",
            (query.band_id,),
        ).fetchone()[0]
    )
    time_actual = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM patrol_logs
            WHERE logged_at >= ? AND logged_at <= ?
            """,
            (query.start_ts, query.end_ts),
        ).fetchone()[0]
    )
    return {
        "node_actuals": node_actuals,
        "base_actual": node_actuals[0],
        "final_actual": node_actuals[-1],
        "predicate_actuals": {
            "region": region_actual,
            "band": band_actual,
            "time": time_actual,
        },
        "scanned_rows": sum(multiplicity for _, multiplicity in current),
    }


def _predicate_payload(predicate: PredicateEstimate) -> dict[str, Any]:
    return {
        "predicate": predicate.predicate,
        "column": predicate.column,
        "operator": predicate.operator,
        "literal": predicate.literal,
        "histogram_selectivity": round(predicate.histogram_selectivity, 9),
        "sample_selectivity": round(predicate.sample_selectivity, 9),
        "final_selectivity": round(predicate.final_selectivity, 9),
        "sample_size": predicate.sample_size,
        "total_rows": predicate.total_rows,
        "sample_weight": round(predicate.sample_weight, 6),
        "hit_buckets": predicate.hit_buckets,
        "formula": predicate.formula,
    }


def estimate_query(
    conn: sqlite3.Connection,
    version: str,
    query: QuerySpec,
    *,
    join_order: tuple[str, ...] = CANONICAL_JOIN_ORDER,
    actual_scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = base_estimate(conn, version, query)
    actual = actual_scan or scan_actual_query(conn, query, join_order)
    node_actuals = actual["node_actuals"]
    if len(node_actuals) != len(join_order) + 1:
        raise ValueError("actual scan and join order disagree")

    nodes: list[dict[str, Any]] = []
    independence_est = float(base["independence_base"])
    combo_est = float(base["combo_base"])
    selected_scope = str(base["selected_scope"])
    cumulative_correction = 1.0

    def append_node(
        *,
        node_order: int,
        node_id: str,
        node_name: str,
        join_table: str | None,
        join_key: str | None,
        overlap_ratio: float,
        right_multiplicity: float,
    ) -> None:
        actual_rows = int(node_actuals[node_order])
        selected_for_node = combo_est if selected_scope.startswith("combo") else independence_est
        error = relative_error(selected_for_node, actual_rows)
        if error > query.deviation_threshold:
            if node_order == 0:
                if base["combo"]["fallback"]:
                    reason = (
                        "区域频段组合未在采样组合摘要中命中，回退独立性估计；"
                        "稀疏组合被独立性假设高估/低估"
                    )
                elif base["combo"]["source"] == "sample":
                    reason = (
                        f"区域频段组合来自 {base['combo']['sample_size']} 行采样，"
                        "热点组合与稀疏组合的联合频率存在采样误差"
                    )
                else:
                    reason = (
                        "桶内均匀假设与采样混合后仍偏离真实窗口行数；"
                        "检查时间桶边界覆盖和区域频段联合分布"
                    )
            else:
                reason = (
                    f"继承上游基数偏差并叠加 {join_table} 键值域包含率 "
                    f"{overlap_ratio:.4f} 与右表多重度 {right_multiplicity:.4f}"
                )
        else:
            reason = "估计值与逐行扫描真实行数的相对偏差在阈值内"
        nodes.append(
            {
                "node_order": node_order,
                "node_id": node_id,
                "node_name": node_name,
                "join_table": join_table,
                "join_key": join_key,
                "estimate_independence": independence_est,
                "estimate_combo": combo_est,
                "selected_estimate": selected_for_node,
                "actual_rows": actual_rows,
                "relative_error": error,
                "scope": selected_scope,
                "overlap_ratio": overlap_ratio,
                "right_multiplicity": right_multiplicity,
                "correction_factor": cumulative_correction,
                "reason": reason,
                "detail": {
                    "formula": "base_selectivity * product(containment * right_multiplicity)",
                    "combo": base["combo"],
                    "time_selectivity": base["predicates"]["time"].final_selectivity,
                },
            }
        )

    append_node(
        node_order=0,
        node_id="N00",
        node_name="patrol_logs 过滤后基表",
        join_table=None,
        join_key=None,
        overlap_ratio=1.0,
        right_multiplicity=1.0,
    )
    for offset, table in enumerate(join_order, start=1):
        spec = JOIN_BY_TABLE[table]
        domain = _join_domain(conn, version, table)
        containment = float(domain["containment"]) if domain else 1.0
        multiplicity = float(domain["right_multiplicity"]) if domain else 1.0
        correction = containment * multiplicity
        independence_est *= correction
        combo_est *= correction
        cumulative_correction *= correction
        append_node(
            node_order=offset,
            node_id=spec.node_id,
            node_name=f"{spec.label} ({table})",
            join_table=table,
            join_key=f"patrol_logs.{spec.fact_column} = {table}.{spec.right_column}",
            overlap_ratio=containment,
            right_multiplicity=multiplicity,
        )

    first_breach = next(
        (
            node["node_id"]
            for node in nodes
            if node["relative_error"] > query.deviation_threshold
        ),
        None,
    )
    errors = [float(node["relative_error"]) for node in nodes]
    selected_for_nodes = combo_est if selected_scope.startswith("combo") else independence_est
    return {
        "query": query,
        "version": version,
        "fact_rows": base["fact_rows"],
        "base_estimate": base,
        "nodes": nodes,
        "predicates": base["predicates"],
        "actual": actual,
        "independence_base": float(base["independence_base"]),
        "combo_base": float(base["combo_base"]),
        "selected_base": float(base["selected_base"]),
        "selected_final": selected_for_nodes,
        "actual_rows": int(actual["final_actual"]),
        "mean_node_error": sum(errors) / len(errors),
        "max_node_error": max(errors),
        "first_breach_node": first_breach,
        "estimate_scope": selected_scope,
        "predicate_json": json_dumps(
            {
                "region": _predicate_payload(base["predicates"]["region"]),
                "band": _predicate_payload(base["predicates"]["band"]),
                "time": _predicate_payload(base["predicates"]["time"]),
                "independence_selectivity": base["independence_selectivity"],
                "combo_selectivity": base["combo_selectivity"],
                "combo": base["combo"],
            }
        ),
    }


def estimate_workload(
    conn: sqlite3.Connection,
    version: str,
    query_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    queries = load_queries(conn, query_ids)
    results: list[dict[str, Any]] = []
    for query in queries:
        results.append(estimate_query(conn, version, query))
    return results
