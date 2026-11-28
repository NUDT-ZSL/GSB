from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import json_dumps, json_loads
from .estimation import (
    base_estimate,
    estimate_query,
    predicate_bundle,
    scan_actual_query,
)
from .generator import load_queries
from .optimizer import recommend_indexes, recommend_join_order
from .statistics import stat_fingerprint, utc_now


def _latest_completed_run(
    conn: sqlite3.Connection, version: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM evaluation_runs
        WHERE version = ? AND completed_at IS NOT NULL
        ORDER BY run_id DESC LIMIT 1
        """,
        (version,),
    ).fetchone()


def evaluate_workload(
    conn: sqlite3.Connection,
    version: str,
    *,
    query_ids: list[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    queries = load_queries(conn, query_ids)
    if not queries:
        raise ValueError("no queries selected")
    started_at = utc_now()
    fingerprint = stat_fingerprint(conn, version)
    cursor = conn.execute(
        """
        INSERT INTO evaluation_runs
            (version, started_at, query_count, stat_fingerprint, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (version, started_at, len(queries), fingerprint, note),
    )
    run_id = int(cursor.lastrowid)
    all_errors: list[float] = []
    per_query: list[dict[str, Any]] = []

    try:
        for query in queries:
            predicates = predicate_bundle(conn, version, query)
            base = base_estimate(conn, version, query, predicates)
            join_recommendation = recommend_join_order(
                conn, version, query, predicates, float(base["selected_base"])
            )
            order_with_marker = join_recommendation["recommended_order"]
            join_order = tuple(
                table for table in order_with_marker if table != "__fact__"
            )
            actual = scan_actual_query(conn, query, join_order)
            estimate = estimate_query(
                conn,
                version,
                query,
                join_order=join_order,
                actual_scan=actual,
            )
            index_decisions = recommend_indexes(
                conn, version, query, predicates, float(base["selected_base"])
            )

            cursor = conn.execute(
                """
                INSERT INTO query_evaluations
                    (run_id, version, query_id, region_id, band_id, start_ts,
                     end_ts, deviation_threshold, fact_rows, independence_base,
                     combo_base, selected_base, actual_rows, mean_node_error,
                     max_node_error, first_breach_node, estimate_scope,
                     predicate_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    version,
                    query.query_id,
                    query.region_id,
                    query.band_id,
                    query.start_ts,
                    query.end_ts,
                    query.deviation_threshold,
                    int(estimate["fact_rows"]),
                    estimate["independence_base"],
                    estimate["combo_base"],
                    estimate["selected_base"],
                    estimate["actual_rows"],
                    estimate["mean_node_error"],
                    estimate["max_node_error"],
                    estimate["first_breach_node"],
                    estimate["estimate_scope"],
                    estimate["predicate_json"],
                    utc_now(),
                ),
            )
            eval_id = int(cursor.lastrowid)
            for node in estimate["nodes"]:
                conn.execute(
                    """
                    INSERT INTO node_estimates
                        (eval_id, node_order, node_id, node_name, join_table,
                         join_key, estimate_independence, estimate_combo,
                         selected_estimate, actual_rows, relative_error, scope,
                         overlap_ratio, right_multiplicity, correction_factor,
                         first_over_threshold, reason, detail_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eval_id,
                        node["node_order"],
                        node["node_id"],
                        node["node_name"],
                        node["join_table"],
                        node["join_key"],
                        node["estimate_independence"],
                        node["estimate_combo"],
                        node["selected_estimate"],
                        node["actual_rows"],
                        node["relative_error"],
                        node["scope"],
                        node["overlap_ratio"],
                        node["right_multiplicity"],
                        node["correction_factor"],
                        1
                        if node["relative_error"] > query.deviation_threshold
                        else 0,
                        node["reason"],
                        json_dumps(node["detail"]),
                    ),
                )
                all_errors.append(float(node["relative_error"]))

            conn.execute(
                """
                INSERT INTO join_order_recommendations
                    (run_id, query_id, recommended_order_json,
                     baseline_order_json, costs_json, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    query.query_id,
                    json_dumps(join_recommendation["recommended_order"]),
                    json_dumps(join_recommendation["baseline_order"]),
                    json_dumps(
                        {
                            "recommended_cost": join_recommendation["recommended_cost"],
                            "baseline_cost": join_recommendation["baseline_cost"],
                            "top_candidates": join_recommendation["top_candidates"],
                        }
                    ),
                    join_recommendation["reason"],
                ),
            )
            for decision in index_decisions:
                conn.execute(
                    """
                    INSERT INTO index_recommendations
                        (run_id, query_id, index_name, table_name, columns_json,
                         decision, reason, estimated_benefit, estimated_cost)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        query.query_id,
                        decision["index_name"],
                        decision["table_name"],
                        json_dumps(decision["columns"]),
                        decision["decision"],
                        decision["reason"],
                        float(decision["benefit"]),
                        float(decision["cost"]),
                    ),
                )

            per_query.append(
                {
                    "query_id": query.query_id,
                    "selected_base": float(estimate["selected_base"]),
                    "actual_rows": int(estimate["actual_rows"]),
                    "mean_node_error": float(estimate["mean_node_error"]),
                    "max_node_error": float(estimate["max_node_error"]),
                    "first_breach_node": estimate["first_breach_node"],
                    "scope": estimate["estimate_scope"],
                    "join_order": list(join_order),
                    "node_count": len(estimate["nodes"]),
                }
            )

        mean_error = sum(all_errors) / len(all_errors) if all_errors else 0.0
        max_error = max(all_errors) if all_errors else 0.0
        conn.execute(
            """
            UPDATE evaluation_runs
            SET completed_at = ?, mean_abs_rel_error = ?, max_abs_rel_error = ?
            WHERE run_id = ?
            """,
            (utc_now(), mean_error, max_error, run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "run_id": run_id,
        "version": version,
        "query_count": len(queries),
        "mean_abs_rel_error": mean_error,
        "max_abs_rel_error": max_error,
        "stat_fingerprint": fingerprint,
        "queries": per_query,
    }


def snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    if run_id is None:
        if version is None:
            raise ValueError("run_id or version is required")
        run = _latest_completed_run(conn, version)
        if run is None:
            raise LookupError(f"no completed evaluation run for {version}")
        run_id = int(run["run_id"])
    run = conn.execute(
        "SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise LookupError(f"evaluation run not found: {run_id}")

    query_rows = []
    for query_eval in conn.execute(
        """
        SELECT * FROM query_evaluations
        WHERE run_id = ? ORDER BY query_id
        """,
        (run_id,),
    ):
        nodes = []
        for node in conn.execute(
            """
            SELECT * FROM node_estimates
            WHERE eval_id = ? ORDER BY node_order
            """,
            (query_eval["eval_id"],),
        ):
            nodes.append(
                {
                    "node_order": int(node["node_order"]),
                    "node_id": node["node_id"],
                    "node_name": node["node_name"],
                    "join_table": node["join_table"],
                    "join_key": node["join_key"],
                    "estimate_independence": node["estimate_independence"],
                    "estimate_combo": node["estimate_combo"],
                    "selected_estimate": node["selected_estimate"],
                    "actual_rows": int(node["actual_rows"]),
                    "relative_error": node["relative_error"],
                    "scope": node["scope"],
                    "overlap_ratio": node["overlap_ratio"],
                    "right_multiplicity": node["right_multiplicity"],
                    "first_over_threshold": bool(node["first_over_threshold"]),
                    "reason": node["reason"],
                    "detail": json_loads(node["detail_json"], {}),
                }
            )
        join_order = conn.execute(
            """
            SELECT * FROM join_order_recommendations
            WHERE run_id = ? AND query_id = ?
            """,
            (run_id, query_eval["query_id"]),
        ).fetchone()
        index_rows = [
            {
                "index_name": row["index_name"],
                "table_name": row["table_name"],
                "columns": json_loads(row["columns_json"], []),
                "decision": row["decision"],
                "reason": row["reason"],
                "estimated_benefit": row["estimated_benefit"],
                "estimated_cost": row["estimated_cost"],
            }
            for row in conn.execute(
                """
                SELECT * FROM index_recommendations
                WHERE run_id = ? AND query_id = ?
                ORDER BY id
                """,
                (run_id, query_eval["query_id"]),
            )
        ]
        query_rows.append(
            {
                "query_id": query_eval["query_id"],
                "region_id": int(query_eval["region_id"]),
                "band_id": int(query_eval["band_id"]),
                "start_ts": query_eval["start_ts"],
                "end_ts": query_eval["end_ts"],
                "threshold": query_eval["deviation_threshold"],
                "fact_rows": int(query_eval["fact_rows"]),
                "independence_base": query_eval["independence_base"],
                "combo_base": query_eval["combo_base"],
                "selected_base": query_eval["selected_base"],
                "actual_rows": int(query_eval["actual_rows"]),
                "mean_node_error": query_eval["mean_node_error"],
                "max_node_error": query_eval["max_node_error"],
                "first_breach_node": query_eval["first_breach_node"],
                "scope": query_eval["estimate_scope"],
                "predicates": json_loads(query_eval["predicate_json"], {}),
                "nodes": nodes,
                "join_order": {
                    "recommended": json_loads(
                        join_order["recommended_order_json"], []
                    )
                    if join_order
                    else [],
                    "baseline": json_loads(join_order["baseline_order_json"], [])
                    if join_order
                    else [],
                    "costs": json_loads(join_order["costs_json"], {})
                    if join_order
                    else {},
                    "reason": join_order["reason"] if join_order else "",
                },
                "index_candidates": index_rows,
            }
        )
    return {
        "run_id": int(run["run_id"]),
        "version": run["version"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "query_count": int(run["query_count"]),
        "mean_abs_rel_error": run["mean_abs_rel_error"],
        "max_abs_rel_error": run["max_abs_rel_error"],
        "stat_fingerprint": run["stat_fingerprint"],
        "note": run["note"],
        "queries": query_rows,
    }


def compare_versions(
    conn: sqlite3.Connection,
    query_id: str,
    from_version: str,
    to_version: str,
) -> dict[str, Any]:
    source_run = _latest_completed_run(conn, from_version)
    target_run = _latest_completed_run(conn, to_version)
    if source_run is None:
        raise LookupError(f"no completed evaluation for {from_version}")
    if target_run is None:
        raise LookupError(f"no completed evaluation for {to_version}")
    source = conn.execute(
        """
        SELECT * FROM query_evaluations
        WHERE run_id = ? AND query_id = ?
        """,
        (source_run["run_id"], query_id),
    ).fetchone()
    target = conn.execute(
        """
        SELECT * FROM query_evaluations
        WHERE run_id = ? AND query_id = ?
        """,
        (target_run["run_id"], query_id),
    ).fetchone()
    if source is None or target is None:
        raise LookupError(f"query {query_id} is missing from one of the versions")

    def nodes_for(eval_id: int) -> list[dict[str, Any]]:
        return [
            {
                "node_order": int(row["node_order"]),
                "node_id": row["node_id"],
                "estimate_independence": row["estimate_independence"],
                "estimate_combo": row["estimate_combo"],
                "selected_estimate": row["selected_estimate"],
                "actual_rows": int(row["actual_rows"]),
                "relative_error": row["relative_error"],
                "scope": row["scope"],
                "reason": row["reason"],
            }
            for row in conn.execute(
                """
                SELECT * FROM node_estimates
                WHERE eval_id = ? ORDER BY node_order
                """,
                (eval_id,),
            )
        ]

    source_nodes = nodes_for(int(source["eval_id"]))
    target_nodes = nodes_for(int(target["eval_id"]))
    delta_by_order = {
        int(node["node_order"]): node for node in target_nodes
    }
    deltas = []
    for node in source_nodes:
        target_node = delta_by_order.get(int(node["node_order"]))
        if target_node is None:
            continue
        deltas.append(
            {
                "node_id": node["node_id"],
                "from_estimate": node["selected_estimate"],
                "to_estimate": target_node["selected_estimate"],
                "estimate_delta": target_node["selected_estimate"]
                - node["selected_estimate"],
                "from_error": node["relative_error"],
                "to_error": target_node["relative_error"],
                "error_delta": target_node["relative_error"]
                - node["relative_error"],
            }
        )
    return {
        "query_id": query_id,
        "from_version": from_version,
        "to_version": to_version,
        "from_run_id": int(source_run["run_id"]),
        "to_run_id": int(target_run["run_id"]),
        "from_estimate": source["selected_base"],
        "to_estimate": target["selected_base"],
        "actual_rows": int(target["actual_rows"]),
        "from_mean_error": source["mean_node_error"],
        "to_mean_error": target["mean_node_error"],
        "error_improved": target["mean_node_error"] < source["mean_node_error"],
        "node_deltas": deltas,
        "from_scope": source["estimate_scope"],
        "to_scope": target["estimate_scope"],
    }


def list_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "run_id": int(row["run_id"]),
            "version": row["version"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "query_count": int(row["query_count"]),
            "mean_abs_rel_error": row["mean_abs_rel_error"],
            "max_abs_rel_error": row["max_abs_rel_error"],
            "note": row["note"],
        }
        for row in conn.execute(
            "SELECT * FROM evaluation_runs ORDER BY run_id"
        )
    ]


def dump_snapshot_json(conn: sqlite3.Connection, run_id: int) -> str:
    return json.dumps(
        snapshot(conn, run_id=run_id),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
