"""Strict, side-effect-free sample validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import LedgerError, fail
from .fixed import parse_fraction, parse_scale
from .model import Column, Config, DatabaseSpec, Operation, Scenario, Table, Transaction
from .util import canonical_json, sha256_text, validate_identifier

ISOLATION_LEVELS = frozenset(
    {"READ_COMMITTED", "REPEATABLE_READ", "SERIALIZABLE", "SNAPSHOT"}
)
OPERATION_TYPES = frozenset({"read", "write", "range_scan", "lock"})
LOCK_MODES = frozenset({"S", "X"})


def _object(value: Any, path: str, code: str = "E_BAD_SCRIPT") -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, "expected a JSON object", path)
    return value


def _array(value: Any, path: str, code: str = "E_BAD_SCRIPT") -> list[Any]:
    if not isinstance(value, list):
        fail(code, "expected a JSON array", path)
    return value


def _string(value: Any, path: str, code: str = "E_BAD_SCRIPT") -> str:
    if not isinstance(value, str) or not value.strip():
        fail(code, "expected a non-empty string", path)
    return value.strip()


def _positive_int(value: Any, path: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(code, "expected a positive integer", path)
    return value


def _nonnegative_int(value: Any, path: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(code, "expected a non-negative integer", path)
    return value


def _mode(value: Any, path: str) -> str:
    mode = _string(value, path, "E_BAD_OPERATION").upper()
    aliases = {"SHARED": "S", "EXCLUSIVE": "X"}
    mode = aliases.get(mode, mode)
    if mode not in LOCK_MODES:
        fail("E_BAD_OPERATION", "lock mode must be S, X, SHARED, or EXCLUSIVE", path)
    return mode


def _json_scalar(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            fail("E_BAD_SCRIPT", "non-finite JSON number is not allowed", path)
        return value
    fail("E_BAD_SCRIPT", "row/range values must be JSON scalars", path)


def _parse_tables(database_value: Any, base: str) -> DatabaseSpec:
    database = _object(database_value, base)
    raw_tables = database.get("tables")
    raw_rows = database.get("rows", {})
    if isinstance(raw_tables, dict):
        tables_iter = [
            {"name": name, **(value if isinstance(value, dict) else {"columns": value})}
            for name, value in raw_tables.items()
        ]
    else:
        tables_iter = _array(raw_tables, f"{base}.tables")
    rows_by_table = _object(raw_rows, f"{base}.rows")

    tables: list[Table] = []
    seen_tables: set[str] = set()
    for table_index, raw_table in enumerate(tables_iter):
        table_path = f"{base}.tables[{table_index}]"
        table_obj = _object(raw_table, table_path)
        name = validate_identifier(
            _string(table_obj.get("name"), f"{table_path}.name"),
            f"{table_path}.name",
        )
        lowered = name.lower()
        if lowered in seen_tables:
            fail("E_DUPLICATE_TABLE", f"duplicate table name: {name}", table_path)
        seen_tables.add(lowered)

        raw_columns = table_obj.get("columns")
        if isinstance(raw_columns, dict):
            columns_iter = [
                {"name": column_name, "type": column_type}
                for column_name, column_type in raw_columns.items()
            ]
        else:
            columns_iter = _array(raw_columns, f"{table_path}.columns")
        columns: list[Column] = []
        seen_columns: set[str] = set()
        for column_index, raw_column in enumerate(columns_iter):
            column_path = f"{table_path}.columns[{column_index}]"
            if isinstance(raw_column, str):
                column_obj = {"name": raw_column, "type": "TEXT"}
            else:
                column_obj = _object(raw_column, column_path)
            column_name = validate_identifier(
                _string(column_obj.get("name"), f"{column_path}.name"),
                f"{column_path}.name",
            )
            column_lower = column_name.lower()
            if column_lower in seen_columns:
                fail(
                    "E_DUPLICATE_COLUMN",
                    f"duplicate column {column_name} in table {name}",
                    column_path,
                )
            seen_columns.add(column_lower)
            column_type = _string(
                column_obj.get("type", "TEXT"), f"{column_path}.type"
            ).upper()
            columns.append(Column(column_name, column_type))
        if not columns:
            fail("E_BAD_SCRIPT", f"table {name} has no columns", table_path)

        primary_key = table_obj.get("primary_key")
        if primary_key is None:
            primary_key = "id" if "id" in seen_columns else columns[0].name
        primary_key = _string(primary_key, f"{table_path}.primary_key")
        if primary_key.lower() not in seen_columns:
            fail(
                "E_NO_COLUMN",
                f"primary key {primary_key} is not a column of table {name}",
                f"{table_path}.primary_key",
            )

        raw_table_rows = rows_by_table.get(name, rows_by_table.get(lowered, []))
        table_rows = _array(raw_table_rows, f"{base}.rows.{name}")
        parsed_rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row_index, raw_row in enumerate(table_rows):
            row_path = f"{base}.rows.{name}[{row_index}]"
            row = _object(raw_row, row_path)
            normalized_row: dict[str, Any] = {}
            row_lookup: set[str] = set()
            for key, value in row.items():
                column_name = _string(key, row_path)
                if column_name.lower() not in seen_columns:
                    fail(
                        "E_NO_COLUMN",
                        f"row references unknown column {column_name} in table {name}",
                        row_path,
                    )
                normalized_row[column_name] = _json_scalar(value, f"{row_path}.{key}")
                row_lookup.add(column_name.lower())
            if primary_key.lower() not in row_lookup:
                fail(
                    "E_BAD_SCRIPT",
                    f"row is missing primary key {primary_key}",
                    row_path,
                )
            key_value = next(
                value
                for column_name, value in normalized_row.items()
                if column_name.lower() == primary_key.lower()
            )
            key_text = canonical_json(key_value)
            if key_text in seen_keys:
                fail(
                    "E_BAD_SCRIPT",
                    f"duplicate primary key {key_value!r} in table {name}",
                    row_path,
                )
            seen_keys.add(key_text)
            parsed_rows.append(normalized_row)
        tables.append(Table(name, tuple(columns), primary_key, tuple(parsed_rows)))
    if not tables:
        fail("E_BAD_SCRIPT", "database must declare at least one table", base)
    return DatabaseSpec(tuple(tables))


def _column_exists(table: Table, column_name: str) -> bool:
    return table.column(column_name) is not None


def _row_lookup(table: Table, key_value: Any) -> dict[str, Any] | None:
    for row in table.rows:
        for column_name, value in row.items():
            if column_name.lower() == table.primary_key.lower() and value == key_value:
                return row
    return None


def _extract_range(obj: dict[str, Any], path: str) -> tuple[Any, Any]:
    if "range" in obj:
        raw_range = obj["range"]
        if isinstance(raw_range, dict):
            lo = raw_range.get("lo", raw_range.get("start"))
            hi = raw_range.get("hi", raw_range.get("end"))
        elif isinstance(raw_range, list) and len(raw_range) == 2:
            lo, hi = raw_range
        else:
            fail("E_BAD_OPERATION", "range must be an object or [lo, hi]", path)
    else:
        lo = obj.get("start", obj.get("lo"))
        hi = obj.get("end", obj.get("hi"))
    if lo is None or hi is None:
        fail("E_BAD_OPERATION", "range requires both start/lo and end/hi", path)
    lo = _json_scalar(lo, f"{path}.start")
    hi = _json_scalar(hi, f"{path}.end")
    try:
        if lo > hi:
            fail("E_BAD_OPERATION", "range start must not exceed range end", path)
    except TypeError:
        fail("E_BAD_OPERATION", "range bounds must be comparable", path)
    return lo, hi


def _parse_operation(
    raw_operation: Any,
    index: int,
    tx_path: str,
    database: DatabaseSpec,
) -> Operation:
    path = f"{tx_path}.operations[{index}]"
    obj = _object(raw_operation, path)
    raw_kind = _string(obj.get("type", obj.get("op")), f"{path}.type", "E_BAD_OPERATION")
    kind = raw_kind.lower()
    if kind == "lock_declare":
        kind = "lock"
    if kind not in OPERATION_TYPES:
        fail(
            "E_BAD_OPERATION",
            f"unsupported operation type {raw_kind!r}; expected {sorted(OPERATION_TYPES)}",
            f"{path}.type",
        )
    op_id = str(obj.get("op_id", index + 1))
    table_name = obj.get("table")
    if table_name is None and isinstance(obj.get("resource"), dict):
        table_name = obj["resource"].get("table")
    table_name = _string(table_name, f"{path}.table")
    table = database.table(table_name)
    if table is None:
        fail("E_NO_TABLE", f"unknown table: {table_name}", f"{path}.table")
    table_name = table.name

    columns: list[str] = []
    raw_columns = obj.get("columns", [])
    if raw_columns is not None:
        for column_index, column_value in enumerate(
            _array(raw_columns, f"{path}.columns")
        ):
            column_name = _string(column_value, f"{path}.columns[{column_index}]")
            if not _column_exists(table, column_name):
                fail(
                    "E_NO_COLUMN",
                    f"unknown column {column_name} in table {table.name}",
                    f"{path}.columns[{column_index}]",
                )
            columns.append(column_name)

    values: dict[str, Any] | None = None
    if "values" in obj:
        raw_values = _object(obj["values"], f"{path}.values")
        values = {}
        for column_name, value in raw_values.items():
            if not _column_exists(table, column_name):
                fail(
                    "E_NO_COLUMN",
                    f"unknown column {column_name} in table {table.name}",
                    f"{path}.values",
                )
            values[column_name] = _json_scalar(value, f"{path}.values.{column_name}")

    if kind in {"read", "write"}:
        row_value = obj.get("row", obj.get("key"))
        if row_value is None:
            fail("E_BAD_OPERATION", f"{kind} requires row/key", path)
        row_value = _json_scalar(row_value, f"{path}.row")
        row = _row_lookup(table, row_value)
        if row is None:
            fail(
                "E_NO_ROW",
                f"{kind} references missing primary key {row_value!r} in {table.name}",
                f"{path}.row",
            )
        if values:
            for column_name in values:
                if column_name.lower() == table.primary_key.lower() and values[column_name] != row_value:
                    fail(
                        "E_BAD_SCRIPT",
                        "write may not change the primary key of the addressed row",
                        f"{path}.values",
                    )
        return Operation(
            index=index,
            op_id=op_id,
            kind=kind,
            table=table.name,
            mode="S" if kind == "read" else "X",
            row_value=row_value,
            columns=tuple(columns),
            values=values,
        )

    if kind == "range_scan":
        lo, hi = _extract_range(obj, path)
        range_column = obj.get("column", table.primary_key)
        range_column = _string(range_column, f"{path}.column")
        if not _column_exists(table, range_column):
            fail(
                "E_NO_COLUMN",
                f"unknown range column {range_column} in table {table.name}",
                f"{path}.column",
            )
        return Operation(
            index=index,
            op_id=op_id,
            kind=kind,
            table=table.name,
            mode="S",
            range_lo=lo,
            range_hi=hi,
            columns=tuple(columns),
        )

    resource = obj.get("resource", obj)
    resource_obj = _object(resource, f"{path}.resource")
    mode = _mode(resource_obj.get("mode", obj.get("mode", "X")), f"{path}.mode")
    if "row" in resource_obj or "key" in resource_obj:
        row_value = resource_obj.get("row", resource_obj.get("key"))
        row_value = _json_scalar(row_value, f"{path}.row")
        return Operation(
            index=index,
            op_id=op_id,
            kind=kind,
            table=table.name,
            mode=mode,
            row_value=row_value,
            columns=tuple(columns),
        )
    if "range" in resource_obj or "start" in resource_obj or "lo" in resource_obj:
        lo, hi = _extract_range(resource_obj, path)
        return Operation(
            index=index,
            op_id=op_id,
            kind=kind,
            table=table.name,
            mode=mode,
            range_lo=lo,
            range_hi=hi,
            columns=tuple(columns),
        )
    fail("E_BAD_OPERATION", "lock requires a row or range target", path)


def _parse_transactions(raw_value: Any, database: DatabaseSpec) -> tuple[Transaction, ...]:
    if isinstance(raw_value, dict):
        raw_transactions = raw_value.get("transactions")
    else:
        raw_transactions = raw_value
    items = _array(raw_transactions, "transactions")
    transactions: list[Transaction] = []
    seen_keys: set[str] = set()
    seen_numbers: set[int] = set()
    for tx_index, raw_tx in enumerate(items):
        path = f"transactions[{tx_index}]"
        obj = _object(raw_tx, path)
        tx_no = obj.get("tx_no", obj.get("transaction_no", tx_index + 1))
        tx_no = _positive_int(tx_no, f"{path}.tx_no", "E_BAD_SCRIPT")
        if tx_no in seen_numbers:
            fail("E_BAD_SCRIPT", f"duplicate transaction number {tx_no}", path)
        seen_numbers.add(tx_no)
        tx_id = obj.get("tx_id", obj.get("id", f"T{tx_no}"))
        tx_key = str(_string(str(tx_id), f"{path}.tx_id"))
        if tx_key in seen_keys:
            fail("E_BAD_SCRIPT", f"duplicate transaction id {tx_key}", path)
        seen_keys.add(tx_key)
        isolation = _string(
            obj.get("isolation"), f"{path}.isolation", "E_BAD_ISOLATION"
        ).upper()
        if isolation not in ISOLATION_LEVELS:
            fail(
                "E_BAD_ISOLATION",
                f"unsupported isolation level {isolation}; expected {sorted(ISOLATION_LEVELS)}",
                f"{path}.isolation",
            )
        budget = obj.get(
            "wait_budget_ms",
            obj.get("lock_wait_budget_ms", obj.get("wait_budget")),
        )
        budget = _positive_int(budget, f"{path}.wait_budget_ms", "E_BAD_BUDGET")
        retries = obj.get("max_retries", obj.get("retry_limit"))
        retries = _positive_int(retries, f"{path}.max_retries", "E_BAD_RETRY")
        raw_ops = _array(obj.get("operations"), f"{path}.operations")
        if not raw_ops:
            fail("E_BAD_SCRIPT", "transaction must contain at least one operation", path)
        operations = tuple(
            _parse_operation(raw_op, op_index, path, database)
            for op_index, raw_op in enumerate(raw_ops)
        )
        seen_op_ids: set[str] = set()
        for operation in operations:
            if operation.op_id in seen_op_ids:
                fail(
                    "E_BAD_SCRIPT",
                    f"duplicate operation id {operation.op_id}",
                    f"{path}.operations",
                )
            seen_op_ids.add(operation.op_id)
        rollback_after = obj.get("rollback_after_ops")
        if rollback_after is not None:
            rollback_after = _nonnegative_int(
                rollback_after, f"{path}.rollback_after_ops", "E_BAD_SCRIPT"
            )
        transactions.append(
            Transaction(
                tx_key=tx_key,
                tx_no=tx_no,
                isolation=isolation,
                wait_budget_ms=budget,
                max_retries=retries,
                operations=operations,
                rollback_after_ops=rollback_after,
            )
        )
    if not transactions:
        fail("E_BAD_SCRIPT", "at least one transaction is required", "transactions")
    return tuple(transactions)


def _parse_config(raw_value: Any) -> Config:
    obj = _object(raw_value, "config", "E_BAD_CONFIG")
    clock_step = _positive_int(
        obj.get("clock_step_ms", 1),
        "config.clock_step_ms",
        "E_BAD_CONFIG",
    )
    scale = parse_scale(obj.get("scale", 1), path="config.scale")
    tolerance = parse_fraction(
        obj.get("tolerance", 0), path="config.tolerance", code="E_BAD_CONFIG"
    )
    if tolerance < 0:
        fail("E_BAD_CONFIG", "tolerance must be non-negative", "config.tolerance")
    error_budget = parse_fraction(
        obj.get("error_budget", 1),
        path="config.error_budget",
        code="E_BAD_CONFIG",
    )
    if error_budget < 0:
        fail(
            "E_BAD_CONFIG",
            "error_budget must be non-negative",
            "config.error_budget",
        )
    max_concurrency = _positive_int(
        obj.get("max_concurrency", 4),
        "config.max_concurrency",
        "E_BAD_CONFIG",
    )
    deadlock_period = _positive_int(
        obj.get("deadlock_check_period_ms", 1),
        "config.deadlock_check_period_ms",
        "E_BAD_CONFIG",
    )
    max_rounds = _positive_int(
        obj.get("max_rounds", 10000), "config.max_rounds", "E_BAD_CONFIG"
    )
    horizon = obj.get("logical_horizon_ms")
    if horizon is not None:
        horizon = _positive_int(
            horizon, "config.logical_horizon_ms", "E_BAD_CONFIG"
        )
    return Config(
        clock_step_ms=clock_step,
        scale=scale,
        tolerance=tolerance,
        error_budget=error_budget,
        max_concurrency=max_concurrency,
        deadlock_check_period_ms=deadlock_period,
        max_rounds=max_rounds,
        logical_horizon_ms=horizon,
    )


def validate_scenario(raw: Any, *, source_path: str = "<memory>") -> Scenario:
    document = _object(raw, "<root>", "E_BAD_SCRIPT")
    for required in ("database", "transactions", "config"):
        if required not in document:
            fail("E_BAD_SCRIPT", f"sample is missing section {required!r}", required)
    database = _parse_tables(document["database"], "database")
    transactions = _parse_transactions(document["transactions"], database)
    config = _parse_config(document["config"])
    canonical = canonical_json(document)
    return Scenario(
        database=database,
        transactions=transactions,
        config=config,
        source_path=source_path,
        source_sha256=sha256_text(canonical),
    )


def load_scenario(path: Path) -> Scenario:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail("E_SAMPLE_READ", str(exc), str(path))
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        fail("E_BAD_JSON", str(exc), f"{path}:{exc.lineno}:{exc.colno}")
    return validate_scenario(raw, source_path=str(path))


__all__ = [
    "ISOLATION_LEVELS",
    "LOCK_MODES",
    "OPERATION_TYPES",
    "LedgerError",
    "load_scenario",
    "validate_scenario",
]

