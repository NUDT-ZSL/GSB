from __future__ import annotations

import sqlite3
from typing import Any

from .db import json_dumps, json_loads
from .estimation import relative_error
from .evaluation import evaluate_workload
from .generator import load_queries
from .statistics import build_statistics, utc_now, validate_summary


def _version_number(conn: sqlite3.Connection, base: str = "v2_updated") -> str:
    candidate = base
    suffix = 1
    while conn.execute(
        "SELECT 1 FROM stat_versions WHERE version = ?", (candidate,)
    ).fetchone():
        suffix += 1
        candidate = f"v{suffix}_updated"
    return candidate


def generate_update_plan(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise LookupError(f"evaluation run not found: {run_id}")
    version = str(run["version"])
    combo_stat = conn.execute(
        """
        SELECT source, sample_size FROM stat_combo_histograms
        WHERE version = ?
        """,
        (version,),
    ).fetchone()
    total_logs = int(
        conn.execute(
            """
            SELECT row_count FROM stat_row_counts
            WHERE version = ? AND table_name = 'patrol_logs'
            """,
            (version,),
        ).fetchone()[0]
    )
    query_rows = conn.execute(
        """
        SELECT query_id, predicate_json FROM query_evaluations
        WHERE run_id = ? ORDER BY query_id
        """,
        (run_id,),
    ).fetchall()
    query_ids = [str(row["query_id"]) for row in query_rows]
    query_specs = {query.query_id: query for query in load_queries(conn, query_ids)}

    column_errors: dict[str, list[float]] = {
        "region_id": [],
        "band_id": [],
        "logged_at": [],
    }
    combo_errors: list[float] = []
    for row in query_rows:
        query = query_specs[str(row["query_id"])]
        detail = json_loads(row["predicate_json"], {})
        actual_region = int(
            conn.execute(
                "SELECT COUNT(*) FROM patrol_logs WHERE region_id = ?",
                (query.region_id,),
            ).fetchone()[0]
        )
        actual_band = int(
            conn.execute(
                "SELECT COUNT(*) FROM patrol_logs WHERE band_id = ?",
                (query.band_id,),
            ).fetchone()[0]
        )
        actual_time = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM patrol_logs
                WHERE logged_at >= ? AND logged_at <= ?
                """,
                (query.start_ts, query.end_ts),
            ).fetchone()[0]
        )
        column_errors["region_id"].append(
            relative_error(
                float(detail["region"]["final_selectivity"]) * total_logs,
                actual_region,
            )
        )
        column_errors["band_id"].append(
            relative_error(
                float(detail["band"]["final_selectivity"]) * total_logs,
                actual_band,
            )
        )
        column_errors["logged_at"].append(
            relative_error(
                float(detail["time"]["final_selectivity"]) * total_logs,
                actual_time,
            )
        )
        actual_combo = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM patrol_logs
                WHERE region_id = ? AND band_id = ?
                  AND logged_at >= ? AND logged_at <= ?
                """,
                (query.region_id, query.band_id, query.start_ts, query.end_ts),
            ).fetchone()[0]
        )
        combo_errors.append(
            relative_error(
                float(detail["combo_selectivity"]) * total_logs,
                actual_combo,
            )
        )

    column_summary = {
        column: {
            "mean_relative_error": sum(errors) / len(errors) if errors else 0.0,
            "max_relative_error": max(errors) if errors else 0.0,
        }
        for column, errors in column_errors.items()
    }
    combo_mean = sum(combo_errors) / len(combo_errors) if combo_errors else 0.0
    combo_max = max(combo_errors) if combo_errors else 0.0
    combo_source = str(combo_stat["source"]) if combo_stat else "missing"
    combo_sample_size = int(combo_stat["sample_size"]) if combo_stat else 0

    suggestions: list[dict[str, Any]] = []
    for column, summary in column_summary.items():
        if summary["mean_relative_error"] > 0.03 or summary["max_relative_error"] > 0.12:
            priority = (
                "high"
                if summary["max_relative_error"] > 0.20
                or summary["mean_relative_error"] > 0.08
                else "medium"
            )
            suggestions.append(
                {
                    "kind": "rebuild_histogram",
                    "table_name": "patrol_logs",
                    "column_name": column,
                    "reason": (
                        f"{column} 单谓词平均相对偏差 "
                        f"{summary['mean_relative_error']:.3f}，最大 "
                        f"{summary['max_relative_error']:.3f}；"
                        "当前桶边界/采样混合不能稳定解释该谓词"
                    ),
                    "priority": priority,
                    "action": {
                        "action": "rescan_and_rebuild_equal_depth_histogram",
                        "table": "patrol_logs",
                        "column": column,
                        "target_buckets": 8,
                    },
                }
            )

    if (
        combo_source != "full"
        or combo_mean > 0.03
        or combo_max > 0.12
    ):
        suggestions.append(
            {
                "kind": "build_combo_histogram",
                "table_name": "patrol_logs",
                "column_name": "region_id+band_id",
                "reason": (
                    f"区域频段组合当前来源={combo_source}，采样行数="
                    f"{combo_sample_size}，组合选择率平均偏差 {combo_mean:.3f}、"
                    f"最大 {combo_max:.3f}；独立性假设或小样本放大了"
                    "偏远区域与热点区域的联合频率误差"
                ),
                "priority": "high" if combo_mean > 0.10 or combo_source != "full" else "medium",
                "action": {
                    "action": "build_full_region_band_joint_histogram",
                    "table": "patrol_logs",
                    "columns": ["region_id", "band_id"],
                    "source": "full",
                },
            }
        )

    issues = validate_summary(conn, version)
    if issues:
        suggestions.append(
            {
                "kind": "repair_summary_metadata",
                "table_name": "<summary>",
                "column_name": None,
                "reason": f"摘要校验发现 {len(issues)} 项失效，需要随重算一并修复",
                "priority": "high",
                "action": {"action": "rebuild_invalid_summary_entries"},
            }
        )

    summary = {
        "source_version": version,
        "source_run_id": run_id,
        "column_errors": column_summary,
        "combo": {
            "source": combo_source,
            "sample_size": combo_sample_size,
            "mean_relative_error": combo_mean,
            "max_relative_error": combo_max,
        },
        "suggestion_count": len(suggestions),
        "validation_issue_count": len(issues),
    }
    cursor = conn.execute(
        """
        INSERT INTO update_plans
            (source_version, created_at, summary_json, source_run_id)
        VALUES (?, ?, ?, ?)
        """,
        (version, utc_now(), json_dumps(summary), run_id),
    )
    plan_id = int(cursor.lastrowid)
    for suggestion in suggestions:
        conn.execute(
            """
            INSERT INTO advisor_suggestions
                (run_id, kind, table_name, column_name, reason, priority,
                 action_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                suggestion["kind"],
                suggestion["table_name"],
                suggestion["column_name"],
                suggestion["reason"],
                suggestion["priority"],
                json_dumps(suggestion["action"]),
            ),
        )
    conn.commit()
    return {
        "plan_id": plan_id,
        "source_version": version,
        "source_run_id": run_id,
        "summary": summary,
        "suggestions": suggestions,
    }


def apply_update_plan(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    target_version: str | None = None,
    sample_size: int = 1024,
) -> dict[str, Any]:
    plan = conn.execute(
        "SELECT * FROM update_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if plan is None:
        raise LookupError(f"update plan not found: {plan_id}")
    if plan["applied_at"] is not None:
        raise ValueError(f"update plan already applied: {plan_id}")
    source_version = str(plan["source_version"])
    source_run_id = int(plan["source_run_id"])
    target = target_version or _version_number(conn)
    source_run = conn.execute(
        "SELECT * FROM evaluation_runs WHERE run_id = ?", (source_run_id,)
    ).fetchone()
    mean_before = float(source_run["mean_abs_rel_error"])

    build_statistics(
        conn,
        target,
        sample_size=sample_size,
        combo_sample_size=sample_size,
        combo_source="full",
        note=f"由更新计划 {plan_id} 触发：重算偏差列直方图并全量构建区域频段组合统计",
    )
    conn.execute(
        """
        UPDATE advisor_suggestions
        SET applied_in_version = ?
        WHERE run_id = ?
        """,
        (target, source_run_id),
    )
    target_run = evaluate_workload(
        conn,
        target,
        note=f"应用摘要更新计划 {plan_id} 后的同批查询复算",
    )
    mean_after = float(target_run["mean_abs_rel_error"])
    converged = mean_after <= mean_before * 0.95 or mean_after <= 0.05
    conn.execute(
        """
        UPDATE update_plans
        SET target_version = ?, applied_at = ?, target_run_id = ?,
            mean_before = ?, mean_after = ?, converged = ?
        WHERE plan_id = ?
        """,
        (
            target,
            utc_now(),
            int(target_run["run_id"]),
            mean_before,
            mean_after,
            1 if converged else 0,
            plan_id,
        ),
    )
    conn.commit()
    suggestions = [
        {
            "suggestion_id": int(row["suggestion_id"]),
            "kind": row["kind"],
            "table_name": row["table_name"],
            "column_name": row["column_name"],
            "priority": row["priority"],
            "reason": row["reason"],
            "applied_in_version": row["applied_in_version"],
        }
        for row in conn.execute(
            "SELECT * FROM advisor_suggestions WHERE run_id = ? ORDER BY suggestion_id",
            (source_run_id,),
        )
    ]
    return {
        "plan_id": plan_id,
        "source_version": source_version,
        "target_version": target,
        "source_run_id": source_run_id,
        "target_run_id": int(target_run["run_id"]),
        "mean_before": mean_before,
        "mean_after": mean_after,
        "improvement_ratio": (mean_before - mean_after) / mean_before
        if mean_before
        else 0.0,
        "converged": converged,
        "suggestions": suggestions,
        "target_run": target_run,
    }


def list_advisor_suggestions(
    conn: sqlite3.Connection, run_id: int
) -> list[dict[str, Any]]:
    return [
        {
            "suggestion_id": int(row["suggestion_id"]),
            "kind": row["kind"],
            "table_name": row["table_name"],
            "column_name": row["column_name"],
            "priority": row["priority"],
            "reason": row["reason"],
            "action": json_loads(row["action_json"], {}),
            "applied_in_version": row["applied_in_version"],
        }
        for row in conn.execute(
            """
            SELECT * FROM advisor_suggestions
            WHERE run_id = ? ORDER BY suggestion_id
            """,
            (run_id,),
        )
    ]
