"""Strict parsing and business validation for samples, scripts and config."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .errors import (
    DiagnosticError,
    E_BAD_BUDGET,
    E_BAD_CLOCK,
    E_BAD_CONCURRENCY,
    E_BAD_DEPENDENCY,
    E_BAD_JSON,
    E_BAD_ISOLATION,
    E_BAD_LOCK_MODE,
    E_BAD_OP,
    E_BAD_OPERATION_ORDER,
    E_BAD_SCALE,
    E_BAD_TOLERANCE,
    E_DUP_INDEX,
    E_DUP_TABLE,
    E_DUP_TX,
    E_NO_COLUMN,
    E_NO_TABLE,
    E_SAMPLE_MISMATCH,
    E_TX_CYCLE,
)
from .model import ClockSpec, Config, Operation, SampleBundle, Transaction
from .util import (
    canonical_json,
    fingerprint,
    parse_positive_decimal_ms,
    parse_positive_int,
    read_json,
)

ISOLATION_LEVELS = frozenset({"READ_COMMITTED", "SNAPSHOT", "SERIALIZABLE"})
LOCK_MODES = frozenset({"shared", "exclusive"})
OP_KINDS = frozenset({"read", "write", "scan", "lock_decl", "rollback"})


def _require_mapping(value: Any, field: str, code: str = E_BAD_OP) -> dict:
    if not isinstance(value, dict):
        raise DiagnosticError(code, f"{field} must be an object", {"field": field})
    return value


def _require_list(value: Any, field: str, code: str = E_BAD_OP) -> list:
    if not isinstance(value, list):
        raise DiagnosticError(code, f"{field} must be an array", {"field": field})
    return value


def _require_int(value: Any, field: str, code: str = E_BAD_OP) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiagnosticError(code, f"{field} must be an integer", {"field": field})
    return value


def _require_str(value: Any, field: str, code: str = E_BAD_OP) -> str:
    if not isinstance(value, str) or not value:
        raise DiagnosticError(code, f"{field} must be a non-empty string", {"field": field})
    return value


def _table_columns(db_path: Path) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
    try:
        uri = "file:" + quote(db_path.resolve().as_posix(), safe="/:") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiagnosticError(E_BAD_JSON, f"cannot open sample database: {exc}", {"path": str(db_path)})
    tables: dict[str, tuple[str, ...]] = {}
    indexes: dict[str, dict[str, Any]] = {}
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (table_name,) in rows:
            columns = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            tables[table_name] = tuple(str(row[1]) for row in columns)
        index_rows = connection.execute(
            """
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type='index' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        for name, table_name, sql in index_rows:
            info = connection.execute(f'PRAGMA index_info("{name}")').fetchall()
            indexes[str(name)] = {
                "table": str(table_name),
                "columns": [str(row[2]) for row in info],
                "sql": sql or "",
            }
    finally:
        connection.close()
    return tables, indexes


def _validate_schema(schema: dict, db_tables: dict, db_indexes: dict) -> None:
    table_entries = _require_list(schema.get("tables"), "schema.tables")
    index_entries = _require_list(schema.get("indexes"), "schema.indexes")
    seen_tables: set[str] = set()
    declared_tables: dict[str, tuple[str, ...]] = {}
    for index, raw_table in enumerate(table_entries):
        table = _require_mapping(raw_table, f"schema.tables[{index}]")
        name = _require_str(table.get("name"), f"schema.tables[{index}].name")
        if name in seen_tables:
            raise DiagnosticError(E_DUP_TABLE, f"duplicate table name: {name}", {"table": name})
        seen_tables.add(name)
        raw_columns = _require_list(table.get("columns"), f"schema.tables[{index}].columns")
        columns: list[str] = []
        for col_index, raw_column in enumerate(raw_columns):
            column = _require_mapping(raw_column, f"schema.tables[{index}].columns[{col_index}]")
            column_name = _require_str(
                column.get("name"), f"schema.tables[{index}].columns[{col_index}].name"
            )
            if column_name in columns:
                raise DiagnosticError(
                    E_DUP_TABLE,
                    f"duplicate column {column_name} in table {name}",
                    {"table": name, "column": column_name},
                )
            columns.append(column_name)
        declared_tables[name] = tuple(columns)

    seen_indexes: set[str] = set()
    for index, raw_index in enumerate(index_entries):
        entry = _require_mapping(raw_index, f"schema.indexes[{index}]")
        name = _require_str(entry.get("name"), f"schema.indexes[{index}].name")
        if name in seen_indexes:
            raise DiagnosticError(E_DUP_INDEX, f"duplicate index name: {name}", {"index": name})
        seen_indexes.add(name)
        table_name = _require_str(entry.get("table"), f"schema.indexes[{index}].table")
        if table_name not in declared_tables:
            raise DiagnosticError(
                E_NO_TABLE, f"index {name} references missing table {table_name}", {"table": table_name}
            )
        for column_name in _require_list(entry.get("columns"), f"schema.indexes[{index}].columns"):
            if column_name not in declared_tables[table_name]:
                raise DiagnosticError(
                    E_NO_COLUMN,
                    f"index {name} references missing column {table_name}.{column_name}",
                    {"table": table_name, "column": column_name},
                )

    if set(db_tables) != set(declared_tables):
        missing = sorted(set(db_tables) - set(declared_tables))
        extra = sorted(set(declared_tables) - set(db_tables))
        raise DiagnosticError(
            E_SAMPLE_MISMATCH,
            "schema.json does not match sample database tables",
            {"missing_in_schema": missing, "missing_in_database": extra},
        )
    for table_name, columns in declared_tables.items():
        if tuple(db_tables[table_name]) != columns:
            raise DiagnosticError(
                E_SAMPLE_MISMATCH,
                f"column definition mismatch for table {table_name}",
                {"table": table_name},
            )
    if set(db_indexes) != seen_indexes:
        raise DiagnosticError(
            E_SAMPLE_MISMATCH,
            "schema.json does not match sample database indexes",
            {
                "missing_in_schema": sorted(set(db_indexes) - seen_indexes),
                "missing_in_database": sorted(seen_indexes - set(db_indexes)),
            },
        )
    for index_name, declared in (
        (entry["name"], entry) for entry in index_entries if isinstance(entry, dict)
    ):
        actual = db_indexes[index_name]
        if actual["table"] != declared["table"] or actual["columns"] != list(declared["columns"]):
            raise DiagnosticError(
                E_SAMPLE_MISMATCH,
                f"index definition mismatch for {index_name}",
                {"index": index_name},
            )


def _resource_for_operation(
    table: str,
    kind: str,
    row_id: int | None,
    column: str | None,
    range_start: int | None,
    range_end: int | None,
) -> tuple[str, str]:
    if kind in {"read", "write"}:
        return canonical_json(["row", table, row_id]), "row"
    if kind == "scan":
        return (
            canonical_json(["range", table, column, range_start, range_end]),
            "range",
        )
    return canonical_json(["table", table]), "table"


def _validate_operation(
    tx_id: str,
    tx_version: int,
    position: int,
    raw: dict,
    tables: dict[str, tuple[str, ...]],
) -> Operation:
    data = dict(raw)
    kind = data.get("op")
    if kind not in OP_KINDS:
        raise DiagnosticError(
            E_BAD_OP,
            f"unsupported operation kind: {kind!r}",
            {"tx_id": tx_id, "op_position": position, "op": kind},
        )
    supplied_seq = data.get("op_seq")
    if supplied_seq is not None:
        supplied_seq = _require_int(supplied_seq, "op_seq")
        if supplied_seq != position:
            raise DiagnosticError(
                E_BAD_OPERATION_ORDER,
                f"operation sequence must be contiguous and 1-based for {tx_id}",
                {"tx_id": tx_id, "expected": position, "actual": supplied_seq},
            )
    op_id = _require_str(data.get("op_id"), f"{tx_id}.operations[{position - 1}].op_id")
    if kind == "rollback":
        reason = data.get("reason", "active")
        if not isinstance(reason, str) or not reason:
            raise DiagnosticError(E_BAD_OP, "rollback reason must be a non-empty string")
        return Operation(
            tx_id=tx_id,
            op_seq=position,
            op_version=tx_version,
            op_id=op_id,
            kind=kind,
            data=data,
            resource_key=None,
            resource_type=None,
            table="",
            row_id=None,
            range_start=None,
            range_end=None,
            mode=None,
        )

    table = _require_str(data.get("table"), "table")
    if table not in tables:
        raise DiagnosticError(
            E_NO_TABLE,
            f"operation references missing table {table}",
            {"tx_id": tx_id, "op_id": op_id, "table": table},
        )
    columns = tables[table]

    if kind in {"read", "write"}:
        column = _require_str(data.get("column"), "column")
        if column not in columns:
            raise DiagnosticError(
                E_NO_COLUMN,
                f"operation references missing column {table}.{column}",
                {"tx_id": tx_id, "op_id": op_id, "table": table, "column": column},
            )
        row_id = _require_int(data.get("row_id"), "row_id")
        mode = data.get("lock")
        if mode not in LOCK_MODES:
            raise DiagnosticError(
                E_BAD_LOCK_MODE,
                f"invalid lock mode: {mode!r}",
                {"tx_id": tx_id, "op_id": op_id, "lock": mode},
            )
        expected = "shared" if kind == "read" else "exclusive"
        if mode != expected:
            raise DiagnosticError(
                E_BAD_LOCK_MODE,
                f"{kind} operation requires {expected} lock",
                {"tx_id": tx_id, "op_id": op_id, "expected": expected, "actual": mode},
            )
        resource_key, resource_type = _resource_for_operation(
            table, kind, row_id, column, None, None
        )
        return Operation(
            tx_id=tx_id,
            op_seq=position,
            op_version=tx_version,
            op_id=op_id,
            kind=kind,
            data=data,
            resource_key=resource_key,
            resource_type=resource_type,
            table=table,
            row_id=row_id,
            range_start=None,
            range_end=None,
            mode=mode,
        )

    if kind == "scan":
        column = _require_str(data.get("column"), "column")
        if column not in columns:
            raise DiagnosticError(
                E_NO_COLUMN,
                f"scan references missing column {table}.{column}",
                {"tx_id": tx_id, "op_id": op_id, "table": table, "column": column},
            )
        start = _require_int(data.get("start"), "start")
        end = _require_int(data.get("end"), "end")
        if start > end:
            raise DiagnosticError(
                E_BAD_OP,
                "scan start must be less than or equal to end",
                {"tx_id": tx_id, "op_id": op_id, "start": start, "end": end},
            )
        mode = data.get("lock", "shared")
        if mode not in LOCK_MODES:
            raise DiagnosticError(
                E_BAD_LOCK_MODE,
                f"invalid lock mode: {mode!r}",
                {"tx_id": tx_id, "op_id": op_id, "lock": mode},
            )
        resource_key, resource_type = _resource_for_operation(
            table, kind, None, column, start, end
        )
        return Operation(
            tx_id=tx_id,
            op_seq=position,
            op_version=tx_version,
            op_id=op_id,
            kind=kind,
            data=data,
            resource_key=resource_key,
            resource_type=resource_type,
            table=table,
            row_id=None,
            range_start=start,
            range_end=end,
            mode=mode,
        )

    resource_type = data.get("resource_type")
    if resource_type not in {"table", "row", "range"}:
        raise DiagnosticError(
            E_BAD_OP,
            f"invalid lock_decl resource_type: {resource_type!r}",
            {"tx_id": tx_id, "op_id": op_id},
        )
    mode = data.get("mode")
    if mode not in LOCK_MODES:
        raise DiagnosticError(
            E_BAD_LOCK_MODE,
            f"invalid lock mode: {mode!r}",
            {"tx_id": tx_id, "op_id": op_id, "lock": mode},
        )
    row_id = None
    range_start = None
    range_end = None
    if resource_type == "row":
        row_id = _require_int(data.get("row_id"), "row_id")
        resource_key = canonical_json(["row", table, row_id])
    elif resource_type == "range":
        column = _require_str(data.get("column"), "column")
        if column not in columns:
            raise DiagnosticError(
                E_NO_COLUMN,
                f"range lock references missing column {table}.{column}",
                {"tx_id": tx_id, "op_id": op_id, "table": table, "column": column},
            )
        range_start = _require_int(data.get("start"), "start")
        range_end = _require_int(data.get("end"), "end")
        if range_start > range_end:
            raise DiagnosticError(E_BAD_OP, "range lock start must not exceed end")
        resource_key = canonical_json(["range", table, column, range_start, range_end])
    else:
        resource_key = canonical_json(["table", table])
    return Operation(
        tx_id=tx_id,
        op_seq=position,
        op_version=tx_version,
        op_id=op_id,
        kind=kind,
        data=data,
        resource_key=resource_key,
        resource_type=resource_type,
        table=table,
        row_id=row_id,
        range_start=range_start,
        range_end=range_end,
        mode=mode,
    )


def _validate_transactions(
    document: dict, tables: dict[str, tuple[str, ...]], default_budget: int
) -> tuple[Transaction, ...]:
    raw_transactions = _require_list(document.get("transactions"), "transactions")
    if not raw_transactions:
        raise DiagnosticError(E_BAD_OP, "transactions must not be empty")
    result: list[Transaction] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    seen_ops: set[str] = set()
    dependencies: dict[str, tuple[str, ...]] = {}
    for tx_index, raw_tx in enumerate(raw_transactions):
        tx = _require_mapping(raw_tx, f"transactions[{tx_index}]")
        tx_id = _require_str(tx.get("tx_id"), f"transactions[{tx_index}].tx_id")
        if tx_id in seen_ids:
            raise DiagnosticError(E_DUP_TX, f"duplicate transaction id: {tx_id}", {"tx_id": tx_id})
        seen_ids.add(tx_id)
        start_order = parse_positive_int(
            tx.get("start_order"), E_BAD_OPERATION_ORDER, f"{tx_id}.start_order"
        )
        if start_order in seen_orders:
            raise DiagnosticError(
                E_BAD_OPERATION_ORDER,
                f"duplicate transaction start_order: {start_order}",
                {"start_order": start_order},
            )
        seen_orders.add(start_order)
        version = parse_positive_int(tx.get("version", 1), E_BAD_OP, f"{tx_id}.version")
        isolation = tx.get("isolation")
        if isolation not in ISOLATION_LEVELS:
            raise DiagnosticError(
                E_BAD_ISOLATION,
                f"invalid isolation level: {isolation!r}",
                {"tx_id": tx_id, "isolation": isolation},
            )
        budget = parse_positive_int(
            tx.get("budget_ms", default_budget), E_BAD_BUDGET, f"{tx_id}.budget_ms"
        )
        raw_dependencies = tx.get("depends_on", [])
        raw_dependencies = _require_list(raw_dependencies, f"{tx_id}.depends_on")
        depends: list[str] = []
        for dependency in raw_dependencies:
            if not isinstance(dependency, str) or not dependency:
                raise DiagnosticError(E_BAD_DEPENDENCY, "depends_on entries must be transaction ids")
            if dependency == tx_id:
                raise DiagnosticError(
                    E_TX_CYCLE, f"transaction {tx_id} depends on itself", {"tx_id": tx_id}
                )
            if dependency in depends:
                raise DiagnosticError(
                    E_BAD_DEPENDENCY,
                    f"duplicate dependency {dependency} in {tx_id}",
                    {"tx_id": tx_id, "dependency": dependency},
                )
            depends.append(dependency)
        dependencies[tx_id] = tuple(depends)
        raw_operations = _require_list(tx.get("operations"), f"{tx_id}.operations")
        if not raw_operations:
            raise DiagnosticError(E_BAD_OP, f"transaction {tx_id} has no operations")
        operations: list[Operation] = []
        saw_rollback = False
        for op_position, raw_operation in enumerate(raw_operations, start=1):
            op_mapping = _require_mapping(raw_operation, f"{tx_id}.operations[{op_position - 1}]")
            op_id = _require_str(op_mapping.get("op_id"), "op_id")
            if op_id in seen_ops:
                raise DiagnosticError(E_BAD_OPERATION_ORDER, f"duplicate operation id: {op_id}")
            seen_ops.add(op_id)
            operation = _validate_operation(tx_id, version, op_position, op_mapping, tables)
            if saw_rollback:
                raise DiagnosticError(
                    E_BAD_OPERATION_ORDER,
                    f"transaction {tx_id} contains operations after rollback",
                    {"tx_id": tx_id, "op_id": op_id},
                )
            if operation.kind == "rollback":
                saw_rollback = True
            operations.append(operation)
        result.append(
            Transaction(
                tx_id=tx_id,
                start_order=start_order,
                version=version,
                isolation=isolation,
                budget_ms=budget,
                depends_on=tuple(depends),
                operations=tuple(operations),
            )
        )

    for tx_id, deps in dependencies.items():
        for dependency in deps:
            if dependency not in seen_ids:
                raise DiagnosticError(
                    E_BAD_DEPENDENCY,
                    f"transaction {tx_id} depends on unknown transaction {dependency}",
                    {"tx_id": tx_id, "dependency": dependency},
                )

    visiting: set[str] = set()
    visited: set[str] = set()
    trail: list[str] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle_start = trail.index(node)
            cycle = trail[cycle_start:] + [node]
            raise DiagnosticError(
                E_TX_CYCLE,
                "transaction dependency cycle: " + " -> ".join(cycle),
                {"cycle": cycle},
            )
        visiting.add(node)
        trail.append(node)
        for dependency in dependencies[node]:
            visit(dependency)
        trail.pop()
        visiting.remove(node)
        visited.add(node)

    for tx_id in sorted(dependencies):
        visit(tx_id)
    return tuple(sorted(result, key=lambda item: (item.start_order, item.tx_id)))


def _validate_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise DiagnosticError(E_BAD_CLOCK, "config must be an object")
    max_concurrency = parse_positive_int(
        raw.get("max_concurrent_transactions"), E_BAD_CONCURRENCY, "max_concurrent_transactions"
    )
    workers = parse_positive_int(raw.get("worker_processes", 2), E_BAD_CONCURRENCY, "worker_processes")
    budget = parse_positive_int(
        raw.get("default_lock_wait_budget_ms"), E_BAD_BUDGET, "default_lock_wait_budget_ms"
    )
    scale = parse_positive_int(raw.get("fixed_scale"), E_BAD_SCALE, "fixed_scale")
    tolerance = parse_positive_decimal_ms(raw.get("wait_tolerance_ms"), "wait_tolerance_ms")
    period = parse_positive_int(
        raw.get("deadlock_check_period", 1), E_BAD_CLOCK, "deadlock_check_period"
    )
    steps = parse_positive_int(raw.get("max_scheduler_steps", 1000), E_BAD_CLOCK, "max_scheduler_steps")
    return Config(
        max_concurrent_transactions=max_concurrency,
        worker_processes=workers,
        default_lock_wait_budget_ms=budget,
        deadlock_check_period=period,
        fixed_scale=scale,
        wait_tolerance_ms=tolerance,
        max_scheduler_steps=steps,
        raw=raw,
    )


def _validate_clock(raw: dict) -> ClockSpec:
    if not isinstance(raw, dict):
        raise DiagnosticError(E_BAD_CLOCK, "clock must be an object")
    raw_per_ms = parse_positive_int(raw.get("raw_units_per_ms"), E_BAD_CLOCK, "raw_units_per_ms")
    step = parse_positive_int(raw.get("step_units"), E_BAD_CLOCK, "step_units")
    samples = _require_list(raw.get("samples"), "clock.samples", E_BAD_CLOCK)
    if not samples:
        raise DiagnosticError(E_BAD_CLOCK, "clock.samples must not be empty")
    parsed: list[int] = []
    previous: int | None = None
    for index, value in enumerate(samples):
        if isinstance(value, bool) or not isinstance(value, int):
            raise DiagnosticError(
                E_BAD_CLOCK, "clock samples must be integers", {"sample_index": index}
            )
        if previous is not None and value <= previous:
            raise DiagnosticError(
                E_BAD_CLOCK,
                "clock samples must be strictly increasing",
                {"sample_index": index},
            )
        previous = value
        parsed.append(value)
    return ClockSpec(raw_units_per_ms=raw_per_ms, step_units=step, samples=tuple(parsed))


def load_and_validate(sample_dir: str | Path) -> SampleBundle:
    """Load and validate a sample directory without writing diagnostic state."""

    sample = Path(sample_dir).resolve()
    manifest_path = sample / "manifest.json"
    manifest = _require_mapping(read_json(manifest_path), "manifest", E_BAD_JSON)
    schema_path = sample / str(manifest.get("schema", "schema.json"))
    transactions_path = sample / str(manifest.get("transactions", "transactions.json"))
    config_path = sample / str(manifest.get("config", "config.json"))
    clock_path = sample / str(manifest.get("clock", "clock.json"))
    db_path = sample / str(manifest.get("database", "sample.db"))

    schema = _require_mapping(read_json(schema_path), "schema", E_BAD_JSON)
    transactions_doc = _require_mapping(read_json(transactions_path), "transactions", E_BAD_JSON)
    config_raw = _require_mapping(read_json(config_path), "config", E_BAD_CLOCK)
    clock_raw = _require_mapping(read_json(clock_path), "clock", E_BAD_CLOCK)
    if not db_path.exists():
        raise DiagnosticError(E_SAMPLE_MISMATCH, f"sample database not found: {db_path}")

    db_tables, db_indexes = _table_columns(db_path)
    _validate_schema(schema, db_tables, db_indexes)
    config = _validate_config(config_raw)
    clock = _validate_clock(clock_raw)
    transactions = _validate_transactions(
        transactions_doc, db_tables, config.default_lock_wait_budget_ms
    )
    manifest_json = {
        "manifest": manifest,
        "schema": schema,
        "transactions": transactions_doc,
        "config": config_raw,
        "clock": clock_raw,
    }
    digest = fingerprint(manifest_json)
    return SampleBundle(
        sample_dir=sample,
        db_path=db_path,
        schema_path=schema_path,
        transactions_path=transactions_path,
        config_path=config_path,
        clock_path=clock_path,
        manifest=manifest,
        schema=schema,
        transactions=transactions,
        config=config,
        clock=clock,
        tables=db_tables,
        indexes=db_indexes,
        fingerprint=digest,
        manifest_json=manifest_json,
    )


def rehydrate_bundle(
    manifest_json: dict[str, Any], run_db_path: str | Path
) -> SampleBundle:
    """Rebuild a validated bundle from state metadata without needing the source tree."""

    schema = _require_mapping(manifest_json.get("schema"), "schema", E_BAD_JSON)
    transactions_doc = _require_mapping(
        manifest_json.get("transactions"), "transactions", E_BAD_JSON
    )
    config_raw = _require_mapping(manifest_json.get("config"), "config", E_BAD_CLOCK)
    clock_raw = _require_mapping(manifest_json.get("clock"), "clock", E_BAD_CLOCK)
    manifest = _require_mapping(manifest_json.get("manifest", {}), "manifest", E_BAD_JSON)
    db_tables, db_indexes = _table_columns(Path(run_db_path))
    _validate_schema(schema, db_tables, db_indexes)
    config = _validate_config(config_raw)
    clock = _validate_clock(clock_raw)
    transactions = _validate_transactions(
        transactions_doc, db_tables, config.default_lock_wait_budget_ms
    )
    digest = fingerprint(manifest_json)
    placeholder = Path(run_db_path).parent
    return SampleBundle(
        sample_dir=placeholder,
        db_path=Path(run_db_path),
        schema_path=placeholder / "schema.json",
        transactions_path=placeholder / "transactions.json",
        config_path=placeholder / "config.json",
        clock_path=placeholder / "clock.json",
        manifest=manifest,
        schema=schema,
        transactions=transactions,
        config=config,
        clock=clock,
        tables=db_tables,
        indexes=db_indexes,
        fingerprint=digest,
        manifest_json=manifest_json,
    )
