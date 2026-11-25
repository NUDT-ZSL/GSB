from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .errors import ErrorCode, ValidationError
from .model import (
    AGG_FUNCTIONS,
    COLUMN_TYPES,
    OPERATORS,
    AggregationSpec,
    ArrivalScript,
    BatchSpec,
    Bundle,
    ColumnSpec,
    CostEstimate,
    FormSpec,
    PlanSpec,
    RuntimeConfig,
    TableSpec,
    WindowSpec,
)


def load_json_file(path: str | Path) -> Any:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal)
    except FileNotFoundError as exc:
        raise ValidationError(ErrorCode.SCHEMA_INVALID, f"input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            ErrorCode.JSON_INVALID,
            f"invalid JSON in {path}: {exc.msg}",
            {"line": exc.lineno, "column": exc.colno},
        ) from exc


def load_sample_bundle(sample_dir: str | Path) -> Bundle:
    root = Path(sample_dir)

    def load_first(names: tuple[str, ...]) -> Any:
        for name in names:
            path = root / name
            if path.exists():
                return load_json_file(path)
        raise ValidationError(
            ErrorCode.SCHEMA_INVALID,
            f"missing input file; tried {', '.join(names)} in {root}",
        )

    return parse_bundle(
        load_first(("table.json", "table_description.json", "tables.json")),
        load_first(("plan.json", "query_plan.json", "streaming_query_plan.json")),
        load_first(("arrivals.json", "arrival_script.json", "batches.json")),
        load_first(("config.json", "execution_config.json")),
    )


def parse_bundle(
    table_raw: Any,
    plan_raw: Any,
    arrivals_raw: Any,
    config_raw: Any,
) -> Bundle:
    tables = parse_tables(table_raw)
    table_by_name = {table.name: table for table in tables}
    plan = parse_plan(plan_raw, table_by_name)
    arrivals = parse_arrivals(arrivals_raw, plan, tables[0].global_seed if tables else 0)
    config = parse_config(config_raw)
    return Bundle(tables=tables, plan=plan, arrivals=arrivals, config=config)


def _mapping(raw: Any, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError(code, message, {"received_type": type(raw).__name__})
    return raw


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    data: Mapping[str, Any],
    key: str,
    code: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    default: int | None = None,
) -> int:
    if key not in data or data[key] is None:
        if default is not None:
            return default
        raise ValidationError(code, f"missing integer field: {key}", {"field": key})
    value = data[key]
    if not _is_int(value):
        raise ValidationError(code, f"field {key!r} must be an integer", {"field": key})
    if positive and value <= 0:
        raise ValidationError(code, f"field {key!r} must be a positive integer", {"value": value})
    if nonnegative and value < 0:
        raise ValidationError(code, f"field {key!r} must be a non-negative integer", {"value": value})
    return value


def _fraction(value: Any, field: str, code: str) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValidationError(code, f"field {field!r} must be numeric", {"value": value})
    if _is_int(value):
        return Fraction(value, 1)
    if isinstance(value, Decimal):
        try:
            return Fraction(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(code, f"field {field!r} is not a valid decimal", {"value": str(value)}) from exc
    if isinstance(value, str):
        try:
            return Fraction(Decimal(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(code, f"field {field!r} is not a valid decimal", {"value": value}) from exc
    if isinstance(value, float):
        return Fraction(Decimal(str(value)))
    raise ValidationError(code, f"field {field!r} must be numeric", {"value": repr(value)})


def _require_fraction(
    data: Mapping[str, Any],
    key: str,
    code: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    open_unit: bool = False,
    default: Fraction | None = None,
) -> Fraction:
    if key not in data or data[key] is None:
        if default is not None:
            return default
        raise ValidationError(code, f"missing numeric field: {key}", {"field": key})
    value = _fraction(data[key], key, code)
    if positive and value <= 0:
        raise ValidationError(code, f"field {key!r} must be positive", {"value": str(value)})
    if nonnegative and value < 0:
        raise ValidationError(code, f"field {key!r} must be non-negative", {"value": str(value)})
    if open_unit and not (0 < value < 1):
        raise ValidationError(
            code,
            f"field {key!r} must be in the open interval (0, 1)",
            {"value": str(value)},
        )
    return value


def _string_list(raw: Any, field: str, code: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) and item for item in raw):
        raise ValidationError(code, f"field {field!r} must be a list of non-empty strings")
    return tuple(raw)


def parse_tables(raw: Any) -> tuple[TableSpec, ...]:
    raw_map = _mapping(raw, ErrorCode.SCHEMA_INVALID, "table description must be an object")
    global_seed = _require_int(
        raw_map,
        "global_seed",
        ErrorCode.GLOBAL_SEED_INVALID,
        default=0,
    )
    if "tables" in raw_map:
        tables_raw = raw_map["tables"]
    elif "name" in raw_map:
        tables_raw = [raw_map]
    else:
        raise ValidationError(ErrorCode.SCHEMA_INVALID, "table description requires 'tables' or one table object")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise ValidationError(ErrorCode.SCHEMA_INVALID, "'tables' must be a non-empty list")

    tables: list[TableSpec] = []
    seen_names: set[str] = set()
    for index, table_raw in enumerate(tables_raw):
        table_map = _mapping(
            table_raw,
            ErrorCode.SCHEMA_INVALID,
            f"table at index {index} must be an object",
        )
        name = table_map.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError(ErrorCode.SCHEMA_INVALID, "table name must be a non-empty string", {"index": index})
        if name in seen_names:
            raise ValidationError(ErrorCode.TABLE_DUPLICATE, f"duplicate table name: {name}")
        seen_names.add(name)

        columns_raw = table_map.get("columns")
        if not isinstance(columns_raw, list) or not columns_raw:
            raise ValidationError(ErrorCode.SCHEMA_INVALID, f"table {name!r} requires non-empty columns")
        columns: list[ColumnSpec] = []
        seen_columns: set[str] = set()
        for col_raw in columns_raw:
            col = _mapping(col_raw, ErrorCode.SCHEMA_INVALID, f"column in table {name!r} must be an object")
            col_name = col.get("name")
            if not isinstance(col_name, str) or not col_name:
                raise ValidationError(ErrorCode.SCHEMA_INVALID, "column name must be a non-empty string")
            if col_name in seen_columns:
                raise ValidationError(ErrorCode.COLUMN_DUPLICATE, f"duplicate column {col_name!r} in table {name!r}")
            seen_columns.add(col_name)
            col_type = col.get("type")
            if col_type not in COLUMN_TYPES:
                raise ValidationError(
                    ErrorCode.COLUMN_TYPE_INVALID,
                    f"column {col_name!r} has unsupported type {col_type!r}",
                    {"supported": sorted(COLUMN_TYPES)},
                )
            ndv = _require_int(col, "ndv", ErrorCode.NDV_INVALID, positive=True)
            if col_type == "boolean" and ndv > 2:
                raise ValidationError(ErrorCode.NDV_INVALID, "boolean column NDV must be at most 2", {"column": col_name})
            decimal_scale = _require_int(
                col,
                "decimal_scale",
                ErrorCode.WINDOW_PARAM_INVALID,
                positive=True,
                default=2,
            )
            columns.append(ColumnSpec(col_name, col_type, ndv, decimal_scale))

        skew_column = table_map.get("skew_column")
        skew_ratio: Fraction | None = None
        if skew_column is not None:
            if not isinstance(skew_column, str) or skew_column not in seen_columns:
                raise ValidationError(
                    ErrorCode.SKEW_COLUMN_INVALID,
                    f"skew_column {skew_column!r} is not a column of table {name!r}",
                )
            skew_col = next(col for col in columns if col.name == skew_column)
            if skew_col.ndv < 2:
                raise ValidationError(
                    ErrorCode.SKEW_COLUMN_INVALID,
                    "skew column NDV must be at least 2 so both hot and cold keys exist",
                    {"column": skew_column},
                )
            skew_ratio = _require_fraction(
                table_map,
                "skew_ratio",
                ErrorCode.SKEW_RATIO_INVALID,
                open_unit=True,
            )
        elif "skew_ratio" in table_map and table_map["skew_ratio"] is not None:
            raise ValidationError(ErrorCode.SKEW_COLUMN_INVALID, "skew_ratio requires skew_column")

        window_raw = table_map.get("window", table_map.get("window_params", {}))
        window_params: dict[str, int] = {}
        if window_raw:
            window_map = _mapping(window_raw, ErrorCode.WINDOW_PARAM_INVALID, "window must be an object")
            for key, value in window_map.items():
                if key == "type" or isinstance(value, str):
                    continue
                if not _is_int(value):
                    raise ValidationError(
                        ErrorCode.WINDOW_PARAM_INVALID,
                        f"window parameter {key!r} must be a positive integer",
                    )
                if key == "allowed_lateness_ms":
                    if value < 0:
                        raise ValidationError(
                            ErrorCode.WINDOW_PARAM_INVALID,
                            "allowed_lateness_ms must be a non-negative integer",
                        )
                elif value <= 0:
                    raise ValidationError(
                        ErrorCode.WINDOW_PARAM_INVALID,
                        f"window parameter {key!r} must be a positive integer",
                    )
            size_value = window_map.get("size_ms", window_map.get("window_size_ms"))
            if size_value is not None:
                if not _is_int(size_value) or size_value <= 0:
                    raise ValidationError(ErrorCode.WINDOW_PARAM_INVALID, "window size_ms must be a positive integer")
                window_params["size_ms"] = size_value
            slide_value = window_map.get("slide_ms", window_map.get("window_slide_ms"))
            if slide_value is not None:
                if not _is_int(slide_value) or slide_value <= 0:
                    raise ValidationError(ErrorCode.WINDOW_PARAM_INVALID, "window slide_ms must be a positive integer")
                window_params["slide_ms"] = slide_value
            for key, value in window_map.items():
                if key in {"size_ms", "window_size_ms", "slide_ms", "window_slide_ms"}:
                    continue
                if key.endswith("_ms") and key != "allowed_lateness_ms":
                    if not _is_int(value) or value <= 0:
                        raise ValidationError(
                            ErrorCode.WINDOW_PARAM_INVALID,
                            f"window parameter {key!r} must be a positive integer",
                        )
                    window_params[key] = value
                elif key == "allowed_lateness_ms":
                    if not _is_int(value) or value < 0:
                        raise ValidationError(
                            ErrorCode.WINDOW_PARAM_INVALID,
                            "allowed_lateness_ms must be a non-negative integer",
                        )
                    window_params[key] = value

        table_seed = _require_int(table_map, "global_seed", ErrorCode.GLOBAL_SEED_INVALID, default=global_seed)
        tables.append(
            TableSpec(
                name=name,
                columns=tuple(columns),
                global_seed=table_seed,
                skew_column=skew_column,
                skew_ratio=skew_ratio,
                window_params=window_params,
            )
        )
    return tuple(tables)


def parse_plan(raw: Any, tables: Mapping[str, TableSpec]) -> PlanSpec:
    plan_map = _mapping(raw, ErrorCode.PLAN_INVALID, "query plan must be an object")
    plan_id = plan_map.get("plan_id", "plan")
    if not isinstance(plan_id, str) or not plan_id:
        raise ValidationError(ErrorCode.PLAN_INVALID, "plan_id must be a non-empty string")
    default_table = plan_map.get("table")
    if default_table is not None and default_table not in tables:
        raise ValidationError(ErrorCode.TABLE_NOT_FOUND, f"plan references unknown table {default_table!r}")

    windows_raw = plan_map.get("windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ValidationError(ErrorCode.PLAN_INVALID, "plan requires a non-empty windows list")

    windows: list[WindowSpec] = []
    window_ids: set[str] = set()
    for win_raw in windows_raw:
        win = _mapping(win_raw, ErrorCode.PLAN_INVALID, "window definition must be an object")
        window_id = win.get("window_id")
        if not isinstance(window_id, str) or not window_id:
            raise ValidationError(ErrorCode.PLAN_INVALID, "window_id must be a non-empty string")
        if window_id in window_ids:
            raise ValidationError(ErrorCode.WINDOW_DUPLICATE, f"duplicate window_id {window_id!r}")
        window_ids.add(window_id)

        table_name = win.get("table", default_table)
        if not isinstance(table_name, str) or table_name not in tables:
            raise ValidationError(ErrorCode.TABLE_NOT_FOUND, f"window {window_id!r} references unknown table {table_name!r}")
        table = tables[table_name]

        group_by = _string_list(win.get("group_by", []), "group_by", ErrorCode.PLAN_INVALID)
        for col_name in group_by:
            try:
                table.column(col_name)
            except KeyError as exc:
                raise ValidationError(ErrorCode.COLUMN_NOT_FOUND, f"group_by column {col_name!r} does not exist") from exc

        aggs_raw = win.get("aggregations", [])
        if not isinstance(aggs_raw, list) or not aggs_raw:
            raise ValidationError(ErrorCode.AGGREGATION_INVALID, f"window {window_id!r} requires aggregations")
        aggs: list[AggregationSpec] = []
        aliases: set[str] = set()
        for agg_raw in aggs_raw:
            agg = _mapping(agg_raw, ErrorCode.AGGREGATION_INVALID, "aggregation must be an object")
            op = agg.get("op")
            if op not in AGG_FUNCTIONS:
                raise ValidationError(ErrorCode.AGGREGATION_INVALID, f"unsupported aggregation op {op!r}")
            column = agg.get("column")
            if op == "count" and column in (None, "*", ""):
                column = None
            else:
                if not isinstance(column, str):
                    raise ValidationError(ErrorCode.AGGREGATION_INVALID, f"{op} aggregation requires a column name")
                try:
                    col = table.column(column)
                except KeyError as exc:
                    raise ValidationError(ErrorCode.COLUMN_NOT_FOUND, f"aggregation column {column!r} does not exist") from exc
                if op in {"sum", "avg"} and col.type not in {"int", "bigint", "smallint", "double", "float", "decimal"}:
                    raise ValidationError(ErrorCode.AGGREGATION_INVALID, f"{op} requires numeric column {column!r}")
            alias = agg.get("alias") or (f"{op}_{column or 'rows'}")
            if not isinstance(alias, str) or not alias or alias in aliases:
                raise ValidationError(ErrorCode.AGGREGATION_INVALID, f"invalid or duplicate aggregation alias {alias!r}")
            aliases.add(alias)
            aggs.append(AggregationSpec(op=op, column=column, alias=alias))

        operator = win.get("operator", "hash")
        if operator not in OPERATORS:
            raise ValidationError(ErrorCode.OPERATOR_INVALID, f"operator {operator!r} is not supported")

        estimate_raw = win.get("cost_estimate", win.get("estimated"))
        estimate_map = _mapping(estimate_raw, ErrorCode.COST_ESTIMATE_INVALID, f"window {window_id!r} requires cost_estimate")
        input_rows = _require_int(estimate_map, "input_rows", ErrorCode.COST_ESTIMATE_INVALID, positive=True)
        cardinality = _require_int(
            estimate_map,
            "output_cardinality",
            ErrorCode.COST_ESTIMATE_INVALID,
            positive=True,
        )
        elapsed = _require_fraction(
            estimate_map,
            "elapsed_ms",
            ErrorCode.COST_ESTIMATE_INVALID,
            positive=True,
        )
        size_ms = win.get("size_ms", table.window_params.get("size_ms"))
        slide_ms = win.get("slide_ms", table.window_params.get("slide_ms"))
        if size_ms is not None and (not _is_int(size_ms) or size_ms <= 0):
            raise ValidationError(ErrorCode.WINDOW_PARAM_INVALID, "size_ms must be a positive integer")
        if slide_ms is not None and (not _is_int(slide_ms) or slide_ms <= 0):
            raise ValidationError(ErrorCode.WINDOW_PARAM_INVALID, "slide_ms must be a positive integer")
        windows.append(
            WindowSpec(
                window_id=window_id,
                table=table_name,
                group_by=group_by,
                aggregations=tuple(aggs),
                operator=operator,
                cost_estimate=CostEstimate(input_rows, cardinality, elapsed),
                size_ms=size_ms,
                slide_ms=slide_ms,
            )
        )

    order_raw = plan_map.get("window_order") or [w.window_id for w in windows]
    window_order = _string_list(order_raw, "window_order", ErrorCode.WINDOW_ORDER_INVALID)
    if set(window_order) != window_ids or len(window_order) != len(set(window_order)):
        raise ValidationError(ErrorCode.WINDOW_ORDER_INVALID, "window_order must exactly list every window once")

    initial_raw = plan_map.get("initial_form")
    if initial_raw is None:
        initial_form = FormSpec(
            form_id="initial",
            window_order=window_order,
            operators={w.window_id: w.operator for w in windows},
        )
    else:
        initial_form = parse_form(initial_raw, window_ids, window_order, tables, default_id="initial")

    candidates_raw = plan_map.get("candidates", [])
    if not isinstance(candidates_raw, list):
        raise ValidationError(ErrorCode.CANDIDATE_INVALID, "candidates must be a list")
    candidates = tuple(
        parse_form(item, window_ids, window_order, tables, default_id=f"candidate-{index}")
        for index, item in enumerate(candidates_raw)
    )
    return PlanSpec(
        plan_id=plan_id,
        table=default_table or windows[0].table,
        window_order=window_order,
        windows=tuple(windows),
        initial_form=initial_form,
        candidates=candidates,
    )


def parse_form(
    raw: Any,
    window_ids: set[str],
    default_order: tuple[str, ...],
    tables: Mapping[str, TableSpec],
    *,
    default_id: str,
) -> FormSpec:
    data = _mapping(raw, ErrorCode.CANDIDATE_INVALID, "execution form must be an object")
    form_id = data.get("form_id", default_id)
    if not isinstance(form_id, str) or not form_id:
        raise ValidationError(ErrorCode.CANDIDATE_INVALID, "form_id must be a non-empty string")
    order_raw = data.get("window_order", default_order)
    order = _string_list(order_raw, "window_order", ErrorCode.WINDOW_ORDER_INVALID)
    if set(order) != window_ids or len(order) != len(set(order)):
        raise ValidationError(ErrorCode.CANDIDATE_INVALID, f"form {form_id!r} window_order must list every window once")

    operators_raw = data.get("operators", {})
    if not isinstance(operators_raw, Mapping):
        raise ValidationError(ErrorCode.OPERATOR_INVALID, f"form {form_id!r} operators must be an object")
    operators: dict[str, str] = {}
    for window_id, operator in operators_raw.items():
        if window_id not in window_ids:
            raise ValidationError(ErrorCode.WINDOW_NOT_FOUND, f"form {form_id!r} references unknown window {window_id!r}")
        if operator not in OPERATORS:
            raise ValidationError(ErrorCode.OPERATOR_INVALID, f"operator {operator!r} is not supported")
        operators[window_id] = operator

    group_orders_raw = data.get("group_orders", {})
    if not isinstance(group_orders_raw, Mapping):
        raise ValidationError(ErrorCode.CANDIDATE_INVALID, "group_orders must be an object")
    group_orders: dict[str, tuple[str, ...]] = {}
    for window_id, columns_raw in group_orders_raw.items():
        if window_id not in window_ids:
            raise ValidationError(ErrorCode.WINDOW_NOT_FOUND, f"group_orders references unknown window {window_id!r}")
        group_orders[window_id] = _string_list(columns_raw, f"group_orders.{window_id}", ErrorCode.CANDIDATE_INVALID)

    switch_cost = None
    if "switch_cost_ms" in data and data["switch_cost_ms"] is not None:
        switch_cost = _require_fraction(data, "switch_cost_ms", ErrorCode.CANDIDATE_INVALID, nonnegative=True)
    setup_cost = None
    if "setup_cost_ms" in data and data["setup_cost_ms"] is not None:
        setup_cost = _require_fraction(data, "setup_cost_ms", ErrorCode.CANDIDATE_INVALID, nonnegative=True)
    runtime_cost = None
    if "projected_runtime_ms" in data and data["projected_runtime_ms"] is not None:
        runtime_cost = _require_fraction(data, "projected_runtime_ms", ErrorCode.CANDIDATE_INVALID, positive=True)
    target_version = data.get("target_version")
    if target_version is not None and (not _is_int(target_version) or target_version <= 0):
        raise ValidationError(ErrorCode.FORM_VERSION_ROLLBACK, "target_version must be a positive integer")
    return FormSpec(
        form_id=form_id,
        window_order=order,
        operators=operators,
        group_orders=group_orders,
        switch_cost_ms=switch_cost,
        setup_cost_ms=setup_cost,
        projected_runtime_ms=runtime_cost,
        target_version=target_version,
    )


def parse_arrivals(raw: Any, plan: PlanSpec, default_seed: int) -> ArrivalScript:
    data = _mapping(raw, ErrorCode.SCHEMA_INVALID, "arrival script must be an object")
    seed = _require_int(data, "global_seed", ErrorCode.GLOBAL_SEED_INVALID, default=default_seed)
    batches_raw = data.get("batches")
    if not isinstance(batches_raw, list):
        raise ValidationError(ErrorCode.SCHEMA_INVALID, "arrival script requires a batches list")
    batches: list[BatchSpec] = []
    previous_seq: int | None = None
    for index, item in enumerate(batches_raw):
        batch = _mapping(item, ErrorCode.SCHEMA_INVALID, f"batch at index {index} must be an object")
        seq = batch.get("seq")
        if not _is_int(seq):
            raise ValidationError(ErrorCode.BATCH_SEQ_INVALID, "batch seq must be an integer", {"index": index})
        if previous_seq is not None and seq <= previous_seq:
            raise ValidationError(
                ErrorCode.BATCH_SEQ_NOT_INCREASING,
                "batch seq values must be strictly increasing",
                {"previous_seq": previous_seq, "current_seq": seq},
            )
        previous_seq = seq
        arrival_ms = _require_int(batch, "arrival_ms", ErrorCode.CLOCK_INVALID, nonnegative=True)
        row_count = _require_int(batch, "row_count", ErrorCode.BATCH_ROW_COUNT_INVALID, positive=True)
        table_name = batch.get("table", plan.table)
        if not isinstance(table_name, str):
            raise ValidationError(ErrorCode.TABLE_NOT_FOUND, "batch table must be a string")
        try:
            plan_ctx_table = None
            for window in plan.windows:
                if window.window_id == batch.get("window_id"):
                    plan_ctx_table = window.table
                    break
        except Exception:  # pragma: no cover - defensive
            plan_ctx_table = None
        window_id = batch.get("window_id")
        if not isinstance(window_id, str):
            raise ValidationError(ErrorCode.WINDOW_NOT_FOUND, "batch window_id must be a string")
        try:
            window = plan.window(window_id)
        except KeyError as exc:
            raise ValidationError(ErrorCode.WINDOW_NOT_FOUND, f"batch {seq} references unknown window {window_id!r}") from exc
        if table_name != window.table:
            raise ValidationError(
                ErrorCode.BATCH_WINDOW_MISMATCH,
                f"batch {seq} table {table_name!r} does not match window table {window.table!r}",
            )
        window_end = batch.get("window_end", False)
        if not isinstance(window_end, bool):
            raise ValidationError(ErrorCode.SCHEMA_INVALID, f"batch {seq} window_end must be boolean")
        batches.append(BatchSpec(seq, table_name, window_id, arrival_ms, row_count, window_end))
    return ArrivalScript(global_seed=seed, batches=tuple(batches))


def parse_config(raw: Any) -> RuntimeConfig:
    source = _mapping(raw, ErrorCode.SCHEMA_INVALID, "execution config must be an object")
    data = dict(source)
    aliases = {
        "cooldown_ms": ("reoptimize_cooldown_logical_ms", "cooldown_logical_ms", "reopt_cooldown_ms"),
        "switch_cost_budget_ms": ("switch_cost_budget", "switch_budget_ms", "switch_budget"),
        "max_parallel_tasks": ("parallel_task_limit", "max_parallel", "parallelism"),
        "logical_time_budget_per_round_ms": (
            "logical_time_budget_ms",
            "round_logical_time_budget_ms",
            "per_round_logical_time_budget_ms",
        ),
        "fixed_point_scale": ("fixed_scale", "scale"),
        "memory_budget_bytes": ("memory_budget",),
        "cost_tolerance_ms": ("cost_tolerance",),
    }
    for canonical, alias_names in aliases.items():
        if canonical not in data or data[canonical] is None:
            for alias_name in alias_names:
                if alias_name in data and data[alias_name] is not None:
                    data[canonical] = data[alias_name]
                    break
    threshold = _require_fraction(
        data,
        "drift_threshold",
        ErrorCode.DRIFT_THRESHOLD_INVALID,
        open_unit=True,
    )
    cooldown = _require_int(data, "cooldown_ms", ErrorCode.COOLDOWN_INVALID, nonnegative=True, default=0)
    switch_budget = _require_fraction(
        data,
        "switch_cost_budget_ms",
        ErrorCode.SWITCH_BUDGET_INVALID,
        nonnegative=True,
        default=Fraction(0),
    )
    parallel = _require_int(
        data,
        "max_parallel_tasks",
        ErrorCode.PARALLEL_LIMIT_INVALID,
        positive=True,
    )
    logical_budget = _require_int(
        data,
        "logical_time_budget_per_round_ms",
        ErrorCode.LOGICAL_BUDGET_INVALID,
        positive=True,
    )
    scale = _require_int(data, "fixed_point_scale", ErrorCode.FIXED_SCALE_INVALID, positive=True)
    memory_budget = _require_int(
        data,
        "memory_budget_bytes",
        ErrorCode.MEMORY_BUDGET_INVALID,
        positive=True,
        default=64 * 1024 * 1024,
    )
    tolerance = _require_fraction(
        data,
        "cost_tolerance_ms",
        ErrorCode.TOLERANCE_INVALID,
        positive=True,
        default=Fraction(1, 1000),
    )
    min_batches = _require_int(
        data,
        "min_observation_batches",
        ErrorCode.SCHEMA_INVALID,
        positive=True,
        default=1,
    )
    rounding_mode = data.get("rounding_mode", "floor")
    if rounding_mode not in {"floor", "ceil"}:
        raise ValidationError(ErrorCode.SCHEMA_INVALID, "rounding_mode must be 'floor' or 'ceil'")
    return RuntimeConfig(
        drift_threshold=threshold,
        cooldown_ms=cooldown,
        switch_cost_budget_ms=switch_budget,
        max_parallel_tasks=parallel,
        logical_time_budget_per_round_ms=Fraction(logical_budget),
        fixed_point_scale=scale,
        memory_budget_bytes=memory_budget,
        cost_tolerance_ms=tolerance,
        min_observation_batches=min_batches,
        rounding_mode=rounding_mode,
    )
