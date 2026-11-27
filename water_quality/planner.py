"""Query decomposition into pipe partitions, point partitions and join tasks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import canonical_json
from .joins import NO_PIPE_PARTITION, NO_POINT_PARTITION, all_join_task_keys, join_task_key
from .predicates import QuerySpec, compile_side_predicate
from .reference import join_params
from .rules import PartitionRules


POINT_SELECT_SQL = """
SELECT
    r.reading_id AS reading_id,
    r.point_id AS point_id,
    r.sampled_at AS sampled_at,
    r.residual_chlorine AS residual_chlorine,
    r.turbidity AS turbidity,
    r.ph AS ph,
    r.water_temp AS water_temp,
    p.pipe_id AS point_pipe_id,
    p.location AS point_location,
    p.metric_types AS point_metric_types,
    p.partition_id AS point_partition_id
FROM readings AS r
INNER JOIN monitoring_points AS p ON p.point_id = r.point_id
"""


PIPE_SELECT_SQL = """
SELECT
    pi.row_id AS pipe_row_id,
    pi.pipe_id AS pipe_id,
    pi.upstream_node AS upstream_node,
    pi.downstream_node AS downstream_node,
    pi.region AS region,
    pi.diameter_mm AS diameter_mm,
    pi.material AS material,
    pi.partition_id AS pipe_partition_id
FROM pipes AS pi
"""


@dataclass(frozen=True)
class PlanPartition:
    part_type: str
    partition_id: str
    predicate_sql: str
    execution_sql: str


@dataclass(frozen=True)
class JoinTaskPlan:
    join_task_key: str
    pipe_partition_id: str
    point_partition_id: str


@dataclass(frozen=True)
class ExecutionPlan:
    query: QuerySpec
    rules_version: str
    point_partitions: tuple[PlanPartition, ...]
    pipe_partitions: tuple[PlanPartition, ...]
    join_tasks: tuple[JoinTaskPlan, ...]
    remaining_steps: tuple[str, ...]
    plan_json: str


def _partition_ids(conn: sqlite3.Connection, table: str) -> list[str]:
    if table not in {"pipes", "monitoring_points"}:
        raise ValueError(table)
    return [
        row["partition_id"]
        for row in conn.execute(
            f"SELECT DISTINCT partition_id FROM {table} ORDER BY partition_id"
        )
    ]


def build_execution_plan(
    conn: sqlite3.Connection,
    query: QuerySpec,
    rules: PartitionRules,
) -> ExecutionPlan:
    point_set = compile_side_predicate(query.point_predicates)
    pipe_set = compile_side_predicate(query.pipe_predicates)

    point_partitions: list[PlanPartition] = []
    for partition_id in _partition_ids(conn, "monitoring_points"):
        predicate_sql = (
            f"(p.partition_id = {sql_string(partition_id)}) "
            f"AND {point_set.sql()} AND {query.reading_predicates.sql()}"
        )
        execution_sql = (
            POINT_SELECT_SQL
            + " WHERE (p.partition_id = ?) AND "
            + point_set.sql()
            + " AND "
            + query.reading_predicates.sql()
            + " ORDER BY r.point_id, r.sampled_at, r.reading_id"
        )
        point_partitions.append(
            PlanPartition(
                part_type="point_filter",
                partition_id=partition_id,
                predicate_sql=predicate_sql,
                execution_sql=execution_sql,
            )
        )

    pipe_partitions: list[PlanPartition] = []
    for partition_id in _partition_ids(conn, "pipes"):
        predicate_sql = (
            f"(pi.partition_id = {sql_string(partition_id)}) AND {pipe_set.sql()}"
        )
        execution_sql = (
            PIPE_SELECT_SQL
            + " WHERE (pi.partition_id = ?) AND "
            + pipe_set.sql()
            + " ORDER BY pi.row_id"
        )
        pipe_partitions.append(
            PlanPartition(
                part_type="pipe_filter",
                partition_id=partition_id,
                predicate_sql=predicate_sql,
                execution_sql=execution_sql,
            )
        )

    point_ids = [item.partition_id for item in point_partitions]
    pipe_ids = [item.partition_id for item in pipe_partitions]
    join_tasks = tuple(
        JoinTaskPlan(*task)
        for task in all_join_task_keys(point_ids, pipe_ids, query.join_type)
    )
    remaining_steps = (
        "merge join-task rows from all partitions",
        "sort by (point_id, sampled_at, pipe_id, reading_id, pipe_row_id, marker)",
        "compare hit set, join-null markers and exact row order with centralized SQL",
        "emit violation and manual-review audit lists from the original readings",
    )
    plan_payload = {
        "query_id": query.query_id,
        "description": query.description,
        "query_sql": {
            "pipe_predicate": pipe_set.sql(),
            "point_predicate": point_set.sql(),
            "reading_predicate": query.reading_predicates.sql(),
        },
        "query_text": {
            "pipe_predicate": pipe_set.text(),
            "point_predicate": point_set.text(),
            "reading_predicate": query.reading_predicates.text(),
        },
        "predicates": {
            "pipe": [item.to_dict() for item in query.pipe_predicates],
            "point": [item.to_dict() for item in query.point_predicates],
            "reading": [
                group_dict
                for group_dict in (
                    [item.to_dict() for item in group.predicates]
                    for group in query.reading_predicates.groups
                )
            ],
        },
        "null_policy": {
            "comparison": "SQL/Kleene three-valued logic; only TRUE is retained",
            "unknown": "UNKNOWN is neither TRUE nor zero",
            "sql_guard": "IS NOT NULL guards every threshold comparison",
        },
        "join": join_params(query),
        "join_tasks": [
            {
                "join_task_key": item.join_task_key,
                "pipe_partition_id": item.pipe_partition_id,
                "point_partition_id": item.point_partition_id,
                "condition": "point_pipe_id = pipe_id",
                "null_equals_null": False,
            }
            for item in join_tasks
        ],
        "remaining_steps": list(remaining_steps),
        "partition_rules_version": rules.version,
    }
    return ExecutionPlan(
        query=query,
        rules_version=rules.version,
        point_partitions=tuple(point_partitions),
        pipe_partitions=tuple(pipe_partitions),
        join_tasks=join_tasks,
        remaining_steps=remaining_steps,
        plan_json=canonical_json(plan_payload),
    )


def persist_plan(
    conn: sqlite3.Connection,
    run_id: str,
    plan: ExecutionPlan,
    *,
    point_result_counts: dict[str, int] | None = None,
    pipe_result_counts: dict[str, int] | None = None,
    join_result_counts: dict[str, int] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO query_plans(
            run_id, query_id, query_json, plan_json, join_type, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, query_id) DO UPDATE SET
            query_json = excluded.query_json,
            plan_json = excluded.plan_json,
            join_type = excluded.join_type,
            created_at = excluded.created_at
        """,
        (
            run_id,
            plan.query.query_id,
            canonical_json(plan.query.raw),
            plan.plan_json,
            plan.query.join_type,
            now,
        ),
    )
    point_result_counts = point_result_counts or {}
    pipe_result_counts = pipe_result_counts or {}
    join_result_counts = join_result_counts or {}
    for partition in plan.point_partitions:
        conn.execute(
            """
            INSERT OR REPLACE INTO query_plan_partitions(
                run_id, query_id, part_type, partition_id,
                predicate_sql, execution_sql, result_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                plan.query.query_id,
                partition.part_type,
                partition.partition_id,
                partition.predicate_sql,
                partition.execution_sql,
                point_result_counts.get(partition.partition_id, 0),
            ),
        )
    for partition in plan.pipe_partitions:
        conn.execute(
            """
            INSERT OR REPLACE INTO query_plan_partitions(
                run_id, query_id, part_type, partition_id,
                predicate_sql, execution_sql, result_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                plan.query.query_id,
                partition.part_type,
                partition.partition_id,
                partition.predicate_sql,
                partition.execution_sql,
                pipe_result_counts.get(partition.partition_id, 0),
            ),
        )
    for task in plan.join_tasks:
        self_nulls = (
            "fp.point_pipe_id IS NOT NULL AND pp.pipe_id IS NOT NULL "
            "AND fp.point_pipe_id = pp.pipe_id"
        )
        marker_sql = (
            "CASE WHEN both sides present THEN 'none' "
            "WHEN point side only THEN point_key_null|missing_pipe_reference "
            "WHEN pipe side only THEN pipe_key_null|missing_point_reference END"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO query_plan_partitions(
                run_id, query_id, part_type, partition_id,
                predicate_sql, execution_sql, result_count
            ) VALUES (?, ?, 'join_task', ?, ?, ?, ?)
            """,
            (
                run_id,
                plan.query.query_id,
                task.join_task_key,
                self_nulls,
                f"{plan.query.join_type.upper()} join task "
                f"pipe={task.pipe_partition_id}, point={task.point_partition_id}; "
                f"NULL != NULL; markers: {marker_sql}",
                join_result_counts.get(task.join_task_key, 0),
            ),
        )


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_queries(conn: sqlite3.Connection) -> list[QuerySpec]:
    return [
        QuerySpec.from_json(row["request_json"])
        for row in conn.execute(
            "SELECT request_json FROM query_requests ORDER BY query_id"
        )
    ]

