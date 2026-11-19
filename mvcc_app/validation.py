"""Validation and normalization for tables, sessions, and engine configuration."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .errors import BusinessError, ErrorCode
from .util import (
    canonical_json,
    parse_json_file,
    require_list,
    require_nonempty_str,
    require_nonnegative_int,
    require_object,
    require_positive_int,
)

ALLOWED_COLUMN_TYPES = frozenset({"integer", "real", "text", "boolean"})
ALLOWED_ISOLATIONS = frozenset({"read_committed", "repeatable_read"})
ALLOWED_OPERATION_TYPES = frozenset({"insert", "update", "delete", "range_read"})
ALLOWED_FILTER_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in"})
ALLOWED_OUTCOMES = frozenset({"committed", "rolled_back"})


def load_table(path: str) -> dict[str, Any]:
    return validate_table(parse_json_file(path, "table"))


def load_sessions(path: str, clock_step_ms: int = 10) -> dict[str, Any]:
    return validate_sessions(parse_json_file(path, "sessions"), clock_step_ms=clock_step_ms)


def load_config(path: str) -> dict[str, Any]:
    return validate_config(parse_json_file(path, "config"))


def validate_table(raw: Any) -> dict[str, Any]:
    table = require_object(raw, "table")
    table_name = table.get("table_name", table.get("table", table.get("name")))
    table_name = require_nonempty_str(table_name, "table.table_name", ErrorCode.INVALID_TABLE)
    columns_raw = require_list(table.get("columns"), "table.columns")
    if not columns_raw:
        raise BusinessError(
            ErrorCode.INVALID_SCHEMA,
            "table must contain at least one column",
            {"table": table_name},
        )

    columns: list[dict[str, Any]] = []
    names: set[str] = set()
    primary_keys = 0
    for index, raw_column in enumerate(columns_raw):
        column = require_object(raw_column, f"table.columns[{index}]")
        name = require_nonempty_str(
            column.get("name"),
            f"table.columns[{index}].name",
            ErrorCode.INVALID_SCHEMA,
        )
        if name in names:
            raise BusinessError(
                ErrorCode.DUPLICATE_COLUMN,
                "column names must be unique",
                {"column": name, "index": index},
            )
        names.add(name)
        column_type = column.get("type")
        if column_type not in ALLOWED_COLUMN_TYPES:
            raise BusinessError(
                ErrorCode.INVALID_COLUMN_TYPE,
                "column type is not in the closed enum",
                {
                    "column": name,
                    "type": column_type,
                    "allowed": sorted(ALLOWED_COLUMN_TYPES),
                },
            )
        pk = column.get("primary_key", False)
        nullable = column.get("nullable", True)
        if not isinstance(pk, bool) or not isinstance(nullable, bool):
            raise BusinessError(
                ErrorCode.INVALID_SCHEMA,
                "primary_key and nullable must be booleans",
                {"column": name},
            )
        if pk:
            primary_keys += 1
            nullable = False
        columns.append(
            {
                "name": name,
                "type": column_type,
                "nullable": nullable,
                "primary_key": pk,
            }
        )
    if primary_keys == 0:
        raise BusinessError(
            ErrorCode.INVALID_SCHEMA,
            "table must declare at least one primary-key column",
            {"table": table_name},
        )
    return {"table_name": table_name, "columns": columns}


def validate_config(raw: Any) -> dict[str, Any]:
    config = require_object(raw, "config", ErrorCode.INVALID_CONFIG)
    result = {
        "clock_step_ms": require_positive_int(
            config.get("clock_step_ms", 10),
            "config.clock_step_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "snapshot_retention": require_positive_int(
            config.get("snapshot_retention", 32),
            "config.snapshot_retention",
            ErrorCode.INVALID_CONFIG,
        ),
        "visibility_tolerance_ms": require_nonnegative_int(
            config.get("visibility_tolerance_ms", 0),
            "config.visibility_tolerance_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "gc_watermark_txn": require_positive_int(
            config.get("gc_watermark_txn", config.get("gc_watermark", 1_000_000)),
            "config.gc_watermark_txn",
            ErrorCode.INVALID_CONFIG,
        ),
        "query_latency_budget_ms": require_positive_int(
            config.get("query_latency_budget_ms", 100),
            "config.query_latency_budget_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "initial_clock_ms": require_nonnegative_int(
            config.get("initial_clock_ms", 0),
            "config.initial_clock_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "query_base_cost_ms": require_nonnegative_int(
            config.get("query_base_cost_ms", 1),
            "config.query_base_cost_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "query_version_cost_ms": require_nonnegative_int(
            config.get("query_version_cost_ms", 1),
            "config.query_version_cost_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "query_row_cost_ms": require_nonnegative_int(
            config.get("query_row_cost_ms", 1),
            "config.query_row_cost_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "edit_cost_ms": require_positive_int(
            config.get("edit_cost_ms", 1),
            "config.edit_cost_ms",
            ErrorCode.INVALID_CONFIG,
        ),
        "db_path": require_nonempty_str(
            config.get("db_path", "data/mvcc.db"),
            "config.db_path",
            ErrorCode.INVALID_CONFIG,
        ),
        "store_dir": require_nonempty_str(
            config.get("store_dir", "data/versions"),
            "config.store_dir",
            ErrorCode.INVALID_CONFIG,
        ),
    }
    return result


def validate_sessions(raw: Any, *, clock_step_ms: int = 10) -> dict[str, Any]:
    clock_step_ms = require_positive_int(
        clock_step_ms,
        "clock_step_ms",
        ErrorCode.INVALID_CONFIG,
    )
    if isinstance(raw, list):
        sessions_raw = raw
    else:
        obj = require_object(raw, "sessions")
        sessions_raw = require_list(obj.get("sessions"), "sessions.sessions")

    sessions: list[dict[str, Any]] = []
    seen_txn_ids: set[int] = set()
    previous_start = -1
    for session_index, raw_session in enumerate(sessions_raw):
        session = require_object(raw_session, f"sessions[{session_index}]")
        txn_id = require_positive_int(
            session.get("transaction_id", session.get("txn_id")),
            f"sessions[{session_index}].transaction_id",
            ErrorCode.INVALID_TXN,
        )
        if txn_id in seen_txn_ids:
            raise BusinessError(
                ErrorCode.INVALID_TXN,
                "transaction_id values must be unique",
                {"transaction_id": txn_id, "index": session_index},
            )
        seen_txn_ids.add(txn_id)
        start_clock = require_positive_int(
            session.get("start_clock_ms", session.get("start_clock")),
            f"sessions[{session_index}].start_clock_ms",
            ErrorCode.INVALID_CLOCK,
        )
        if start_clock < previous_start:
            raise BusinessError(
                ErrorCode.TXN_START_ORDER,
                "transaction start clocks must be non-decreasing",
                {"previous": previous_start, "current": start_clock, "index": session_index},
            )
        previous_start = start_clock
        isolation = session.get("isolation")
        if isolation not in ALLOWED_ISOLATIONS:
            raise BusinessError(
                ErrorCode.INVALID_ISOLATION,
                "isolation must be read_committed or repeatable_read",
                {
                    "transaction_id": txn_id,
                    "isolation": isolation,
                    "allowed": sorted(ALLOWED_ISOLATIONS),
                },
            )
        sessions.append(
            {
                "transaction_id": txn_id,
                "start_clock_ms": start_clock,
                "isolation": isolation,
                "start_order": session_index,
                "_raw_operations": session.get("operations", []),
                "_raw_outcome": session.get("outcome", "committed"),
                "_raw_commit_clock": session.get("commit_clock_ms", session.get("commit_clock")),
            }
        )

    table_columns = _table_columns_from_sessions_context(raw)
    for session in sessions:
        operations_raw = require_list(
            session.pop("_raw_operations"),
            f"transaction {session['transaction_id']} operations",
            ErrorCode.INVALID_OPERATION,
        )
        operations = _validate_operations(
            session["transaction_id"],
            session["start_clock_ms"],
            operations_raw,
            table_columns,
            clock_step_ms,
        )
        max_op_clock = max(
            (operation["clock_ms"] for operation in operations),
            default=session["start_clock_ms"],
        )
        outcome = session.pop("_raw_outcome")
        if outcome not in ALLOWED_OUTCOMES:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "session outcome must be committed or rolled_back",
                {"transaction_id": session["transaction_id"], "outcome": outcome},
            )
        raw_commit_clock = session.pop("_raw_commit_clock")
        commit_clock = (
            max_op_clock + clock_step_ms
            if raw_commit_clock is None
            else require_positive_int(
                raw_commit_clock,
                f"transaction {session['transaction_id']} commit_clock_ms",
                ErrorCode.INVALID_CLOCK,
            )
        )
        if commit_clock < session["start_clock_ms"] or commit_clock < max_op_clock:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK,
                "commit clock must not precede start or operation clocks",
                {
                    "transaction_id": session["transaction_id"],
                    "commit_clock_ms": commit_clock,
                    "max_op_clock_ms": max_op_clock,
                },
            )
        session["operations"] = operations
        session["outcome"] = outcome
        session["commit_clock_ms"] = commit_clock
    return {"sessions": sessions}


def _table_columns_from_sessions_context(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    table = raw.get("table")
    if not isinstance(table, dict):
        return None
    try:
        return validate_table(table)["columns"]
    except BusinessError:
        return None


def _validate_operations(
    txn_id: int,
    start_clock_ms: int,
    operations_raw: list[Any],
    table_columns: list[dict[str, Any]] | None,
    clock_step_ms: int,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    used_seq: set[int] = set()
    previous_clock = start_clock_ms
    for index, raw_operation in enumerate(operations_raw):
        operation = require_object(
            raw_operation,
            f"transaction {txn_id} operations[{index}]",
            ErrorCode.INVALID_OPERATION,
        )
        operation_type = operation.get("type")
        if operation_type not in ALLOWED_OPERATION_TYPES:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "operation type is not in the closed enum",
                {
                    "transaction_id": txn_id,
                    "index": index,
                    "type": operation_type,
                    "allowed": sorted(ALLOWED_OPERATION_TYPES),
                },
            )
        seq = operation.get("seq", index + 1)
        seq = require_positive_int(
            seq,
            f"transaction {txn_id} operations[{index}].seq",
            ErrorCode.INVALID_OPERATION,
        )
        if seq in used_seq:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "operation seq values must be unique within a transaction",
                {"transaction_id": txn_id, "seq": seq},
            )
        used_seq.add(seq)
        clock_ms = operation.get("clock_ms", start_clock_ms + clock_step_ms * (index + 1))
        clock_ms = require_positive_int(
            clock_ms,
            f"transaction {txn_id} operations[{index}].clock_ms",
            ErrorCode.INVALID_CLOCK,
        )
        if clock_ms < start_clock_ms:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK,
                "operation clock must not precede transaction start",
                {
                    "transaction_id": txn_id,
                    "index": index,
                    "start_clock_ms": start_clock_ms,
                    "clock_ms": clock_ms,
                },
            )
        if clock_ms < previous_clock:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK,
                "operation clocks must be non-decreasing within a transaction",
                {
                    "transaction_id": txn_id,
                    "index": index,
                    "previous": previous_clock,
                    "clock_ms": clock_ms,
                },
            )
        previous_clock = clock_ms
        normalized: dict[str, Any] = {
            "seq": seq,
            "type": operation_type,
            "clock_ms": clock_ms,
        }
        if operation_type == "insert":
            row = require_object(
                operation.get("row"),
                f"transaction {txn_id} insert row",
                ErrorCode.INVALID_OPERATION,
            )
            normalized["row"] = _validate_row(
                row,
                table_columns,
                txn_id,
                require_complete=False,
            )
        elif operation_type == "update":
            key = require_object(
                operation.get("key"),
                f"transaction {txn_id} update key",
                ErrorCode.INVALID_OPERATION,
            )
            changes = require_object(
                operation.get("changes"),
                f"transaction {txn_id} update changes",
                ErrorCode.INVALID_OPERATION,
            )
            if not changes:
                raise BusinessError(
                    ErrorCode.INVALID_OPERATION,
                    "update changes must not be empty",
                    {"transaction_id": txn_id, "seq": seq},
                )
            normalized["key"] = _validate_key(key, table_columns, txn_id)
            normalized["changes"] = _validate_changes(changes, table_columns, txn_id)
        elif operation_type == "delete":
            key = require_object(
                operation.get("key"),
                f"transaction {txn_id} delete key",
                ErrorCode.INVALID_OPERATION,
            )
            normalized["key"] = _validate_key(key, table_columns, txn_id)
        else:
            normalized.update(_validate_range_read(operation, table_columns, txn_id, seq))
        operations.append(normalized)
    return operations


def _validate_row(
    row: dict[str, Any],
    columns: list[dict[str, Any]] | None,
    txn_id: int,
    *,
    require_complete: bool,
) -> dict[str, Any]:
    if columns is None:
        return deepcopy(row)
    by_name = {column["name"]: column for column in columns}
    unknown = sorted(set(row) - set(by_name))
    if unknown:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            "row contains unknown columns",
            {"transaction_id": txn_id, "columns": unknown},
        )
    normalized = deepcopy(row)
    for column in columns:
        name = column["name"]
        if name not in normalized:
            if column["primary_key"] or not column["nullable"]:
                raise BusinessError(
                    ErrorCode.INVALID_VALUE,
                    "required column is missing",
                    {"transaction_id": txn_id, "column": name},
                )
            normalized[name] = None
        _validate_value(name, normalized[name], column, txn_id)
    if require_complete:
        missing = sorted(set(by_name) - set(normalized))
        if missing:
            raise BusinessError(
                ErrorCode.INVALID_VALUE,
                "row is missing columns",
                {"transaction_id": txn_id, "columns": missing},
            )
    return normalized


def _validate_key(
    key: dict[str, Any],
    columns: list[dict[str, Any]] | None,
    txn_id: int,
) -> dict[str, Any]:
    if columns is None:
        return deepcopy(key)
    by_name = {column["name"]: column for column in columns}
    pk_names = [column["name"] for column in columns if column["primary_key"]]
    unknown = sorted(set(key) - set(pk_names))
    missing = sorted(set(pk_names) - set(key))
    if unknown or missing:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            "row key must contain exactly the primary-key columns",
            {
                "transaction_id": txn_id,
                "unknown": unknown,
                "missing": missing,
                "primary_key": pk_names,
            },
        )
    normalized: dict[str, Any] = {}
    for name in pk_names:
        _validate_value(name, key[name], by_name[name], txn_id)
        normalized[name] = key[name]
    return normalized


def _validate_changes(
    changes: dict[str, Any],
    columns: list[dict[str, Any]] | None,
    txn_id: int,
) -> dict[str, Any]:
    if columns is None:
        return deepcopy(changes)
    by_name = {column["name"]: column for column in columns}
    unknown = sorted(set(changes) - set(by_name))
    if unknown:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            "update changes contain unknown columns",
            {"transaction_id": txn_id, "columns": unknown},
        )
    for name, value in changes.items():
        if by_name[name]["primary_key"]:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "primary-key columns cannot be updated",
                {"transaction_id": txn_id, "column": name},
            )
        _validate_value(name, value, by_name[name], txn_id)
    return deepcopy(changes)


def _validate_range_read(
    operation: dict[str, Any],
    columns: list[dict[str, Any]] | None,
    txn_id: int,
    seq: int,
) -> dict[str, Any]:
    where_raw = operation.get("where", [])
    if isinstance(where_raw, dict):
        where_raw = [
            {"column": name, "op": "eq", "value": value}
            for name, value in where_raw.items()
        ]
    where = require_list(
        where_raw,
        f"transaction {txn_id} range_read where",
        ErrorCode.INVALID_OPERATION,
    )
    conditions: list[dict[str, Any]] = []
    by_name = {column["name"]: column for column in columns or []}
    for condition_index, raw_condition in enumerate(where):
        condition = require_object(
            raw_condition,
            f"transaction {txn_id} range_read where[{condition_index}]",
            ErrorCode.INVALID_OPERATION,
        )
        column_name = condition.get("column")
        if columns is not None and column_name not in by_name:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "range-read column does not exist",
                {"transaction_id": txn_id, "column": column_name},
            )
        op = condition.get("op", "eq")
        if op not in ALLOWED_FILTER_OPS:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "range-read filter operator is not supported",
                {"transaction_id": txn_id, "op": op, "allowed": sorted(ALLOWED_FILTER_OPS)},
            )
        value = condition.get("value")
        if columns is not None:
            if op == "in":
                values = require_list(
                    condition.get("values", value),
                    "range_read in-values",
                    ErrorCode.INVALID_OPERATION,
                )
                for item in values:
                    _validate_value(column_name, item, by_name[column_name], txn_id)
                value = deepcopy(values)
            else:
                _validate_value(column_name, value, by_name[column_name], txn_id)
        conditions.append(
            {
                "column": column_name,
                "op": op,
                "value": deepcopy(value),
            }
        )
    order_by = operation.get("order_by")
    if order_by is None:
        order_by = [column["name"] for column in columns or [] if column["primary_key"]]
    order_by = require_list(order_by, "range_read order_by", ErrorCode.INVALID_OPERATION)
    normalized_order: list[str] = []
    for column_name in order_by:
        column_name = require_nonempty_str(
            column_name,
            "range_read order_by column",
            ErrorCode.INVALID_OPERATION,
        )
        if columns is not None and column_name not in by_name:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "order_by column does not exist",
                {"transaction_id": txn_id, "column": column_name},
            )
        if column_name not in normalized_order:
            normalized_order.append(column_name)
    if columns is not None:
        for column in columns:
            if column["primary_key"] and column["name"] not in normalized_order:
                normalized_order.append(column["name"])
    limit = operation.get("limit")
    if limit is not None:
        limit = require_positive_int(
            limit,
            "range_read limit",
            ErrorCode.INVALID_OPERATION,
        )
    read_id = operation.get("read_id", operation.get("name", f"{txn_id}:read:{seq}"))
    read_id = require_nonempty_str(read_id, "range_read read_id", ErrorCode.INVALID_OPERATION)
    return {
        "range": {
            "read_id": read_id,
            "where": conditions,
            "order_by": normalized_order,
            "limit": limit,
        }
    }


def _validate_value(
    name: str,
    value: Any,
    column: dict[str, Any],
    txn_id: int,
) -> None:
    if value is None:
        if not column["nullable"]:
            raise BusinessError(
                ErrorCode.INVALID_VALUE,
                "value violates the column nullability",
                {"transaction_id": txn_id, "column": name},
            )
        return
    column_type = column["type"]
    valid = False
    if column_type == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif column_type == "real":
        if isinstance(value, int) and not isinstance(value, bool):
            valid = True
        else:
            valid = isinstance(value, float) and math.isfinite(value)
    elif column_type == "text":
        valid = isinstance(value, str)
    elif column_type == "boolean":
        valid = isinstance(value, bool)
    if not valid:
        raise BusinessError(
            ErrorCode.INVALID_VALUE,
            "value does not match the column type",
            {
                "transaction_id": txn_id,
                "column": name,
                "expected": column_type,
                "actual": type(value).__name__,
            },
        )


def session_fingerprint(sessions: dict[str, Any]) -> str:
    return canonical_json(sessions)


def validate_operation(
    raw: Any,
    table: dict[str, Any],
    *,
    txn_id: int = 1,
    start_clock_ms: int = 1,
    clock_step_ms: int = 1,
) -> dict[str, Any]:
    return _validate_operations(
        txn_id,
        start_clock_ms,
        [raw],
        table["columns"],
        clock_step_ms,
    )[0]


def validate_range_spec(
    raw: Any,
    table: dict[str, Any],
    *,
    txn_id: int = 1,
    seq: int = 1,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "seq": seq,
        "clock_ms": 1,
        "type": "range_read",
    }
    if raw is not None:
        raw_object = require_object(raw, "range", ErrorCode.INVALID_OPERATION)
        if raw_object.get("type", "range_read") != "range_read":
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "read range must describe a range_read operation",
                {"type": raw_object.get("type")},
            )
        for key, value in raw_object.items():
            if key not in {"type", "clock_ms", "seq"}:
                operation[key] = value
    normalized = validate_operation(operation, table, txn_id=txn_id, clock_step_ms=1)
    return normalized["range"]

