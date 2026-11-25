"""Fixed-seed sample loading, validation, and policy resolution."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    read_json,
    read_jsonl,
    sha256_value,
    validate_scale,
)
from .errors import ErrorCode, LabError, require


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ISOLATION_LEVELS = {
    "READ_COMMITTED",
    "REPEATABLE_READ",
    "SERIALIZABLE",
}
COLUMN_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB"}
SUPPORTED_SCOPES = ("resource:", "tx:", "table:", "default")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_int(
    value: Any,
    *,
    field: str,
    path: str,
    minimum: int | None = None,
    maximum: int | None = None,
    error_code: str = ErrorCode.SCHEMA_INVALID,
) -> int:
    require(
        _is_int(value),
        error_code,
        "value must be an integer",
        field=field,
        path=path,
    )
    assert isinstance(value, int)
    if minimum is not None:
        require(
            value >= minimum,
            error_code,
            f"value must be >= {minimum}",
            field=field,
            path=path,
        )
    if maximum is not None:
        require(
            value <= maximum,
            error_code,
            f"value must be <= {maximum}",
            field=field,
            path=path,
        )
    return value


def _validate_bool(value: Any, *, field: str, path: str) -> bool:
    require(
        isinstance(value, bool),
        ErrorCode.SCHEMA_INVALID,
        "value must be a boolean",
        field=field,
        path=path,
    )
    assert isinstance(value, bool)
    return value


def _validate_nonempty_string(value: Any, *, field: str, path: str) -> str:
    require(
        isinstance(value, str) and bool(value),
        ErrorCode.SCHEMA_INVALID,
        "value must be a non-empty string",
        field=field,
        path=path,
    )
    assert isinstance(value, str)
    return value


@dataclass
class PolicyView:
    generation: int
    effective_ms: int
    summary: str
    scale: int
    tolerance_ns: int
    default_budget_ms: int
    entries: dict[str, dict[str, Any]]
    retry_multiplier: int

    def resolve(self, tx_id: str, table: str, resource: str) -> tuple[int, int]:
        candidates = (f"resource:{resource}", f"tx:{tx_id}", f"table:{table}")
        for scope in candidates:
            entry = self.entries.get(scope)
            if entry is not None and not entry["tombstone"]:
                return entry["budget_ms"], entry["retry_multiplier"]
        default = self.entries.get("default")
        if default is not None and not default["tombstone"]:
            return default["budget_ms"], default["retry_multiplier"]
        return self.default_budget_ms, self.retry_multiplier


@dataclass
class SampleBundle:
    root: Path
    database: dict[str, Any]
    transactions: list[dict[str, Any]]
    clock: dict[str, Any]
    policy_v1: dict[str, Any]
    policy_v2: dict[str, Any]
    policy_shards: list[dict[str, Any]]
    policy_views: dict[int, PolicyView]
    fingerprint: str
    sample_sqlite: Path
    policy_sqlite: Path
    tables: dict[str, dict[str, Any]] = field(init=False)
    rows: dict[str, list[dict[str, Any]]] = field(init=False)
    transactions_by_id: dict[str, dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.tables = {table["name"]: table for table in self.database["tables"]}
        self.rows = {
            table["name"]: list(table["rows"]) for table in self.database["tables"]
        }
        self.transactions_by_id = {
            transaction["tx_id"]: transaction for transaction in self.transactions
        }

    def table(self, name: str) -> dict[str, Any]:
        return self.tables[name]

    def find_row(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        table_def = self.tables[table]
        primary_key = table_def["primary_key"]
        for row in self.rows[table]:
            if all(row[column] == key[column] for column in primary_key):
                return row
        return None

    def policy(self, generation: int) -> PolicyView:
        require(
            generation in self.policy_views,
            ErrorCode.POLICY_V2_INVALID,
            f"unknown policy generation {generation}",
            field="generation",
        )
        return self.policy_views[generation]

    def summary(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "seed": self.clock["seed"],
            "database": {
                "name": self.database["database"],
                "tables": len(self.database["tables"]),
                "columns": sum(
                    len(table["columns"]) for table in self.database["tables"]
                ),
                "rows": sum(len(table["rows"]) for table in self.database["tables"]),
                "indexes": sum(
                    len(table.get("indexes", []))
                    for table in self.database["tables"]
                ),
            },
            "transactions": {
                "count": len(self.transactions),
                "operations": sum(
                    len(transaction["operations"])
                    for transaction in self.transactions
                ),
                "isolation_levels": sorted(
                    {transaction["isolation"] for transaction in self.transactions}
                ),
            },
            "policies": [
                {
                    "generation": view.generation,
                    "effective_ms": view.effective_ms,
                    "summary": view.summary,
                    "scale": view.scale,
                    "tolerance_ns": view.tolerance_ns,
                    "active_entries": sum(
                        1
                        for entry in view.entries.values()
                        if not entry["tombstone"]
                    ),
                    "tombstones": sum(
                        1
                        for entry in view.entries.values()
                        if entry["tombstone"]
                    ),
                }
                for view in sorted(
                    self.policy_views.values(), key=lambda item: item.generation
                )
            ],
            "clock": {
                "logical_unit": self.clock["logical_unit"],
                "max_logical_ns": self.clock["max_logical_ms"] * 1_000_000,
                "parallelism": self.clock["parallelism"],
                "round_budget_ns": self.clock["round_budget_ms"] * 1_000_000,
                "policy_switch_at_ns": self.clock["policy_switch_at_ms"]
                * 1_000_000,
            },
        }


def _validate_table_name(name: Any, path: str, field_name: str = "name") -> str:
    require(
        isinstance(name, str) and bool(IDENTIFIER.fullmatch(name)),
        ErrorCode.SCHEMA_INVALID,
        "table name must match [A-Za-z_][A-Za-z0-9_]*",
        field=field_name,
        path=path,
    )
    assert isinstance(name, str)
    return name


def _column_type_matches(column_type: str, value: Any) -> bool:
    if value is None:
        return True
    if column_type == "INTEGER":
        return _is_int(value)
    if column_type == "TEXT":
        return isinstance(value, str)
    if column_type == "REAL":
        return (isinstance(value, (int, float)) and not isinstance(value, bool))
    if column_type == "BLOB":
        return isinstance(value, bytes)
    return False


def _row_tuple(table: dict[str, Any], row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in table["primary_key"])


def _validate_database(database: Any, root: Path) -> None:
    path = str(root / "sample_db.json")
    require(
        isinstance(database, dict),
        ErrorCode.SCHEMA_INVALID,
        "database description must be an object",
        path=path,
    )
    assert isinstance(database, dict)
    require(
        database.get("format_version") == 1,
        ErrorCode.SCHEMA_INVALID,
        "database format_version must be 1",
        field="format_version",
        path=path,
    )
    _validate_nonempty_string(database.get("database"), field="database", path=path)
    ledger = database.get("ledger")
    require(
        isinstance(ledger, dict),
        ErrorCode.SCHEMA_INVALID,
        "database ledger metadata must be an object",
        field="ledger",
        path=path,
    )
    assert isinstance(ledger, dict)
    require(
        ledger.get("duration_unit") == "nanosecond",
        ErrorCode.SCHEMA_INVALID,
        "duration_unit must be nanosecond",
        field="ledger.duration_unit",
        path=path,
    )
    validate_scale(ledger.get("duration_scale"), "ledger.duration_scale", path)
    require(
        ledger.get("rounding") == "HALF_UP",
        ErrorCode.SCHEMA_INVALID,
        "rounding must be HALF_UP",
        field="ledger.rounding",
        path=path,
    )
    tables = database.get("tables")
    require(
        isinstance(tables, list) and bool(tables),
        ErrorCode.SCHEMA_INVALID,
        "tables must be a non-empty array",
        field="tables",
        path=path,
    )
    assert isinstance(tables, list)

    table_names: set[str] = set()
    column_types: dict[str, dict[str, str]] = {}
    primary_keys: dict[str, list[str]] = {}
    unique_column_sets: dict[str, set[tuple[str, ...]]] = {}
    index_names: set[str] = set()
    parsed_tables: dict[str, dict[str, Any]] = {}

    for table_index, table in enumerate(tables):
        table_path = f"{path}#tables[{table_index}]"
        require(
            isinstance(table, dict),
            ErrorCode.SCHEMA_INVALID,
            "table must be an object",
            field=f"tables[{table_index}]",
            path=path,
        )
        assert isinstance(table, dict)
        name = _validate_table_name(table.get("name"), table_path)
        if name in table_names:
            raise LabError(
                ErrorCode.DB_DUPLICATE_TABLE,
                f"duplicate table name {name!r}",
                field=f"tables[{table_index}].name",
                path=path,
            )
        table_names.add(name)
        parsed_tables[name] = table
        columns = table.get("columns")
        require(
            isinstance(columns, list) and bool(columns),
            ErrorCode.SCHEMA_INVALID,
            "columns must be a non-empty array",
            field=f"tables[{table_index}].columns",
            path=path,
        )
        assert isinstance(columns, list)
        column_names: list[str] = []
        this_types: dict[str, str] = {}
        for column_index, column in enumerate(columns):
            column_path = f"{table_path}.columns[{column_index}]"
            require(
                isinstance(column, dict),
                ErrorCode.SCHEMA_INVALID,
                "column must be an object",
                field=f"tables[{table_index}].columns[{column_index}]",
                path=path,
            )
            assert isinstance(column, dict)
            column_name = column.get("name")
            require(
                isinstance(column_name, str) and bool(IDENTIFIER.fullmatch(column_name)),
                ErrorCode.DB_COLUMN_INVALID,
                "column name must match [A-Za-z_][A-Za-z0-9_]*",
                field=f"tables[{table_index}].columns[{column_index}].name",
                path=path,
            )
            assert isinstance(column_name, str)
            if column_name in this_types:
                raise LabError(
                    ErrorCode.DB_DUPLICATE_COLUMN,
                    f"duplicate column {column_name!r} in table {name!r}",
                    field=f"tables[{table_index}].columns[{column_index}].name",
                    path=path,
                )
            column_type = column.get("type")
            require(
                isinstance(column_type, str) and column_type in COLUMN_TYPES,
                ErrorCode.DB_COLUMN_INVALID,
                "column type must be INTEGER, TEXT, REAL or BLOB",
                field=f"tables[{table_index}].columns[{column_index}].type",
                path=path,
            )
            require(
                isinstance(column.get("nullable"), bool),
                ErrorCode.DB_COLUMN_INVALID,
                "column nullable must be boolean",
                field=f"tables[{table_index}].columns[{column_index}].nullable",
                path=path,
            )
            column_names.append(column_name)
            this_types[column_name] = column_type
        column_types[name] = this_types
        for column_name in column_names:
            require(
                IDENTIFIER.fullmatch(column_name) is not None,
                ErrorCode.DB_COLUMN_INVALID,
                "invalid column name",
                field=f"tables[{table_index}].columns",
                path=path,
            )

        primary_key = table.get("primary_key")
        require(
            isinstance(primary_key, list) and bool(primary_key),
            ErrorCode.DB_KEY_INVALID,
            "primary_key must be a non-empty array",
            field=f"tables[{table_index}].primary_key",
            path=path,
        )
        assert isinstance(primary_key, list)
        require(
            all(
                isinstance(column_name, str)
                and bool(IDENTIFIER.fullmatch(column_name))
                for column_name in primary_key
            ),
            ErrorCode.DB_KEY_INVALID,
            "primary_key entries must be valid identifiers",
            field=f"tables[{table_index}].primary_key",
            path=path,
        )
        require(
            len(primary_key) == len(set(primary_key)),
            ErrorCode.DB_KEY_INVALID,
            "primary_key columns must be unique",
            field=f"tables[{table_index}].primary_key",
            path=path,
        )
        for column_name in primary_key:
            require(
                column_name in this_types,
                ErrorCode.DB_KEY_INVALID,
                f"primary key references unknown column {column_name!r}",
                field=f"tables[{table_index}].primary_key",
                path=path,
            )
        primary_keys[name] = list(primary_key)
        unique_column_sets[name] = {tuple(primary_key)}

        indexes = table.get("indexes", [])
        require(
            isinstance(indexes, list),
            ErrorCode.SCHEMA_INVALID,
            "indexes must be an array",
            field=f"tables[{table_index}].indexes",
            path=path,
        )
        for index_index, index in enumerate(indexes):
            require(
                isinstance(index, dict),
                ErrorCode.SCHEMA_INVALID,
                "index must be an object",
                field=f"tables[{table_index}].indexes[{index_index}]",
                path=path,
            )
            assert isinstance(index, dict)
            index_name = index.get("name")
            require(
                isinstance(index_name, str) and bool(IDENTIFIER.fullmatch(index_name)),
                ErrorCode.DB_INDEX_INVALID,
                "index name must be a valid identifier",
                field=f"tables[{table_index}].indexes[{index_index}].name",
                path=path,
            )
            assert isinstance(index_name, str)
            if index_name in index_names:
                raise LabError(
                    ErrorCode.DB_INDEX_INVALID,
                    f"duplicate index name {index_name!r}",
                    field=f"tables[{table_index}].indexes[{index_index}].name",
                    path=path,
                )
            index_names.add(index_name)
            index_columns = index.get("columns")
            require(
                isinstance(index_columns, list) and bool(index_columns),
                ErrorCode.DB_INDEX_INVALID,
                "index columns must be a non-empty array",
                field=f"tables[{table_index}].indexes[{index_index}].columns",
                path=path,
            )
            assert isinstance(index_columns, list)
            require(
                all(
                    isinstance(column_name, str)
                    and bool(IDENTIFIER.fullmatch(column_name))
                    for column_name in index_columns
                ),
                ErrorCode.DB_INDEX_INVALID,
                "index columns must be valid identifiers",
                field=f"tables[{table_index}].indexes[{index_index}].columns",
                path=path,
            )
            require(
                len(index_columns) == len(set(index_columns)),
                ErrorCode.DB_INDEX_INVALID,
                "index columns must be unique",
                field=f"tables[{table_index}].indexes[{index_index}].columns",
                path=path,
            )
            for column_name in index_columns:
                require(
                    column_name in this_types,
                    ErrorCode.DB_INDEX_INVALID,
                    f"index references unknown column {column_name!r}",
                    field=f"tables[{table_index}].indexes[{index_index}].columns",
                    path=path,
                )
            require(
                isinstance(index.get("unique"), bool),
                ErrorCode.DB_INDEX_INVALID,
                "index unique must be boolean",
                field=f"tables[{table_index}].indexes[{index_index}].unique",
                path=path,
            )
            if index["unique"]:
                unique_column_sets[name].add(tuple(index_columns))

    for table_index, table in enumerate(tables):
        name = table["name"]
        foreign_keys = table.get("foreign_keys", [])
        require(
            isinstance(foreign_keys, list),
            ErrorCode.SCHEMA_INVALID,
            "foreign_keys must be an array",
            field=f"tables[{table_index}].foreign_keys",
            path=path,
        )
        for fk_index, foreign_key in enumerate(foreign_keys):
            require(
                isinstance(foreign_key, dict),
                ErrorCode.SCHEMA_INVALID,
                "foreign key must be an object",
                field=f"tables[{table_index}].foreign_keys[{fk_index}]",
                path=path,
            )
            assert isinstance(foreign_key, dict)
            local_columns = foreign_key.get("columns")
            require(
                isinstance(local_columns, list) and bool(local_columns),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key columns must be a non-empty array",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].columns",
                path=path,
            )
            assert isinstance(local_columns, list)
            require(
                all(
                    isinstance(column_name, str)
                    and bool(IDENTIFIER.fullmatch(column_name))
                    for column_name in local_columns
                ),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key columns must be valid identifiers",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].columns",
                path=path,
            )
            for column_name in local_columns:
                require(
                    column_name in column_types[name],
                    ErrorCode.DB_FOREIGN_KEY_INVALID,
                    f"foreign key references unknown local column {column_name!r}",
                    field=f"tables[{table_index}].foreign_keys[{fk_index}].columns",
                    path=path,
                )
            references = foreign_key.get("references")
            require(
                isinstance(references, dict),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key references must be an object",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].references",
                path=path,
            )
            assert isinstance(references, dict)
            target_table = references.get("table")
            require(
                isinstance(target_table, str) and target_table in table_names,
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                f"foreign key references unknown table {target_table!r}",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].references.table",
                path=path,
            )
            target_columns = references.get("columns")
            require(
                isinstance(target_columns, list) and bool(target_columns),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key target columns must be a non-empty array",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].references.columns",
                path=path,
            )
            assert isinstance(target_columns, list)
            require(
                all(
                    isinstance(column_name, str)
                    and bool(IDENTIFIER.fullmatch(column_name))
                    for column_name in target_columns
                ),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key target columns must be valid identifiers",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].references.columns",
                path=path,
            )
            require(
                len(target_columns) == len(local_columns),
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key local and target column counts must match",
                field=f"tables[{table_index}].foreign_keys[{fk_index}]",
                path=path,
            )
            for column_name in target_columns:
                require(
                    column_name in column_types[target_table],
                    ErrorCode.DB_FOREIGN_KEY_INVALID,
                    f"foreign key target references unknown column {column_name!r}",
                    field=f"tables[{table_index}].foreign_keys[{fk_index}].references.columns",
                    path=path,
                )
            require(
                tuple(target_columns) in unique_column_sets[target_table],
                ErrorCode.DB_FOREIGN_KEY_INVALID,
                "foreign key target columns must be a primary key or unique index",
                field=f"tables[{table_index}].foreign_keys[{fk_index}].references.columns",
                path=path,
            )

    row_index: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for table_index, table in enumerate(tables):
        name = table["name"]
        rows = table.get("rows")
        require(
            isinstance(rows, list),
            ErrorCode.DB_DATA_INVALID,
            "rows must be an array",
            field=f"tables[{table_index}].rows",
            path=path,
        )
        assert isinstance(rows, list)
        expected_columns = set(column_types[name])
        seen_keys: set[tuple[Any, ...]] = set()
        row_index[name] = {}
        unique_values: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            unique_columns: set() for unique_columns in unique_column_sets[name]
        }
        for row_index_value, row in enumerate(rows):
            require(
                isinstance(row, dict),
                ErrorCode.DB_DATA_INVALID,
                "row must be an object",
                field=f"tables[{table_index}].rows[{row_index_value}]",
                path=path,
            )
            assert isinstance(row, dict)
            require(
                set(row) == expected_columns,
                ErrorCode.DB_DATA_INVALID,
                "row columns must exactly match the table description",
                field=f"tables[{table_index}].rows[{row_index_value}]",
                path=path,
            )
            for column in table["columns"]:
                value = row[column["name"]]
                require(
                    _column_type_matches(column["type"], value),
                    ErrorCode.DB_DATA_INVALID,
                    f"row value for {column['name']!r} does not match {column['type']}",
                    field=f"tables[{table_index}].rows[{row_index_value}].{column['name']}",
                    path=path,
                )
                if not column["nullable"]:
                    require(
                        value is not None,
                        ErrorCode.DB_DATA_INVALID,
                        f"non-nullable column {column['name']!r} is null",
                        field=f"tables[{table_index}].rows[{row_index_value}].{column['name']}",
                        path=path,
                    )
            primary_key = tuple(primary_keys[name])
            key = tuple(row[column] for column in primary_key)
            require(
                key not in seen_keys,
                ErrorCode.DB_KEY_INVALID,
                "duplicate primary key row",
                field=f"tables[{table_index}].rows[{row_index_value}]",
                path=path,
            )
            seen_keys.add(key)
            row_index[name][key] = row
            for unique_columns, values in unique_values.items():
                unique_key = tuple(row[column] for column in unique_columns)
                if all(value is not None for value in unique_key):
                    require(
                        unique_key not in values,
                        ErrorCode.DB_KEY_INVALID,
                        f"duplicate unique value for {unique_columns}",
                        field=f"tables[{table_index}].rows[{row_index_value}]",
                        path=path,
                    )
                    values.add(unique_key)

    unique_target_values: dict[
        tuple[str, tuple[str, ...]], set[tuple[Any, ...]]
    ] = {}
    for table in tables:
        name = table["name"]
        for unique_columns in unique_column_sets[name]:
            unique_target_values[(name, unique_columns)] = {
                tuple(row[column] for column in unique_columns)
                for row in table["rows"]
            }

    for table_index, table in enumerate(tables):
        name = table["name"]
        for fk_index, foreign_key in enumerate(table.get("foreign_keys", [])):
            target = foreign_key["references"]
            local_columns = foreign_key["columns"]
            target_columns = target["columns"]
            for row_index_value, row in enumerate(table["rows"]):
                local_values = tuple(row[column] for column in local_columns)
                if any(value is None for value in local_values):
                    continue
                require(
                    local_values
                    in unique_target_values[
                        (target["table"], tuple(target_columns))
                    ],
                    ErrorCode.DB_FOREIGN_KEY_INVALID,
                    "foreign key value has no matching target row",
                    field=(
                        f"tables[{table_index}].rows[{row_index_value}]."
                        + ",".join(local_columns)
                    ),
                    path=path,
                )

    sqlite_path = root / "sample.sqlite"
    require(
        sqlite_path.exists(),
        ErrorCode.SAMPLE_MISSING,
        "sample SQLite database is missing",
        path=str(sqlite_path),
    )
    try:
        connection = sqlite3.connect(sqlite_path)
    except sqlite3.Error as exc:
        raise LabError(
            ErrorCode.DB_SQLITE_MISMATCH,
            f"cannot open sample SQLite database: {exc}",
            path=str(sqlite_path),
        ) from exc
    try:
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        require(
            actual_tables == set(table_names),
            ErrorCode.DB_SQLITE_MISMATCH,
            "SQLite table set does not match sample_db.json",
            field="tables",
            path=str(sqlite_path),
            details={
                "expected": sorted(table_names),
                "actual": sorted(actual_tables),
            },
        )
        for table in tables:
            name = table["name"]
            sqlite_rows = connection.execute(
                f'SELECT * FROM "{name}"'
            ).fetchall()
            table_info = list(
                connection.execute(f'PRAGMA table_info("{name}")')
            )
            sqlite_columns = [row[1] for row in table_info]
            expected_columns = [column["name"] for column in table["columns"]]
            require(
                sqlite_columns == expected_columns,
                ErrorCode.DB_SQLITE_MISMATCH,
                "SQLite column order does not match sample_db.json",
                field=f"{name}.columns",
                path=str(sqlite_path),
            )
            expected_column_meta = [
                (
                    column["name"],
                    column["type"],
                    0 if column["nullable"] else 1,
                )
                for column in table["columns"]
            ]
            actual_column_meta = [
                (row[1], row[2], row[3]) for row in table_info
            ]
            require(
                actual_column_meta == expected_column_meta,
                ErrorCode.DB_SQLITE_MISMATCH,
                "SQLite column types/nullability do not match sample_db.json",
                field=f"{name}.columns",
                path=str(sqlite_path),
            )
            expected_rows = sorted(
                (
                    tuple(row[column] for column in expected_columns)
                    for row in table["rows"]
                ),
                key=lambda item: repr(item),
            )
            actual_rows = sorted(sqlite_rows, key=lambda item: repr(item))
            require(
                actual_rows == expected_rows,
                ErrorCode.DB_SQLITE_MISMATCH,
                "SQLite rows do not match sample_db.json",
                field=f"{name}.rows",
                path=str(sqlite_path),
            )
            described_indexes = {
                index["name"]: (
                    bool(index["unique"]),
                    tuple(index["columns"]),
                )
                for index in table.get("indexes", [])
            }
            actual_indexes: dict[str, tuple[bool, tuple[str, ...]]] = {}
            for index_row in connection.execute(f'PRAGMA index_list("{name}")'):
                index_name = index_row[1]
                if index_name.startswith("sqlite_autoindex"):
                    continue
                index_columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    )
                )
                actual_indexes[index_name] = (bool(index_row[2]), index_columns)
            require(
                actual_indexes == described_indexes,
                ErrorCode.DB_SQLITE_MISMATCH,
                "SQLite indexes do not match sample_db.json",
                field=f"{name}.indexes",
                path=str(sqlite_path),
            )
    finally:
        connection.close()


def _validate_transactions(
    raw_transactions: list[Any],
    database: dict[str, Any],
    root: Path,
) -> list[dict[str, Any]]:
    path = str(root / "transactions.jsonl")
    tables = {table["name"]: table for table in database["tables"]}
    require(
        bool(raw_transactions),
        ErrorCode.TX_FIELD_INVALID,
        "transaction stream must not be empty",
        field="transactions",
        path=path,
    )
    seen_tx_ids: set[str] = set()
    seen_op_ids: set[str] = set()
    transactions: list[dict[str, Any]] = []
    for line_index, tx in enumerate(raw_transactions):
        tx_path = f"{path}#line{line_index + 1}"
        require(
            isinstance(tx, dict),
            ErrorCode.TX_FIELD_INVALID,
            "transaction record must be an object",
            field=f"transactions[{line_index}]",
            path=path,
        )
        assert isinstance(tx, dict)
        require(
            tx.get("format_version") == 1,
            ErrorCode.TX_FIELD_INVALID,
            "transaction format_version must be 1",
            field="format_version",
            path=tx_path,
        )
        tx_id = tx.get("tx_id")
        require(
            isinstance(tx_id, str) and bool(IDENTIFIER.fullmatch(tx_id)),
            ErrorCode.TX_FIELD_INVALID,
            "tx_id must be a valid identifier",
            field="tx_id",
            path=tx_path,
        )
        assert isinstance(tx_id, str)
        if tx_id in seen_tx_ids:
            raise LabError(
                ErrorCode.TX_DUPLICATE_ID,
                f"duplicate transaction id {tx_id!r}",
                field="tx_id",
                path=tx_path,
            )
        seen_tx_ids.add(tx_id)
        require(
            tx.get("script_seq") == line_index,
            ErrorCode.TX_FIELD_INVALID,
            "script_seq must equal JSONL order",
            field="script_seq",
            path=tx_path,
        )
        _validate_int(
            tx.get("start_at_ms"),
            field="start_at_ms",
            path=tx_path,
            minimum=0,
        )
        _validate_int(
            tx.get("retry_limit"),
            field="retry_limit",
            path=tx_path,
            minimum=0,
            maximum=100,
        )
        isolation = tx.get("isolation")
        if not isinstance(isolation, str) or isolation not in ISOLATION_LEVELS:
            raise LabError(
                ErrorCode.TX_ISOLATION_INVALID,
                "isolation must be READ_COMMITTED, REPEATABLE_READ or SERIALIZABLE",
                field="isolation",
                path=tx_path,
            )
        operations = tx.get("operations")
        require(
            isinstance(operations, list) and bool(operations),
            ErrorCode.TX_OPERATION_INVALID,
            "operations must be a non-empty array",
            field="operations",
            path=tx_path,
        )
        assert isinstance(operations, list)
        for op_index, operation in enumerate(operations):
            op_path = f"{tx_path}.operations[{op_index}]"
            require(
                isinstance(operation, dict),
                ErrorCode.TX_OPERATION_INVALID,
                "operation must be an object",
                field=f"operations[{op_index}]",
                path=tx_path,
            )
            assert isinstance(operation, dict)
            op_id = operation.get("op_id")
            require(
                isinstance(op_id, str) and bool(IDENTIFIER.fullmatch(op_id.replace("-", "_"))),
                ErrorCode.TX_OPERATION_INVALID,
                "op_id must be a stable identifier",
                field=f"operations[{op_index}].op_id",
                path=tx_path,
            )
            assert isinstance(op_id, str)
            if op_id in seen_op_ids:
                raise LabError(
                    ErrorCode.TX_DUPLICATE_ID,
                    f"duplicate operation id {op_id!r}",
                    field=f"operations[{op_index}].op_id",
                    path=tx_path,
                )
            seen_op_ids.add(op_id)
            kind = operation.get("kind")
            require(
                isinstance(kind, str)
                and kind in {"READ", "WRITE", "ROLLBACK"},
                ErrorCode.TX_OPERATION_INVALID,
                "operation kind must be READ, WRITE or ROLLBACK",
                field=f"operations[{op_index}].kind",
                path=tx_path,
            )
            _validate_int(
                operation.get("duration_ns"),
                field=f"operations[{op_index}].duration_ns",
                path=tx_path,
                minimum=0,
            )
            if kind == "ROLLBACK":
                require(
                    op_index == len(operations) - 1,
                    ErrorCode.TX_OPERATION_INVALID,
                    "ROLLBACK must be the final operation",
                    field=f"operations[{op_index}].kind",
                    path=tx_path,
                )
                require(
                    operation.get("lock") == "NONE",
                    ErrorCode.TX_LOCK_INVALID,
                    "ROLLBACK lock declaration must be NONE",
                    field=f"operations[{op_index}].lock",
                    path=tx_path,
                )
                require(
                    "table" not in operation and "key" not in operation,
                    ErrorCode.TX_OPERATION_INVALID,
                    "ROLLBACK must not reference a row",
                    field=f"operations[{op_index}]",
                    path=tx_path,
                )
                continue
            expected_lock = "SHARED" if kind == "READ" else "EXCLUSIVE"
            if operation.get("lock") != expected_lock:
                raise LabError(
                    ErrorCode.TX_LOCK_INVALID,
                    f"{kind} requires {expected_lock} lock declaration",
                    field=f"operations[{op_index}].lock",
                    path=tx_path,
                )
            table_name = operation.get("table")
            require(
                isinstance(table_name, str) and table_name in tables,
                ErrorCode.TX_OPERATION_INVALID,
                f"operation references unknown table {table_name!r}",
                field=f"operations[{op_index}].table",
                path=tx_path,
            )
            table = tables[table_name]
            key = operation.get("key")
            require(
                isinstance(key, dict),
                ErrorCode.TX_OPERATION_INVALID,
                "operation key must be an object",
                field=f"operations[{op_index}].key",
                path=tx_path,
            )
            assert isinstance(key, dict)
            primary_key = table["primary_key"]
            require(
                list(key) == primary_key or set(key) == set(primary_key),
                ErrorCode.TX_OPERATION_INVALID,
                "operation key must contain exactly the primary key columns",
                field=f"operations[{op_index}].key",
                path=tx_path,
            )
            table_columns = {column["name"]: column for column in table["columns"]}
            for column_name, value in key.items():
                require(
                    _column_type_matches(table_columns[column_name]["type"], value),
                    ErrorCode.TX_OPERATION_INVALID,
                    "operation key value has the wrong type",
                    field=f"operations[{op_index}].key.{column_name}",
                    path=tx_path,
                )
            require(
                any(
                    all(row[column] == key[column] for column in primary_key)
                    for row in table["rows"]
                ),
                ErrorCode.TX_OPERATION_INVALID,
                "operation key does not identify a sample row",
                field=f"operations[{op_index}].key",
                path=tx_path,
            )
            if kind == "WRITE":
                values = operation.get("values", {})
                require(
                    isinstance(values, dict),
                    ErrorCode.TX_OPERATION_INVALID,
                    "WRITE values must be an object",
                    field=f"operations[{op_index}].values",
                    path=tx_path,
                )
                assert isinstance(values, dict)
                for column_name, value in values.items():
                    require(
                        column_name in table_columns,
                        ErrorCode.TX_OPERATION_INVALID,
                        f"WRITE references unknown column {column_name!r}",
                        field=f"operations[{op_index}].values.{column_name}",
                        path=tx_path,
                    )
                    require(
                        column_name not in primary_key,
                        ErrorCode.TX_OPERATION_INVALID,
                        "WRITE must not modify primary key columns",
                        field=f"operations[{op_index}].values.{column_name}",
                        path=tx_path,
                    )
                    column = table_columns[column_name]
                    require(
                        _column_type_matches(column["type"], value),
                        ErrorCode.TX_OPERATION_INVALID,
                        "WRITE value has the wrong type",
                        field=f"operations[{op_index}].values.{column_name}",
                        path=tx_path,
                    )
                    require(
                        column["nullable"] or value is not None,
                        ErrorCode.TX_OPERATION_INVALID,
                        "WRITE must not assign null to a non-nullable column",
                        field=f"operations[{op_index}].values.{column_name}",
                        path=tx_path,
                    )
            else:
                require(
                    "values" not in operation,
                    ErrorCode.TX_OPERATION_INVALID,
                    "READ must not contain write values",
                    field=f"operations[{op_index}].values",
                    path=tx_path,
                )
        require(
            sum(1 for operation in operations if operation["kind"] == "ROLLBACK") <= 1,
            ErrorCode.TX_OPERATION_INVALID,
            "at most one ROLLBACK operation is allowed",
            field="operations",
            path=tx_path,
        )
        transactions.append(tx)
    return transactions


def _validate_scope(
    scope: Any,
    table_names: set[str],
    tx_ids: set[str],
    path: str,
    field_name: str,
    error_code: str = ErrorCode.POLICY_V1_INVALID,
) -> str:
    require(
        isinstance(scope, str) and bool(scope),
        error_code,
        "policy scope must be a non-empty string",
        field=field_name,
        path=path,
    )
    assert isinstance(scope, str)
    if scope == "default":
        return scope
    if scope.startswith("tx:"):
        require(
            scope[3:] in tx_ids,
            error_code,
            f"policy references unknown transaction {scope[3:]!r}",
            field=field_name,
            path=path,
        )
        return scope
    if scope.startswith("table:"):
        require(
            scope[6:] in table_names,
            error_code,
            f"policy references unknown table {scope[6:]!r}",
            field=field_name,
            path=path,
        )
        return scope
    if scope.startswith("resource:"):
        resource = scope[9:]
        require(
            "#" in resource,
            error_code,
            "resource policy must use resource:<table>#<key>",
            field=field_name,
            path=path,
        )
        table_name = resource.split("#", 1)[0]
        require(
            table_name in table_names,
            error_code,
            f"resource policy references unknown table {table_name!r}",
            field=field_name,
            path=path,
        )
        return scope
    raise LabError(
        error_code,
        "policy scope must be default, tx:, table: or resource:",
        field=field_name,
        path=path,
    )


def _validate_v1(
    v1: Any,
    database: dict[str, Any],
    transactions: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    path = str(root / "policies" / "v1.json")
    require(
        isinstance(v1, dict),
        ErrorCode.POLICY_V1_INVALID,
        "v1 policy must be an object",
        path=path,
    )
    assert isinstance(v1, dict)
    require(
        v1.get("format_version") == 1,
        ErrorCode.POLICY_V1_INVALID,
        "v1 policy format_version must be 1",
        field="format_version",
        path=path,
    )
    _validate_int(
        v1.get("generation"),
        field="generation",
        path=path,
        minimum=1,
        error_code=ErrorCode.POLICY_V1_INVALID,
    )
    _validate_int(
        v1.get("effective_ms"),
        field="effective_ms",
        path=path,
        minimum=0,
        error_code=ErrorCode.POLICY_V1_INVALID,
    )
    _validate_int(
        v1.get("default_budget_ms"),
        field="default_budget_ms",
        path=path,
        minimum=0,
        error_code=ErrorCode.POLICY_V1_INVALID,
    )
    _validate_int(
        v1.get("retry_multiplier"),
        field="retry_multiplier",
        path=path,
        minimum=1,
        error_code=ErrorCode.POLICY_V1_INVALID,
    )
    _validate_int(
        v1.get("tolerance_ns"),
        field="tolerance_ns",
        path=path,
        minimum=0,
        error_code=ErrorCode.POLICY_V1_INVALID,
    )
    rounding = v1.get("rounding")
    require(
        isinstance(rounding, dict),
        ErrorCode.POLICY_V1_INVALID,
        "rounding must be an object",
        field="rounding",
        path=path,
    )
    assert isinstance(rounding, dict)
    validate_scale(rounding.get("scale"), "rounding.scale", path)
    require(
        rounding.get("mode") == "HALF_UP",
        ErrorCode.POLICY_V1_INVALID,
        "rounding mode must be HALF_UP",
        field="rounding.mode",
        path=path,
    )
    mapping = v1.get("flat_mapping")
    require(
        isinstance(mapping, dict) and bool(mapping),
        ErrorCode.POLICY_V1_INVALID,
        "flat_mapping must be a non-empty object",
        field="flat_mapping",
        path=path,
    )
    assert isinstance(mapping, dict)
    table_names = {table["name"] for table in database["tables"]}
    tx_ids = {tx["tx_id"] for tx in transactions}
    for key, budget in mapping.items():
        _validate_scope(key, table_names, tx_ids, path, f"flat_mapping.{key}")
        _validate_int(
            budget,
            field=f"flat_mapping.{key}",
            path=path,
            minimum=0,
            error_code=ErrorCode.POLICY_V1_INVALID,
        )
    payload = {
        "generation": v1["generation"],
        "effective_ms": v1["effective_ms"],
        "default_budget_ms": v1["default_budget_ms"],
        "retry_multiplier": v1["retry_multiplier"],
        "flat_mapping": v1["flat_mapping"],
        "rounding": v1["rounding"],
        "tolerance_ns": v1["tolerance_ns"],
    }
    require(
        v1.get("summary") == sha256_value(payload),
        ErrorCode.POLICY_SUMMARY,
        "v1 policy summary does not match canonical policy payload",
        field="summary",
        path=path,
    )
    return v1


def _validate_v2(
    v1: dict[str, Any],
    manifest: Any,
    shards: list[dict[str, Any]],
    database: dict[str, Any],
    transactions: list[dict[str, Any]],
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], PolicyView, PolicyView]:
    manifest_path = root / "policies" / "v2" / "manifest.json"
    path = str(manifest_path)
    require(
        isinstance(manifest, dict),
        ErrorCode.POLICY_V2_INVALID,
        "v2 manifest must be an object",
        path=path,
    )
    assert isinstance(manifest, dict)
    require(
        manifest.get("format_version") == 2,
        ErrorCode.POLICY_V2_INVALID,
        "v2 manifest format_version must be 2",
        field="format_version",
        path=path,
    )
    generation = _validate_int(
        manifest.get("generation"),
        field="generation",
        path=path,
        minimum=2,
        error_code=ErrorCode.POLICY_V2_INVALID,
    )
    effective_ms = _validate_int(
        manifest.get("effective_ms"),
        field="effective_ms",
        path=path,
        minimum=0,
        error_code=ErrorCode.POLICY_V2_INVALID,
    )
    require(
        generation > v1["generation"],
        ErrorCode.POLICY_MONOTONIC,
        "v2 generation must be greater than v1 generation",
        field="generation",
        path=path,
    )
    require(
        effective_ms >= v1["effective_ms"],
        ErrorCode.POLICY_MONOTONIC,
        "v2 effective_ms must not precede v1 effective_ms",
        field="effective_ms",
        path=path,
    )
    tolerance_ns = _validate_int(
        manifest.get("tolerance_ns"),
        field="tolerance_ns",
        path=path,
        minimum=0,
        error_code=ErrorCode.POLICY_V2_INVALID,
    )
    rounding = manifest.get("rounding")
    require(
        isinstance(rounding, dict),
        ErrorCode.POLICY_V2_INVALID,
        "v2 rounding must be an object",
        field="rounding",
        path=path,
    )
    assert isinstance(rounding, dict)
    validate_scale(rounding.get("scale"), "rounding.scale", path)
    require(
        rounding.get("mode") == "HALF_UP",
        ErrorCode.POLICY_V2_INVALID,
        "v2 rounding mode must be HALF_UP",
        field="rounding.mode",
        path=path,
    )
    manifests = manifest.get("shards")
    require(
        isinstance(manifests, list) and bool(manifests),
        ErrorCode.POLICY_V2_INVALID,
        "v2 shards must be a non-empty array",
        field="shards",
        path=path,
    )
    assert isinstance(manifests, list)
    require(
        all(
            isinstance(item, dict)
            and _is_int(item.get("shard"))
            and item.get("shard") >= 0
            for item in manifests
        ),
        ErrorCode.POLICY_V2_INVALID,
        "every shard descriptor must contain a non-negative integer shard",
        field="shards",
        path=path,
    )
    require(
        [item.get("shard") for item in manifests if isinstance(item, dict)]
        == sorted(
            item.get("shard") for item in manifests if isinstance(item, dict)
        ),
        ErrorCode.POLICY_V2_INVALID,
        "v2 shard descriptors must be sorted by shard number",
        field="shards",
        path=path,
    )
    if len(shards) != len(manifests):
        raise LabError(
            ErrorCode.POLICY_V2_INVALID,
            "v2 shard file count does not match manifest",
            field="shards",
            path=path,
        )

    table_names = {table["name"] for table in database["tables"]}
    tx_ids = {tx["tx_id"] for tx in transactions}
    entries_by_scope: dict[str, dict[str, Any]] = {}
    shard_by_number: dict[int, dict[str, Any]] = {}
    for index, shard in enumerate(shards):
        require(
            isinstance(shard, dict),
            ErrorCode.POLICY_V2_INVALID,
            "v2 shard must be an object",
            field=f"shards[{index}]",
            path=path,
        )
        assert isinstance(shard, dict)
        require(
            shard.get("format_version") == 2,
            ErrorCode.POLICY_V2_INVALID,
            "shard format_version must be 2",
            field="format_version",
            path=path,
        )
        require(
            shard.get("generation") == generation,
            ErrorCode.POLICY_MONOTONIC,
            "shard generation does not match manifest",
            field="generation",
            path=path,
        )
        require(
            shard.get("effective_ms") == effective_ms,
            ErrorCode.POLICY_MONOTONIC,
            "shard effective_ms does not match manifest",
            field="effective_ms",
            path=path,
        )
        shard_no = _validate_int(
            shard.get("shard"),
            field=f"shards[{index}].shard",
            path=path,
            minimum=0,
            error_code=ErrorCode.POLICY_V2_INVALID,
        )
        if shard_no in shard_by_number:
            raise LabError(
                ErrorCode.POLICY_V2_INVALID,
                f"duplicate v2 shard number {shard_no}",
                field=f"shards[{index}].shard",
                path=path,
            )
        shard_by_number[shard_no] = shard
        entries = shard.get("entries")
        require(
            isinstance(entries, list),
            ErrorCode.POLICY_V2_INVALID,
            "shard entries must be an array",
            field=f"shards[{index}].entries",
            path=path,
        )
        assert isinstance(entries, list)
        for entry_index, entry in enumerate(entries):
            require(
                isinstance(entry, dict),
                ErrorCode.POLICY_V2_INVALID,
                "policy entry must be an object",
                field=f"shards[{index}].entries[{entry_index}]",
                path=path,
            )
            assert isinstance(entry, dict)
            scope = _validate_scope(
                entry.get("scope"),
                table_names,
                tx_ids,
                path,
                f"shards[{index}].entries[{entry_index}].scope",
                ErrorCode.POLICY_V2_INVALID,
            )
            if scope in entries_by_scope:
                raise LabError(
                    ErrorCode.POLICY_V2_INVALID,
                    f"duplicate policy scope {scope!r} across v2 shards",
                    field=f"shards[{index}].entries[{entry_index}].scope",
                    path=path,
                )
            _validate_int(
                entry.get("budget_ms"),
                field=f"shards[{index}].entries[{entry_index}].budget_ms",
                path=path,
                minimum=0,
                error_code=ErrorCode.POLICY_V2_INVALID,
            )
            _validate_int(
                entry.get("retry_multiplier"),
                field=f"shards[{index}].entries[{entry_index}].retry_multiplier",
                path=path,
                minimum=1,
                error_code=ErrorCode.POLICY_V2_INVALID,
            )
            _validate_bool(
                entry.get("tombstone"),
                field=f"shards[{index}].entries[{entry_index}].tombstone",
                path=path,
            )
            entries_by_scope[scope] = {**entry, "shard": shard_no}

    for descriptor_index, descriptor in enumerate(manifests):
        require(
            isinstance(descriptor, dict),
            ErrorCode.POLICY_V2_INVALID,
            "shard descriptor must be an object",
            field=f"shards[{descriptor_index}]",
            path=path,
        )
        assert isinstance(descriptor, dict)
        shard_no = _validate_int(
            descriptor.get("shard"),
            field=f"shards[{descriptor_index}].shard",
            path=path,
            minimum=0,
            error_code=ErrorCode.POLICY_V2_INVALID,
        )
        require(
            shard_no in shard_by_number,
            ErrorCode.POLICY_V2_INVALID,
            "manifest references a missing shard",
            field=f"shards[{descriptor_index}].shard",
            path=path,
        )
        filename = descriptor.get("file")
        require(
            filename == f"shard-{shard_no}.json",
            ErrorCode.POLICY_V2_INVALID,
            "shard filename must be shard-<number>.json",
            field=f"shards[{descriptor_index}].file",
            path=path,
        )
        require(
            descriptor.get("sha256") == sha256_value(shard_by_number[shard_no]),
            ErrorCode.POLICY_SUMMARY,
            "v2 shard checksum does not match shard content",
            field=f"shards[{descriptor_index}].sha256",
            path=path,
        )

    tombstones = manifest.get("tombstones")
    require(
        isinstance(tombstones, list),
        ErrorCode.POLICY_V2_INVALID,
        "tombstones must be an array",
        field="tombstones",
        path=path,
    )
    assert isinstance(tombstones, list)
    tombstone_scopes: set[str] = set()
    for index, tombstone in enumerate(tombstones):
        require(
            isinstance(tombstone, dict),
            ErrorCode.POLICY_TOMBSTONE,
            "tombstone must be an object",
            field=f"tombstones[{index}]",
            path=path,
        )
        assert isinstance(tombstone, dict)
        scope = _validate_scope(
            tombstone.get("scope"),
            table_names,
            tx_ids,
            path,
            f"tombstones[{index}].scope",
            ErrorCode.POLICY_V2_INVALID,
        )
        require(
            scope not in tombstone_scopes,
            ErrorCode.POLICY_TOMBSTONE,
            "duplicate tombstone scope",
            field=f"tombstones[{index}].scope",
            path=path,
        )
        tombstone_scopes.add(scope)
        require(
            isinstance(tombstone.get("reason"), str) and bool(tombstone["reason"]),
            ErrorCode.POLICY_TOMBSTONE,
            "tombstone reason must be a non-empty string",
            field=f"tombstones[{index}].reason",
            path=path,
        )
        entry = entries_by_scope.get(scope)
        require(
            entry is not None and entry["tombstone"] is True,
            ErrorCode.POLICY_TOMBSTONE,
            "manifest tombstone must correspond to a tombstone entry",
            field=f"tombstones[{index}].scope",
            path=path,
        )
    for scope, entry in entries_by_scope.items():
        if entry["tombstone"]:
            require(
                scope in tombstone_scopes,
                ErrorCode.POLICY_TOMBSTONE,
                "tombstone entry is missing from manifest tombstones",
                field="tombstones",
                path=path,
            )
    require(
        "default" in entries_by_scope
        and entries_by_scope["default"]["tombstone"] is False,
        ErrorCode.POLICY_V2_INVALID,
        "v2 policy must contain an active default entry",
        field="default",
        path=path,
    )

    entries = [
        {
            "scope": scope,
            "budget_ms": entry["budget_ms"],
            "retry_multiplier": entry["retry_multiplier"],
            "tombstone": entry["tombstone"],
        }
        for scope, entry in sorted(entries_by_scope.items())
    ]
    payload = {
        "generation": generation,
        "effective_ms": effective_ms,
        "entries": entries,
        "rounding": rounding,
        "tolerance_ns": tolerance_ns,
    }
    require(
        manifest.get("summary") == sha256_value(payload),
        ErrorCode.POLICY_SUMMARY,
        "v2 policy summary does not match canonical shard payload",
        field="summary",
        path=path,
    )

    meta_path = root / "policies" / "v2" / "policy_meta.sqlite"
    require(
        meta_path.exists(),
        ErrorCode.SAMPLE_MISSING,
        "v2 policy metadata SQLite database is missing",
        path=str(meta_path),
    )
    try:
        connection = sqlite3.connect(meta_path)
    except sqlite3.Error as exc:
        raise LabError(
            ErrorCode.POLICY_V2_INVALID,
            f"cannot open v2 policy metadata: {exc}",
            path=str(meta_path),
        ) from exc
    try:
        required_tables = {"policy_generations", "policy_entries", "policy_tombstones"}
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        require(
            required_tables.issubset(actual_tables),
            ErrorCode.POLICY_V2_INVALID,
            "v2 policy metadata is missing required tables",
            field="policy_meta",
            path=str(meta_path),
        )
        generation_rows = list(
            connection.execute(
                "SELECT generation, effective_ms, summary, tombstone "
                "FROM policy_generations ORDER BY generation"
            )
        )
        require(
            bool(generation_rows),
            ErrorCode.POLICY_V2_INVALID,
            "policy_generations must not be empty",
            field="policy_generations",
            path=str(meta_path),
        )
        previous_generation = -1
        previous_effective = -1
        generation_map: dict[int, tuple[int, str, int]] = {}
        for row in generation_rows:
            row_generation, row_effective, row_summary, row_tombstone = row
            require(
                _is_int(row_generation) and row_generation > previous_generation,
                ErrorCode.POLICY_MONOTONIC,
                "policy generation values must be strictly increasing",
                field="policy_generations.generation",
                path=str(meta_path),
            )
            require(
                _is_int(row_effective) and row_effective >= previous_effective,
                ErrorCode.POLICY_MONOTONIC,
                "policy effective_ms values must be monotonic",
                field="policy_generations.effective_ms",
                path=str(meta_path),
            )
            require(
                row_tombstone in (0, 1),
                ErrorCode.POLICY_TOMBSTONE,
                "policy generation tombstone must be 0 or 1",
                field="policy_generations.tombstone",
                path=str(meta_path),
            )
            previous_generation = row_generation
            previous_effective = row_effective
            generation_map[row_generation] = (
                row_effective,
                row_summary,
                row_tombstone,
            )
        require(
            v1["generation"] in generation_map,
            ErrorCode.POLICY_MONOTONIC,
            "v1 generation is absent from policy metadata",
            field="policy_generations",
            path=str(meta_path),
        )
        require(
            generation_map[v1["generation"]][1] == v1["summary"],
            ErrorCode.POLICY_SUMMARY,
            "v1 policy summary disagrees with policy metadata",
            field="policy_generations.summary",
            path=str(meta_path),
        )
        require(
            generation in generation_map,
            ErrorCode.POLICY_MONOTONIC,
            "v2 generation is absent from policy metadata",
            field="policy_generations",
            path=str(meta_path),
        )
        require(
            generation_map[generation][0] == effective_ms,
            ErrorCode.POLICY_MONOTONIC,
            "v2 effective_ms disagrees with policy metadata",
            field="policy_generations.effective_ms",
            path=str(meta_path),
        )
        require(
            generation_map[generation][1] == manifest["summary"],
            ErrorCode.POLICY_SUMMARY,
            "v2 policy summary disagrees with policy metadata",
            field="policy_generations.summary",
            path=str(meta_path),
        )
        metadata_entries = list(
            connection.execute(
                "SELECT generation, scope, budget_ms, retry_multiplier, "
                "tombstone, shard_no FROM policy_entries ORDER BY generation, scope"
            )
        )
        expected_entries = {
            (
                generation,
                entry["scope"],
                entry["budget_ms"],
                entry["retry_multiplier"],
                int(entry["tombstone"]),
                entries_by_scope[entry["scope"]]["shard"],
            )
            for entry in entries
        }
        actual_entries = {
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
            )
            for row in metadata_entries
        }
        require(
            actual_entries == expected_entries,
            ErrorCode.POLICY_SUMMARY,
            "v2 policy metadata entries disagree with shards",
            field="policy_entries",
            path=str(meta_path),
        )
        metadata_tombstones = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT generation, scope FROM policy_tombstones"
            )
        }
        expected_tombstones = {
            (generation, scope) for scope in tombstone_scopes
        }
        require(
            expected_tombstones.issubset(metadata_tombstones),
            ErrorCode.POLICY_TOMBSTONE,
            "v2 policy metadata is missing tombstones",
            field="policy_tombstones",
            path=str(meta_path),
        )
    finally:
        connection.close()

    v1_entries: dict[str, dict[str, Any]] = {
        scope: {
            "scope": scope,
            "budget_ms": budget,
            "retry_multiplier": v1["retry_multiplier"],
            "tombstone": False,
        }
        for scope, budget in v1["flat_mapping"].items()
    }
    v1_entries["default"] = {
        "scope": "default",
        "budget_ms": v1["default_budget_ms"],
        "retry_multiplier": v1["retry_multiplier"],
        "tombstone": False,
    }
    v2_entries = {
        entry["scope"]: {
            **entry,
            "shard": entries_by_scope[entry["scope"]]["shard"],
        }
        for entry in entries
    }
    v1_view = PolicyView(
        generation=v1["generation"],
        effective_ms=v1["effective_ms"],
        summary=v1["summary"],
        scale=v1["rounding"]["scale"],
        tolerance_ns=v1["tolerance_ns"],
        default_budget_ms=v1["default_budget_ms"],
        entries=v1_entries,
        retry_multiplier=v1["retry_multiplier"],
    )
    v2_view = PolicyView(
        generation=generation,
        effective_ms=effective_ms,
        summary=manifest["summary"],
        scale=rounding["scale"],
        tolerance_ns=tolerance_ns,
        default_budget_ms=next(
            entry["budget_ms"]
            for entry in entries
            if entry["scope"] == "default" and not entry["tombstone"]
        ),
        entries=v2_entries,
        retry_multiplier=next(
            entry["retry_multiplier"]
            for entry in entries
            if entry["scope"] == "default" and not entry["tombstone"]
        ),
    )
    return manifest, entries, v1_view, v2_view


def _validate_clock(
    clock: Any,
    root: Path,
    database: dict[str, Any],
    transactions: list[dict[str, Any]],
) -> dict[str, Any]:
    path = str(root / "clock.json")
    require(
        isinstance(clock, dict),
        ErrorCode.SCHEMA_INVALID,
        "clock config must be an object",
        path=path,
    )
    assert isinstance(clock, dict)
    require(
        clock.get("format_version") == 1,
        ErrorCode.SCHEMA_INVALID,
        "clock format_version must be 1",
        field="format_version",
        path=path,
    )
    _validate_int(clock.get("seed"), field="seed", path=path, minimum=0)
    require(
        clock.get("logical_unit") == "nanosecond",
        ErrorCode.SCHEMA_INVALID,
        "logical_unit must be nanosecond",
        field="logical_unit",
        path=path,
    )
    scale = validate_scale(clock.get("ledger_scale"), "ledger_scale", path)
    require(
        scale == database["ledger"]["duration_scale"],
        ErrorCode.SCHEMA_INVALID,
        "clock ledger_scale must match the database ledger scale",
        field="ledger_scale",
        path=path,
    )
    _validate_int(
        clock.get("tolerance_ns"), field="tolerance_ns", path=path, minimum=0
    )
    _validate_int(
        clock.get("max_logical_ms"), field="max_logical_ms", path=path, minimum=1
    )
    _validate_int(
        clock.get("parallelism"), field="parallelism", path=path, minimum=1
    )
    _validate_int(
        clock.get("round_budget_ms"), field="round_budget_ms", path=path, minimum=1
    )
    _validate_int(
        clock.get("policy_switch_at_ms"),
        field="policy_switch_at_ms",
        path=path,
        minimum=0,
            error_code=ErrorCode.POLICY_V2_INVALID,
    )
    require(
        clock["policy_switch_at_ms"] <= clock["max_logical_ms"],
        ErrorCode.SCHEMA_INVALID,
        "policy_switch_at_ms must not exceed max_logical_ms",
        field="policy_switch_at_ms",
        path=path,
    )
    require(
        any(
            tx["start_at_ms"] > clock["max_logical_ms"] for tx in transactions
        ),
        ErrorCode.SCHEMA_INVALID,
        "sample must include a transaction beyond the logical budget",
        field="max_logical_ms",
        path=path,
    )
    return clock


def load_bundle(root: Path | str) -> SampleBundle:
    root = Path(root)
    if not root.exists() or not root.is_dir():
        raise LabError(
            ErrorCode.SAMPLE_MISSING,
            "samples directory does not exist",
            path=str(root),
        )
    database = read_json(root / "sample_db.json")
    _validate_database(database, root)
    raw_transactions = read_jsonl(root / "transactions.jsonl")
    transactions = _validate_transactions(raw_transactions, database, root)
    v1 = _validate_v1(
        read_json(root / "policies" / "v1.json"), database, transactions, root
    )
    v2_manifest = read_json(root / "policies" / "v2" / "manifest.json")
    require(
        isinstance(v2_manifest, dict),
        ErrorCode.POLICY_V2_INVALID,
        "v2 manifest must be an object",
        path=str(root / "policies" / "v2" / "manifest.json"),
    )
    assert isinstance(v2_manifest, dict)
    shard_file_list = v2_manifest.get("shards")
    require(
        isinstance(shard_file_list, list),
        ErrorCode.POLICY_V2_INVALID,
        "v2 manifest shards must be an array",
        field="shards",
        path=str(root / "policies" / "v2" / "manifest.json"),
    )
    assert isinstance(shard_file_list, list)
    shards: list[dict[str, Any]] = []
    for index, descriptor in enumerate(shard_file_list):
        require(
            isinstance(descriptor, dict),
            ErrorCode.POLICY_V2_INVALID,
            "shard descriptor must be an object",
            field=f"shards[{index}]",
            path=str(root / "policies" / "v2" / "manifest.json"),
        )
        assert isinstance(descriptor, dict)
        filename = descriptor.get("file")
        require(
            isinstance(filename, str) and "/" not in filename and "\\" not in filename,
            ErrorCode.POLICY_V2_INVALID,
            "shard filename must be a plain filename",
            field=f"shards[{index}].file",
            path=str(root / "policies" / "v2" / "manifest.json"),
        )
        shards.append(
            read_json(root / "policies" / "v2" / filename)
        )
    v2, v2_entries, v1_view, v2_view = _validate_v2(
        v1, v2_manifest, shards, database, transactions, root
    )
    clock = _validate_clock(read_json(root / "clock.json"), root, database, transactions)
    fingerprint = sha256_value(
        {
            "database": database,
            "transactions": transactions,
            "policy_v1": v1,
            "policy_v2": v2,
            "policy_shards": sorted(shards, key=lambda shard: shard["shard"]),
            "clock": clock,
        }
    )
    return SampleBundle(
        root=root,
        database=database,
        transactions=transactions,
        clock=clock,
        policy_v1=v1,
        policy_v2=v2,
        policy_shards=sorted(shards, key=lambda shard: shard["shard"]),
        policy_views={
            v1_view.generation: v1_view,
            v2_view.generation: v2_view,
        },
        fingerprint=fingerprint,
        sample_sqlite=root / "sample.sqlite",
        policy_sqlite=root / "policies" / "v2" / "policy_meta.sqlite",
    )


def policy_payload(view: PolicyView) -> dict[str, Any]:
    return {
        "generation": view.generation,
        "effective_ms": view.effective_ms,
        "summary": view.summary,
        "scale": view.scale,
        "tolerance_ns": view.tolerance_ns,
        "default_budget_ms": view.default_budget_ms,
        "retry_multiplier": view.retry_multiplier,
        "entries": [
            view.entries[scope] for scope in sorted(view.entries)
        ],
    }


def iter_policy_views(bundle: SampleBundle) -> Iterable[PolicyView]:
    for generation in sorted(bundle.policy_views):
        yield bundle.policy_views[generation]
