"""Centralized SQLite reference execution.

The reference intentionally uses one full-table SQL statement (CTEs plus the
requested join). It never consumes partition hit tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .predicates import QuerySpec, compile_side_predicate


POINT_ROW_COLUMNS = (
    "reading_id",
    "point_id",
    "sampled_at",
    "residual_chlorine",
    "turbidity",
    "ph",
    "water_temp",
    "point_pipe_id",
    "point_location",
    "point_metric_types",
    "point_partition_id",
)


PIPE_ROW_COLUMNS = (
    "pipe_row_id",
    "pipe_id",
    "upstream_node",
    "downstream_node",
    "region",
    "diameter_mm",
    "material",
    "pipe_partition_id",
)


JOIN_ROW_COLUMNS = POINT_ROW_COLUMNS + PIPE_ROW_COLUMNS + ("join_null_source",)


def row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Canonical ordering shared by centralized and pushdown execution."""

    return (
        row.get("point_id") or "",
        row.get("sampled_at") or "",
        row.get("pipe_id") or "",
        row.get("reading_id") if row.get("reading_id") is not None else -1,
        row.get("pipe_row_id") if row.get("pipe_row_id") is not None else -1,
        row.get("join_null_source") or "",
    )


def sort_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=row_sort_key)


def _point_cte(query: QuerySpec) -> tuple[str, list[Any]]:
    point_set = compile_side_predicate(query.point_predicates)
    return (
        f"""
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
        WHERE {point_set.sql()}
          AND {query.reading_predicates.sql()}
        """,
        [],
    )


def _pipe_cte(query: QuerySpec) -> tuple[str, list[Any]]:
    pipe_set = compile_side_predicate(query.pipe_predicates)
    return (
        f"""
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
        WHERE {pipe_set.sql()}
        """,
        [],
    )


def _join_select_list() -> str:
    point_columns = ", ".join(f"fp.{column} AS {column}" for column in POINT_ROW_COLUMNS)
    pipe_columns = ", ".join(f"pp.{column} AS {column}" for column in PIPE_ROW_COLUMNS)
    marker = """
        CASE
            WHEN fp.reading_id IS NOT NULL AND pp.pipe_row_id IS NOT NULL THEN 'none'
            WHEN pp.pipe_row_id IS NULL THEN
                CASE
                    WHEN fp.point_pipe_id IS NULL THEN 'point_key_null'
                    ELSE 'missing_pipe_reference'
                END
            ELSE
                CASE
                    WHEN pp.pipe_id IS NULL THEN 'pipe_key_null'
                    ELSE 'missing_point_reference'
                END
        END AS join_null_source
    """
    return f"{point_columns}, {pipe_columns}, {marker}"


def build_central_sql(query: QuerySpec) -> str:
    point_cte, _ = _point_cte(query)
    pipe_cte, _ = _pipe_cte(query)
    select_list = _join_select_list()
    if query.join_type == "inner":
        relation_sql = """
        SELECT {select_list}
        FROM filtered_points AS fp
        INNER JOIN filtered_pipes AS pp ON fp.point_pipe_id = pp.pipe_id
        """
    elif query.join_type == "left":
        relation_sql = """
        SELECT {select_list}
        FROM filtered_points AS fp
        LEFT JOIN filtered_pipes AS pp ON fp.point_pipe_id = pp.pipe_id
        """
    elif query.join_type == "right":
        relation_sql = """
        SELECT {select_list}
        FROM filtered_pipes AS pp
        LEFT JOIN filtered_points AS fp ON fp.point_pipe_id = pp.pipe_id
        """
    elif query.join_type == "full":
        relation_sql = """
        SELECT {select_list}
        FROM filtered_points AS fp
        LEFT JOIN filtered_pipes AS pp ON fp.point_pipe_id = pp.pipe_id
        UNION ALL
        SELECT {select_list}
        FROM filtered_pipes AS pp
        LEFT JOIN filtered_points AS fp ON fp.point_pipe_id = pp.pipe_id
        WHERE fp.reading_id IS NULL
        """
    else:
        raise ValueError(f"unsupported join type: {query.join_type}")
    relation_sql = relation_sql.format(select_list=select_list)
    return f"""
    WITH
    filtered_points AS ({point_cte}),
    filtered_pipes AS ({pipe_cte})
    {relation_sql}
    """


def execute_central_reference(
    conn: sqlite3.Connection, query: QuerySpec
) -> list[dict[str, Any]]:
    sql = build_central_sql(query)
    rows = [dict(row) for row in conn.execute(sql).fetchall()]
    return sort_rows(rows)


def join_params(query: QuerySpec) -> dict[str, Any]:
    """Descriptive join information persisted with each query plan."""

    return {
        "left_alias": "p (monitoring_points)",
        "right_alias": "pi (pipes)",
        "condition": "p.pipe_id = pi.pipe_id",
        "join_type": query.join_type,
        "null_equals_null": False,
        "inner_null_rule": "either key NULL -> no match",
        "outer_key_null_rule": (
            "preserve unmatched row; marker point_key_null or pipe_key_null"
        ),
        "missing_reference_rule": (
            "non-NULL key without counterpart -> missing_*_reference marker"
        ),
    }

