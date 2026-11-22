from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import EngineError
from .models import (
    BatchSpec,
    ColumnSpec,
    ColumnStats,
    JoinPredicate,
    PlanSpec,
    ReoptConfig,
    SampleSpec,
    Shape,
    SkewSpec,
    StatsSnapshot,
    SUPPORTED_ALGORITHMS,
    SUPPORTED_TYPES,
    TableSpec,
    TableStats,
)


def _bad(code: str, message: str, **details: Any) -> EngineError:
    return EngineError(code=code, message=message, details=details)


def _require_object(value: Any, code: str, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad(code, f"{where} must be an object", where=where)
    return value


def _require_list(value: Any, code: str, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise _bad(code, f"{where} must be an array", where=where)
    return value


def _require_str(value: Any, code: str, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(code, f"{where} must be a non-empty string", where=where)
    return value


def _require_int(value: Any, code: str, where: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(code, f"{where} must be an integer", where=where)
    if minimum is not None and value < minimum:
        raise _bad(code, f"{where} must be >= {minimum}", where=where)
    return value


def _require_number(value: Any, code: str, where: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(code, f"{where} must be a number", where=where)
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise _bad(code, f"{where} must be finite", where=where)
    if minimum is not None and result < minimum:
        raise _bad(code, f"{where} must be >= {minimum}", where=where)
    return result


def _parse_column(raw: Any, table_name: str) -> ColumnSpec:
    item = _require_object(raw, "E_BAD_TABLE", f"tables[{table_name}].columns[]")
    name = _require_str(item.get("name"), "E_BAD_TABLE", f"tables[{table_name}].columns[].name")
    column_type = _require_str(item.get("type"), "E_BAD_TABLE", f"tables[{table_name}].{name}.type")
    if column_type not in SUPPORTED_TYPES:
        raise _bad(
            "E_BAD_TABLE",
            f"unsupported column type for {table_name}.{name}",
            table=table_name,
            column=name,
            supported=sorted(SUPPORTED_TYPES),
        )
    width = _require_int(
        item.get("width_bytes"), "E_BAD_TABLE", f"tables[{table_name}].{name}.width_bytes", minimum=1
    )
    ndv = _require_int(item.get("ndv"), "E_BAD_TABLE", f"tables[{table_name}].{name}.ndv", minimum=1)
    join_key = item.get("join_key", False)
    if not isinstance(join_key, bool):
        raise _bad("E_BAD_TABLE", f"join_key for {table_name}.{name} must be boolean")
    return ColumnSpec(name=name, type=column_type, width_bytes=width, ndv=ndv, join_key=join_key)


def _parse_table(raw: Any) -> TableSpec:
    item = _require_object(raw, "E_BAD_TABLE", "tables[]")
    name = _require_str(item.get("name"), "E_BAD_TABLE", "tables[].name")
    row_count = _require_int(item.get("row_count"), "E_BAD_TABLE", f"table {name}.row_count", minimum=1)
    columns_raw = _require_list(item.get("columns"), "E_BAD_TABLE", f"table {name}.columns")
    if not columns_raw:
        raise _bad("E_BAD_TABLE", f"table {name} must have at least one column", table=name)
    columns = tuple(_parse_column(raw_column, name) for raw_column in columns_raw)
    names = [column.name for column in columns]
    if len(names) != len(set(names)):
        raise _bad("E_BAD_TABLE", f"duplicate column in table {name}", table=name)
    for column in columns:
        if column.ndv > row_count:
            raise _bad(
                "E_BAD_TABLE",
                f"NDV for {name}.{column.name} cannot exceed row_count",
                table=name,
                column=column.name,
                ndv=column.ndv,
                row_count=row_count,
            )

    skew_raw = item.get("skew", {"enabled": False})
    skew_item = _require_object(skew_raw, "E_BAD_TABLE", f"table {name}.skew")
    enabled = skew_item.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _bad("E_BAD_TABLE", f"table {name}.skew.enabled must be boolean")
    skew = SkewSpec(enabled=enabled)
    if enabled:
        column_name = _require_str(skew_item.get("column"), "E_BAD_TABLE", f"table {name}.skew.column")
        column_map = {column.name: column for column in columns}
        if column_name not in column_map:
            raise _bad("E_BAD_TABLE", f"skew column {column_name} not found in table {name}")
        column = column_map[column_name]
        if column.type not in {"int64", "string"}:
            raise _bad("E_BAD_TABLE", f"skew column {name}.{column_name} must be int64 or string")
        hot_fraction = _require_number(
            skew_item.get("hot_fraction"), "E_BAD_TABLE", f"table {name}.skew.hot_fraction", minimum=0.0
        )
        if not 0.0 < hot_fraction < 1.0:
            raise _bad("E_BAD_TABLE", f"skew hot_fraction for {name} must be in (0, 1)")
        hot_key = skew_item.get("hot_key")
        if column.type == "int64":
            hot_key = _require_int(hot_key, "E_BAD_TABLE", f"table {name}.skew.hot_key", minimum=0)
            if hot_key >= column.ndv:
                raise _bad("E_BAD_TABLE", f"hot_key for {name}.{column_name} must be below NDV")
        else:
            hot_key = _require_str(hot_key, "E_BAD_TABLE", f"table {name}.skew.hot_key")
        skew = SkewSpec(enabled=True, column=column_name, hot_key=hot_key, hot_fraction=hot_fraction)
    return TableSpec(name=name, row_count=row_count, columns=columns, skew=skew)


def _parse_predicate(raw: Any, table_map: dict[str, TableSpec]) -> JoinPredicate:
    item = _require_object(raw, "E_BAD_PLAN", "plan.joins[]")
    left_table = _require_str(item.get("left_table"), "E_BAD_PLAN", "plan.joins[].left_table")
    right_table = _require_str(item.get("right_table"), "E_BAD_PLAN", "plan.joins[].right_table")
    left_column = _require_str(item.get("left_column"), "E_BAD_PLAN", "plan.joins[].left_column")
    right_column = _require_str(item.get("right_column"), "E_BAD_PLAN", "plan.joins[].right_column")
    if left_table == right_table:
        raise _bad("E_BAD_PLAN", "self joins are not supported", table=left_table)
    if left_table not in table_map or right_table not in table_map:
        raise _bad(
            "E_BAD_PLAN",
            "join predicate references an unknown table",
            left_table=left_table,
            right_table=right_table,
        )
    left_spec = table_map[left_table].column_map.get(left_column)
    right_spec = table_map[right_table].column_map.get(right_column)
    if left_spec is None or right_spec is None:
        raise _bad(
            "E_BAD_PLAN",
            "join predicate references an unknown column",
            left=f"{left_table}.{left_column}",
            right=f"{right_table}.{right_column}",
        )
    if not left_spec.join_key or not right_spec.join_key:
        raise _bad(
            "E_BAD_PLAN",
            "join predicate columns must be declared as join keys",
            left=f"{left_table}.{left_column}",
            right=f"{right_table}.{right_column}",
        )
    if left_spec.type != right_spec.type:
        raise _bad(
            "E_BAD_PLAN",
            "join predicate columns have incompatible types",
            left=f"{left_table}.{left_column}:{left_spec.type}",
            right=f"{right_table}.{right_column}:{right_spec.type}",
        )
    return JoinPredicate(left_table, right_table, left_column, right_column)


def _parse_shape(raw: Any, table_names: set[str], code: str) -> Shape:
    item = _require_object(raw, code, "plan.initial_shape")
    order_raw = _require_list(item.get("join_order"), code, "plan.initial_shape.join_order")
    join_order = tuple(_require_str(name, code, "plan.initial_shape.join_order[]") for name in order_raw)
    if len(join_order) != len(table_names) or set(join_order) != table_names:
        raise _bad(
            code,
            "initial join order must contain every table exactly once",
            expected=sorted(table_names),
            actual=list(join_order),
        )
    algorithms_raw = _require_list(item.get("algorithms"), code, "plan.initial_shape.algorithms")
    algorithms = tuple(_require_str(algo, code, "plan.initial_shape.algorithms[]") for algo in algorithms_raw)
    if len(algorithms) != len(join_order) - 1:
        raise _bad(code, "initial algorithms length must be len(join_order) - 1")
    invalid = sorted(set(algorithms) - SUPPORTED_ALGORITHMS)
    if invalid:
        raise _bad(code, "unsupported join algorithm", algorithms=invalid)
    return Shape(join_order=join_order, algorithms=algorithms)


def _prefix_connected(shape: Shape, predicates: tuple[JoinPredicate, ...]) -> bool:
    joined = {shape.join_order[0]}
    for table in shape.join_order[1:]:
        connected = any(
            (predicate.left_table == table and predicate.right_table in joined)
            or (predicate.right_table == table and predicate.left_table in joined)
            for predicate in predicates
        )
        if not connected:
            return False
        joined.add(table)
    return True


def _parse_plan(
    raw: Any,
    table_map: dict[str, TableSpec],
) -> tuple[PlanSpec, dict[int, BatchSpec]]:
    item = _require_object(raw, "E_BAD_PLAN", "plan")
    query_id = _require_str(item.get("query_id"), "E_BAD_PLAN", "plan.query_id")
    shape = _parse_shape(item.get("initial_shape"), set(table_map), "E_BAD_PLAN")
    predicates_raw = _require_list(item.get("joins"), "E_BAD_PLAN", "plan.joins")
    if len(table_map) > 1 and not predicates_raw:
        raise _bad("E_BAD_PLAN", "multi-table plans require at least one join predicate")
    predicates = tuple(_parse_predicate(raw_predicate, table_map) for raw_predicate in predicates_raw)
    edge_keys = [predicate.edge_key for predicate in predicates]
    if len(edge_keys) != len(set(edge_keys)):
        raise _bad("E_BAD_PLAN", "duplicate join predicate")
    if not _prefix_connected(shape, predicates):
        raise _bad("E_BAD_PLAN", "initial join order is not connected by predicates")

    batches_raw = _require_list(item.get("batches"), "E_BAD_PLAN", "plan.batches")
    if not batches_raw:
        raise _bad("E_BAD_PLAN", "plan.batches must not be empty")
    batches: list[BatchSpec] = []
    arrivals: set[int] = set()
    for raw_batch in batches_raw:
        batch_item = _require_object(raw_batch, "E_BAD_PLAN", "plan.batches[]")
        arrival = _require_int(batch_item.get("arrival"), "E_BAD_PLAN", "batch.arrival", minimum=1)
        table_name = _require_str(batch_item.get("table"), "E_BAD_PLAN", "batch.table")
        row_count = _require_int(batch_item.get("row_count"), "E_BAD_PLAN", "batch.row_count", minimum=1)
        if arrival in arrivals:
            raise _bad("E_BAD_PLAN", f"duplicate batch arrival {arrival}")
        if table_name not in table_map:
            raise _bad("E_BAD_PLAN", f"batch {arrival} references unknown table {table_name}")
        arrivals.add(arrival)
        batches.append(BatchSpec(arrival=arrival, table=table_name, row_count=row_count))
    batches.sort(key=lambda batch: batch.arrival)

    totals = {name: 0 for name in table_map}
    for batch in batches:
        totals[batch.table] += batch.row_count
    for table_name, total in totals.items():
        if total != table_map[table_name].row_count:
            raise _bad(
                "E_BAD_PLAN",
                f"batch row counts for {table_name} do not match table.row_count",
                table=table_name,
                declared=table_map[table_name].row_count,
                streamed=total,
            )
    return PlanSpec(query_id=query_id, initial_shape=shape, joins=predicates, batches=tuple(batches)), {
        batch.arrival: batch for batch in batches
    }


def _parse_stats(raw: Any, table_map: dict[str, TableSpec], predicates: tuple[JoinPredicate, ...]) -> StatsSnapshot:
    item = _require_object(raw, "E_BAD_STATS", "statistics")
    tables_raw = _require_object(item.get("tables"), "E_BAD_STATS", "statistics.tables")
    if set(tables_raw) != set(table_map):
        raise _bad(
            "E_BAD_STATS",
            "statistics must contain exactly the tables in the sample",
            expected=sorted(table_map),
            actual=sorted(tables_raw),
        )
    stats_tables: dict[str, TableStats] = {}
    for table_name, raw_table in tables_raw.items():
        table_item = _require_object(raw_table, "E_BAD_STATS", f"statistics.tables[{table_name}]")
        row_count = _require_int(
            table_item.get("row_count"), "E_BAD_STATS", f"stats {table_name}.row_count", minimum=1
        )
        columns_raw = _require_object(table_item.get("columns"), "E_BAD_STATS", f"stats {table_name}.columns")
        expected_columns = set(table_map[table_name].column_map)
        if set(columns_raw) != expected_columns:
            raise _bad(
                "E_BAD_STATS",
                f"statistics columns for {table_name} do not match table description",
                expected=sorted(expected_columns),
                actual=sorted(columns_raw),
            )
        column_stats: dict[str, ColumnStats] = {}
        for column_name, raw_column in columns_raw.items():
            column_item = _require_object(raw_column, "E_BAD_STATS", f"stats {table_name}.{column_name}")
            ndv = _require_int(
                column_item.get("ndv"),
                "E_BAD_STATS",
                f"stats {table_name}.{column_name}.ndv",
                minimum=1,
            )
            if ndv > row_count:
                raise _bad(
                    "E_BAD_STATS",
                    f"stats NDV for {table_name}.{column_name} exceeds stats row_count",
                    table=table_name,
                    column=column_name,
                )
            selectivity = _require_number(
                column_item.get("selectivity"),
                "E_BAD_STATS",
                f"stats {table_name}.{column_name}.selectivity",
            )
            if not 0.0 < selectivity < 1.0:
                raise _bad(
                    "E_BAD_STATS",
                    f"stats selectivity for {table_name}.{column_name} must be in (0, 1)",
                )
            skew_raw = column_item.get("skew_fraction")
            skew_fraction = None
            if skew_raw is not None:
                skew_fraction = _require_number(
                    skew_raw, "E_BAD_STATS", f"stats {table_name}.{column_name}.skew_fraction"
                )
                if not 0.0 <= skew_fraction < 1.0:
                    raise _bad(
                        "E_BAD_STATS",
                        f"stats skew_fraction for {table_name}.{column_name} must be in [0, 1)",
                    )
            column_stats[column_name] = ColumnStats(
                ndv=ndv, selectivity=selectivity, skew_fraction=skew_fraction
            )
        stats_tables[table_name] = TableStats(row_count=row_count, columns=column_stats)

    selectivities_raw = _require_list(item.get("join_selectivities"), "E_BAD_STATS", "statistics.join_selectivities")
    selectivities: dict[tuple[str, str, str, str], float] = {}
    for raw_join in selectivities_raw:
        join_item = _require_object(raw_join, "E_BAD_STATS", "statistics.join_selectivities[]")
        left_table = _require_str(join_item.get("left_table"), "E_BAD_STATS", "join_selectivities[].left_table")
        right_table = _require_str(join_item.get("right_table"), "E_BAD_STATS", "join_selectivities[].right_table")
        left_column = _require_str(join_item.get("left_column"), "E_BAD_STATS", "join_selectivities[].left_column")
        right_column = _require_str(join_item.get("right_column"), "E_BAD_STATS", "join_selectivities[].right_column")
        edge = (left_table, left_column, right_table, right_column)
        reverse = (right_table, right_column, left_table, left_column)
        declared_edges = {predicate.edge_key for predicate in predicates}
        if edge not in declared_edges and reverse not in declared_edges:
            raise _bad("E_BAD_STATS", "join selectivity references an unknown predicate", edge=edge)
        if edge in selectivities or reverse in selectivities:
            raise _bad("E_BAD_STATS", "duplicate join selectivity", edge=edge)
        selectivity = _require_number(join_item.get("selectivity"), "E_BAD_STATS", "join selectivity")
        if not 0.0 < selectivity < 1.0:
            raise _bad("E_BAD_STATS", "join selectivity must be in (0, 1)", edge=edge)
        selectivities[edge] = selectivity

    declared_edges = {predicate.edge_key for predicate in predicates}
    missing = [
        edge
        for edge in declared_edges
        if edge not in selectivities
        and (edge[2], edge[3], edge[0], edge[1]) not in selectivities
    ]
    if missing:
        raise _bad("E_BAD_STATS", "missing join selectivity", missing=[list(edge) for edge in sorted(missing)])
    return StatsSnapshot(tables=stats_tables, join_selectivities=selectivities)


def _parse_config(raw: Any) -> ReoptConfig:
    item = _require_object(raw, "E_BAD_CONFIG", "reopt")
    drift_threshold = _require_number(
        item.get("drift_threshold"), "E_BAD_CONFIG", "reopt.drift_threshold", minimum=0.0
    )
    if drift_threshold <= 0.0:
        raise _bad("E_BAD_CONFIG", "reopt.drift_threshold must be > 0")
    cooldown_ms = _require_int(item.get("cooldown_ms"), "E_BAD_CONFIG", "reopt.cooldown_ms", minimum=0)
    clock_step_ms = _require_int(item.get("clock_step_ms"), "E_BAD_CONFIG", "reopt.clock_step_ms", minimum=1)
    switch_cost_budget = _require_number(
        item.get("switch_cost_budget"), "E_BAD_CONFIG", "reopt.switch_cost_budget", minimum=0.0
    )
    float_tolerance = _require_number(
        item.get("float_tolerance"), "E_BAD_CONFIG", "reopt.float_tolerance", minimum=0.0
    )
    error_bound_budget = _require_number(
        item.get("error_bound_budget"), "E_BAD_CONFIG", "reopt.error_bound_budget", minimum=0.0
    )
    if error_bound_budget <= 0.0:
        raise _bad("E_BAD_CONFIG", "reopt.error_bound_budget must be > 0")
    algorithms_raw = item.get("algorithm_candidates", ["hash", "sort_merge", "nested_loop"])
    algorithms_list = _require_list(algorithms_raw, "E_BAD_CONFIG", "reopt.algorithm_candidates")
    algorithms = tuple(
        _require_str(value, "E_BAD_CONFIG", "reopt.algorithm_candidates[]") for value in algorithms_list
    )
    invalid = sorted(set(algorithms) - SUPPORTED_ALGORITHMS)
    if invalid:
        raise _bad("E_BAD_CONFIG", "unsupported algorithm candidate", algorithms=invalid)
    if not algorithms:
        raise _bad("E_BAD_CONFIG", "algorithm_candidates must not be empty")
    if len(algorithms) != len(set(algorithms)):
        raise _bad("E_BAD_CONFIG", "algorithm_candidates must be unique", algorithms=list(algorithms))
    return ReoptConfig(
        drift_threshold=drift_threshold,
        cooldown_ms=cooldown_ms,
        clock_step_ms=clock_step_ms,
        switch_cost_budget=switch_cost_budget,
        float_tolerance=float_tolerance,
        error_bound_budget=error_bound_budget,
        algorithm_candidates=algorithms,
    )


def parse_sample(raw: Any, source_path: str = "") -> SampleSpec:
    item = _require_object(raw, "E_BAD_SAMPLE", "sample")
    sample_id = _require_str(item.get("sample_id"), "E_BAD_SAMPLE", "sample_id")
    global_seed = _require_int(item.get("global_seed"), "E_BAD_SAMPLE", "global_seed", minimum=0)
    tables_raw = _require_list(item.get("tables"), "E_BAD_SAMPLE", "tables")
    if not tables_raw:
        raise _bad("E_BAD_TABLE", "sample must define at least one table")
    tables = tuple(_parse_table(raw_table) for raw_table in tables_raw)
    table_names = [table.name for table in tables]
    if len(table_names) != len(set(table_names)):
        raise _bad("E_BAD_TABLE", "table names must be unique", tables=table_names)
    table_map = {table.name: table for table in tables}
    plan, _ = _parse_plan(item.get("plan"), table_map)
    statistics = _parse_stats(item.get("statistics"), table_map, plan.joins)
    config = _parse_config(item.get("reopt"))
    return SampleSpec(
        sample_id=sample_id,
        global_seed=global_seed,
        tables=tables,
        plan=plan,
        statistics=statistics,
        config=config,
        source_path=source_path,
    )


def load_sample(path: str | Path) -> SampleSpec:
    sample_path = Path(path)
    try:
        with sample_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise _bad("E_BAD_SAMPLE", "sample file not found", path=str(sample_path)) from None
    except json.JSONDecodeError as exc:
        raise _bad(
            "E_BAD_SAMPLE",
            "sample file is not valid JSON",
            path=str(sample_path),
            line=exc.lineno,
            column=exc.colno,
        ) from None
    except OSError as exc:
        raise _bad("E_IO", "unable to read sample file", path=str(sample_path), error=str(exc)) from None
    return parse_sample(raw, source_path=str(sample_path))
