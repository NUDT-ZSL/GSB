from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json, row_key_text, sha256_json
from .errors import WarbiterError


COLUMN_TYPES = frozenset({"string", "integer", "number", "boolean"})
STRATEGIES = (
    "last_write_wins",
    "column_merge",
    "reject_rollback",
    "manual_review",
)
STRATEGY_SET = frozenset(STRATEGIES)
ROW_STRATEGIES = frozenset(
    {"last_write_wins", "reject_rollback", "manual_review"}
)
SAME_COLUMN_STRATEGIES = frozenset(
    {"last_write_wins", "reject_rollback", "manual_review"}
)
ISOLATION_LEVELS = frozenset(
    {"read_committed", "repeatable_read", "serializable"}
)
OP_TYPES = frozenset({"insert", "update", "delete", "conditional_update"})
ASSERTION_OPS = frozenset(
    {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "not_null", "in"}
)
ASSERTION_ACTIONS = frozenset({"rollback", "manual_review"})


def _bad(code: str, message: str, **details: Any) -> None:
    raise WarbiterError(code, message, details)


def _expect_object(value: Any, code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _bad(code, f"{path} must be a JSON object", path=path)
    return value


def _expect_list(value: Any, code: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(code, f"{path} must be a JSON array", path=path)
    return value


def _expect_string(value: Any, code: str, path: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(code, f"{path} must be a non-empty string", path=path)
    return value


def _expect_positive_int(value: Any, code: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _bad(code, f"{path} must be a positive integer", path=path)
    return value


def _expect_nonnegative_int(value: Any, code: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _bad(code, f"{path} must be a non-negative integer", path=path)
    return value


def value_matches_type(value: Any, column_type: str) -> bool:
    if column_type == "string":
        return isinstance(value, str)
    if column_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if column_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if column_type == "boolean":
        return isinstance(value, bool)
    return False


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True


@dataclass(frozen=True)
class TablePolicy:
    version_label: str
    row_strategy: str
    column_strategies: dict[str, str]
    row_overwrite_columns: tuple[str, ...]
    same_column_merge: str
    allowed_strategies: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_label": self.version_label,
            "row_strategy": self.row_strategy,
            "column_strategies": dict(sorted(self.column_strategies.items())),
            "row_overwrite_columns": list(self.row_overwrite_columns),
            "same_column_merge": self.same_column_merge,
            "allowed_strategies": list(self.allowed_strategies),
        }


@dataclass(frozen=True)
class Table:
    tenant: str
    name: str
    primary_key: str
    columns: tuple[Column, ...]
    policy: TablePolicy

    @property
    def column_map(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def non_pk_columns(self) -> tuple[str, ...]:
        return tuple(name for name in self.column_names if name != self.primary_key)

    def as_physical_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "name": self.name,
            "primary_key": self.primary_key,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                }
                for column in self.columns
            ],
        }

    def as_policy_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "table": self.name,
            "policy": self.policy.as_dict(),
        }


@dataclass(frozen=True)
class Schema:
    tenants: tuple[str, ...]
    tables: tuple[Table, ...]

    @property
    def table_map(self) -> dict[tuple[str, str], Table]:
        return {(table.tenant, table.name): table for table in self.tables}

    def get_table(self, tenant: str, table_name: str) -> Table | None:
        return self.table_map.get((tenant, table_name))

    def as_dict(self) -> dict[str, Any]:
        by_tenant: dict[str, list[dict[str, Any]]] = {name: [] for name in self.tenants}
        for table in self.tables:
            item = table.as_physical_dict()
            item["conflict_policy"] = table.policy.as_dict()
            by_tenant[table.tenant].append(item)
        return {
            "format": "warbiter-schema-v1",
            "tenants": [
                {"name": tenant, "tables": by_tenant[tenant]}
                for tenant in self.tenants
            ],
        }

    def physical_document(self) -> dict[str, Any]:
        return {
            "format": "warbiter-schema-v1",
            "tenants": [
                {
                    "name": tenant,
                    "tables": [
                        table.as_physical_dict()
                        for table in self.tables
                        if table.tenant == tenant
                    ],
                }
                for tenant in self.tenants
            ],
        }

    def policy_document(self) -> dict[str, Any]:
        return {
            "format": "warbiter-policy-v1",
            "schema_hash": sha256_json(self.physical_document()),
            "tables": [
                table.as_policy_dict()
                for table in sorted(self.tables, key=lambda item: (item.tenant, item.name))
            ],
        }


@dataclass(frozen=True)
class ArbiterConfig:
    conflict_tolerance_ms: int
    logical_clock_step_ms: int
    max_conflict_replay: int
    ledger_retention_generations: int
    assertion_failure_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "warbiter-config-v1",
            "conflict_tolerance_ms": self.conflict_tolerance_ms,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "max_conflict_replay": self.max_conflict_replay,
            "ledger_retention_generations": self.ledger_retention_generations,
            "assertion_failure_action": self.assertion_failure_action,
        }


@dataclass(frozen=True)
class WriteOp:
    index: int
    op: str
    tenant: str
    table: str
    row_key: Any
    row_key_text: str
    values: dict[str, Any]
    assertion: Any | None = None
    recorded_observation: dict[str, Any] | None = None
    whole_row: bool = False


@dataclass(frozen=True)
class Transaction:
    tx_id: int
    tenant: str
    start_clock_ms: int
    isolation: str
    writes: tuple[WriteOp, ...]
    commit_clock_ms: int
    payload: dict[str, Any]

    @property
    def payload_hash(self) -> str:
        return sha256_json(self.payload)


@dataclass
class FinalRowWrite:
    tx_id: int
    tenant: str
    table: str
    row_key: Any
    row_key_text: str
    final_kind: str
    whole_row: bool
    coverage: tuple[str, ...]
    values: dict[str, Any]


@dataclass
class NormalizedTransaction:
    transaction: Transaction
    final_rows: list[FinalRowWrite]
    audit_rows: list[dict[str, Any]]
    assertions: list[dict[str, Any]]


@dataclass(frozen=True)
class Script:
    transactions: tuple[Transaction, ...]
    document: dict[str, Any]
    script_hash: str


def _parse_policy(raw: Any, table_name: str) -> TablePolicy:
    policy = _expect_object(raw, "E_BAD_POLICY", f"table {table_name}.conflict_policy")
    version_label = _expect_string(
        policy.get("version_label", "policy-v1"),
        "E_BAD_POLICY",
        f"table {table_name}.conflict_policy.version_label",
    )
    row_strategy = _expect_string(
        policy.get("row_strategy"),
        "E_BAD_POLICY",
        f"table {table_name}.conflict_policy.row_strategy",
    )
    if row_strategy not in ROW_STRATEGIES:
        _bad(
            "E_BAD_POLICY",
            f"table {table_name}: row_strategy must be one of {sorted(ROW_STRATEGIES)}",
            table=table_name,
            value=row_strategy,
        )
    raw_columns = policy.get("column_strategies", {})
    raw_columns = _expect_object(
        raw_columns,
        "E_BAD_POLICY",
        f"table {table_name}.conflict_policy.column_strategies",
    )
    column_strategies: dict[str, str] = {}
    for column_name, strategy in raw_columns.items():
        if not isinstance(column_name, str) or not column_name:
            _bad("E_BAD_POLICY", f"table {table_name}: invalid column strategy key")
        if strategy not in STRATEGY_SET:
            _bad(
                "E_BAD_POLICY",
                f"table {table_name}: unknown strategy for column {column_name}",
                table=table_name,
                column=column_name,
                value=strategy,
            )
        column_strategies[column_name] = strategy
    raw_allowed = policy.get("allowed_strategies", list(STRATEGIES))
    raw_allowed = _expect_list(
        raw_allowed,
        "E_BAD_POLICY",
        f"table {table_name}.conflict_policy.allowed_strategies",
    )
    allowed: list[str] = []
    for strategy in raw_allowed:
        if strategy not in STRATEGY_SET:
            _bad(
                "E_BAD_POLICY",
                f"table {table_name}: allowed_strategies contains an unknown label",
                value=strategy,
            )
        if strategy in allowed:
            _bad("E_BAD_POLICY", f"table {table_name}: duplicate allowed strategy")
        allowed.append(strategy)
    if not allowed:
        _bad("E_BAD_POLICY", f"table {table_name}: allowed_strategies cannot be empty")
    for strategy in [row_strategy, *column_strategies.values()]:
        if strategy not in allowed:
            _bad(
                "E_BAD_POLICY",
                f"table {table_name}: selected strategy {strategy} is not allowed",
                strategy=strategy,
            )
    same_column_merge = _expect_string(
        policy.get("same_column_merge", "last_write_wins"),
        "E_BAD_POLICY",
        f"table {table_name}.conflict_policy.same_column_merge",
    )
    if same_column_merge not in SAME_COLUMN_STRATEGIES:
        _bad(
            "E_BAD_POLICY",
            f"table {table_name}: same_column_merge is invalid",
            value=same_column_merge,
        )
    if same_column_merge not in allowed:
        _bad(
            "E_BAD_POLICY",
            f"table {table_name}: same_column_merge is not allowed",
            value=same_column_merge,
        )
    raw_row_columns = policy.get("row_overwrite_columns")
    row_overwrite_columns: list[str] = []
    if raw_row_columns is not None:
        raw_row_columns = _expect_list(
            raw_row_columns,
            "E_BAD_POLICY",
            f"table {table_name}.conflict_policy.row_overwrite_columns",
        )
        for column_name in raw_row_columns:
            if not isinstance(column_name, str) or not column_name:
                _bad(
                    "E_BAD_POLICY",
                    f"table {table_name}: row_overwrite_columns must contain strings",
                )
            if column_name in row_overwrite_columns:
                _bad(
                    "E_BAD_POLICY",
                    f"table {table_name}: duplicate row overwrite column {column_name}",
                )
            row_overwrite_columns.append(column_name)
    return TablePolicy(
        version_label=version_label,
        row_strategy=row_strategy,
        column_strategies=column_strategies,
        row_overwrite_columns=tuple(sorted(row_overwrite_columns)),
        same_column_merge=same_column_merge,
        allowed_strategies=tuple(sorted(allowed)),
    )


def parse_schema(raw: Any) -> Schema:
    document = _expect_object(raw, "E_BAD_SCHEMA", "schema")
    if document.get("format") != "warbiter-schema-v1":
        _bad("E_BAD_SCHEMA", "schema.format must be warbiter-schema-v1")
    tenants_raw = _expect_list(document.get("tenants"), "E_BAD_SCHEMA", "schema.tenants")
    tenant_names: list[str] = []
    tables: list[Table] = []
    for tenant_index, tenant_raw in enumerate(tenants_raw):
        tenant_raw = _expect_object(
            tenant_raw, "E_BAD_SCHEMA", f"schema.tenants[{tenant_index}]"
        )
        tenant_name = _expect_string(
            tenant_raw.get("name"),
            "E_BAD_SCHEMA",
            f"schema.tenants[{tenant_index}].name",
        )
        if tenant_name in tenant_names:
            _bad("E_BAD_SCHEMA", f"duplicate tenant {tenant_name}")
        tenant_names.append(tenant_name)
        tables_raw = _expect_list(
            tenant_raw.get("tables"),
            "E_BAD_SCHEMA",
            f"tenant {tenant_name}.tables",
        )
        tenant_table_names: set[str] = set()
        for table_index, table_raw in enumerate(tables_raw):
            table_raw = _expect_object(
                table_raw,
                "E_BAD_SCHEMA",
                f"tenant {tenant_name}.tables[{table_index}]",
            )
            table_name = _expect_string(
                table_raw.get("name"),
                "E_BAD_SCHEMA",
                f"tenant {tenant_name}.tables[{table_index}].name",
            )
            if table_name in tenant_table_names:
                _bad(
                    "E_BAD_SCHEMA",
                    f"tenant {tenant_name}: duplicate table {table_name}",
                )
            tenant_table_names.add(table_name)
            primary_key = _expect_string(
                table_raw.get("primary_key"),
                "E_BAD_SCHEMA",
                f"table {table_name}.primary_key",
            )
            columns_raw = _expect_list(
                table_raw.get("columns"),
                "E_BAD_SCHEMA",
                f"table {table_name}.columns",
            )
            columns: list[Column] = []
            column_names: set[str] = set()
            for column_index, column_raw in enumerate(columns_raw):
                column_raw = _expect_object(
                    column_raw,
                    "E_BAD_SCHEMA",
                    f"table {table_name}.columns[{column_index}]",
                )
                column_name = _expect_string(
                    column_raw.get("name"),
                    "E_BAD_SCHEMA",
                    f"table {table_name}.columns[{column_index}].name",
                )
                if column_name in column_names:
                    _bad(
                        "E_BAD_SCHEMA",
                        f"table {table_name}: duplicate column {column_name}",
                    )
                column_names.add(column_name)
                column_type = _expect_string(
                    column_raw.get("type"),
                    "E_BAD_SCHEMA",
                    f"table {table_name}.{column_name}.type",
                )
                if column_type not in COLUMN_TYPES:
                    _bad(
                        "E_BAD_SCHEMA",
                        f"table {table_name}: invalid column type {column_type}",
                        table=table_name,
                        column=column_name,
                        value=column_type,
                    )
                nullable = column_raw.get("nullable", column_name != primary_key)
                if not isinstance(nullable, bool):
                    _bad(
                        "E_BAD_SCHEMA",
                        f"table {table_name}.{column_name}.nullable must be boolean",
                    )
                columns.append(Column(column_name, column_type, nullable))
            if primary_key not in column_names:
                _bad(
                    "E_BAD_SCHEMA",
                    f"table {table_name}: primary key {primary_key} is not a column",
                )
            policy = _parse_policy(table_raw.get("conflict_policy"), table_name)
            for column_name in policy.column_strategies:
                if column_name not in column_names:
                    _bad(
                        "E_BAD_POLICY",
                        f"table {table_name}: strategy refers to unknown column {column_name}",
                    )
            for column_name in policy.row_overwrite_columns:
                if column_name not in column_names:
                    _bad(
                        "E_BAD_POLICY",
                        f"table {table_name}: row overwrite refers to unknown column {column_name}",
                    )
                if column_name in policy.column_strategies and (
                    policy.column_strategies[column_name] == "column_merge"
                ):
                    _bad(
                        "E_BAD_POLICY",
                        f"table {table_name}: column {column_name} is both row-overwrite and column-merge",
                        table=table_name,
                        column=column_name,
                    )
            pk_column = next(column for column in columns if column.name == primary_key)
            if pk_column.nullable:
                _bad(
                    "E_BAD_SCHEMA",
                    f"table {table_name}: primary key must be non-nullable",
                )
            tables.append(
                Table(
                    tenant=tenant_name,
                    name=table_name,
                    primary_key=primary_key,
                    columns=tuple(columns),
                    policy=policy,
                )
            )
    if not tenant_names:
        _bad("E_BAD_SCHEMA", "schema must declare at least one tenant")
    return Schema(tuple(sorted(tenant_names)), tuple(tables))


def parse_config(raw: Any) -> ArbiterConfig:
    document = _expect_object(raw, "E_BAD_CONFIG", "config")
    if document.get("format") != "warbiter-config-v1":
        _bad("E_BAD_CONFIG", "config.format must be warbiter-config-v1")
    tolerance = _expect_nonnegative_int(
        document.get("conflict_tolerance_ms"),
        "E_BAD_CONFIG",
        "config.conflict_tolerance_ms",
    )
    step = _expect_positive_int(
        document.get("logical_clock_step_ms"),
        "E_BAD_CONFIG",
        "config.logical_clock_step_ms",
    )
    replay = _expect_positive_int(
        document.get("max_conflict_replay"),
        "E_BAD_CONFIG",
        "config.max_conflict_replay",
    )
    retention = _expect_positive_int(
        document.get("ledger_retention_generations"),
        "E_BAD_CONFIG",
        "config.ledger_retention_generations",
    )
    action = _expect_string(
        document.get("assertion_failure_action"),
        "E_BAD_CONFIG",
        "config.assertion_failure_action",
    )
    if action not in ASSERTION_ACTIONS:
        _bad(
            "E_BAD_CONFIG",
            "config.assertion_failure_action must be rollback or manual_review",
            value=action,
        )
    return ArbiterConfig(tolerance, step, replay, retention, action)


def _validate_value(
    value: Any,
    column: Column,
    code: str,
    path: str,
) -> None:
    if value is None:
        if not column.nullable:
            _bad(code, f"{path}: column {column.name} is non-nullable")
        return
    if not value_matches_type(value, column.type):
        _bad(
            code,
            f"{path}: value for column {column.name} must be {column.type}",
            column=column.name,
            expected=column.type,
            actual=type(value).__name__,
        )


def _validate_assertion(assertion: Any, table: Table, path: str) -> None:
    if not isinstance(assertion, dict):
        _bad("E_BAD_SCRIPT", f"{path} must be a JSON object")
    keys = set(assertion)
    if keys == {"all"} or keys == {"any"}:
        key = "all" if "all" in keys else "any"
        children = assertion.get(key)
        if not isinstance(children, list) or not children:
            _bad("E_BAD_SCRIPT", f"{path}.{key} must be a non-empty array")
        for index, child in enumerate(children):
            _validate_assertion(child, table, f"{path}.{key}[{index}]")
        return
    if keys == {"not"}:
        _validate_assertion(assertion.get("not"), table, f"{path}.not")
        return
    column_name = assertion.get("column")
    op = assertion.get("op")
    if not isinstance(column_name, str) or column_name not in table.column_map:
        _bad("E_BAD_SCRIPT", f"{path}: assertion column is unknown")
    if op not in ASSERTION_OPS:
        _bad("E_BAD_SCRIPT", f"{path}: unknown assertion operator {op!r}")
    column = table.column_map[column_name]
    if op in {"is_null", "not_null"}:
        if set(assertion) - {"column", "op"}:
            _bad("E_BAD_SCRIPT", f"{path}: {op} does not accept a value")
        return
    if "value" not in assertion:
        _bad("E_BAD_SCRIPT", f"{path}: assertion value is required")
    if op == "in":
        values = assertion.get("value")
        if not isinstance(values, list) or not values:
            _bad("E_BAD_SCRIPT", f"{path}.value must be a non-empty array")
        for index, value in enumerate(values):
            _validate_value(value, column, "E_BAD_SCRIPT", f"{path}.value[{index}]")
        return
    _validate_value(assertion.get("value"), column, "E_BAD_SCRIPT", f"{path}.value")


def parse_script(raw: Any, schema: Schema, config: ArbiterConfig) -> Script:
    document = _expect_object(raw, "E_BAD_SCRIPT", "script")
    if document.get("format") != "warbiter-script-v1":
        _bad("E_BAD_SCRIPT", "script.format must be warbiter-script-v1")
    transactions_raw = _expect_list(
        document.get("transactions"), "E_BAD_SCRIPT", "script.transactions"
    )
    seen_tx_ids: set[int] = set()
    transactions: list[Transaction] = []
    for tx_index, tx_raw in enumerate(transactions_raw):
        tx_raw = _expect_object(tx_raw, "E_BAD_SCRIPT", f"transactions[{tx_index}]")
        tx_id = _expect_positive_int(
            tx_raw.get("tx_id"), "E_BAD_SCRIPT", f"transactions[{tx_index}].tx_id"
        )
        if tx_id in seen_tx_ids:
            _bad("E_BAD_SCRIPT", f"duplicate tx_id {tx_id} in script")
        seen_tx_ids.add(tx_id)
        tenant = _expect_string(
            tx_raw.get("tenant"),
            "E_BAD_SCRIPT",
            f"transactions[{tx_index}].tenant",
        )
        if tenant not in {table.tenant for table in schema.tables}:
            _bad("E_BAD_SCRIPT", f"unknown tenant {tenant}")
        start_clock = _expect_positive_int(
            tx_raw.get("start_clock_ms"),
            "E_BAD_SCRIPT",
            f"transactions[{tx_index}].start_clock_ms",
        )
        isolation = _expect_string(
            tx_raw.get("isolation"),
            "E_BAD_SCRIPT",
            f"transactions[{tx_index}].isolation",
        )
        if isolation not in ISOLATION_LEVELS:
            _bad("E_BAD_SCRIPT", f"unknown isolation level {isolation}")
        writes_raw = _expect_list(
            tx_raw.get("writes"),
            "E_BAD_SCRIPT",
            f"transactions[{tx_index}].writes",
        )
        if not writes_raw:
            _bad("E_BAD_SCRIPT", "a transaction must contain at least one write")
        normal_writes: list[dict[str, Any]] = []
        writes: list[WriteOp] = []
        for op_index, op_raw in enumerate(writes_raw):
            path = f"transactions[{tx_index}].writes[{op_index}]"
            op_raw = _expect_object(op_raw, "E_BAD_SCRIPT", path)
            op = _expect_string(op_raw.get("op"), "E_BAD_SCRIPT", f"{path}.op")
            if op not in OP_TYPES:
                _bad("E_BAD_SCRIPT", f"{path}: unknown op {op}")
            table_name = _expect_string(
                op_raw.get("table"), "E_BAD_SCRIPT", f"{path}.table"
            )
            table = schema.get_table(tenant, table_name)
            if table is None:
                _bad(
                    "E_BAD_SCRIPT",
                    f"{path}: table {tenant}/{table_name} does not exist",
                )
            if "row_key" not in op_raw:
                _bad("E_BAD_SCRIPT", f"{path}.row_key is required")
            row_key = op_raw["row_key"]
            _validate_value(
                row_key,
                table.column_map[table.primary_key],
                "E_BAD_SCRIPT",
                f"{path}.row_key",
            )
            key_text = row_key_text(row_key)
            values_raw = op_raw.get("values", {})
            values_raw = _expect_object(values_raw, "E_BAD_SCRIPT", f"{path}.values")
            values: dict[str, Any] = {}
            for column_name, value in values_raw.items():
                column = table.column_map.get(column_name)
                if column is None:
                    _bad("E_BAD_SCRIPT", f"{path}: unknown column {column_name}")
                _validate_value(value, column, "E_BAD_SCRIPT", f"{path}.values")
                values[column_name] = value
            whole_row = op_raw.get("whole_row", False)
            if not isinstance(whole_row, bool):
                _bad("E_BAD_SCRIPT", f"{path}.whole_row must be boolean")
            if op == "insert":
                if table.primary_key not in values:
                    _bad("E_BAD_SCRIPT", f"{path}: insert must include primary key")
                if values[table.primary_key] != row_key:
                    _bad(
                        "E_BAD_SCRIPT",
                        f"{path}: insert row_key and primary key value differ",
                    )
                for column in table.columns:
                    if column.name not in values and not column.nullable:
                        _bad(
                            "E_BAD_SCRIPT",
                            f"{path}: insert is missing non-nullable column {column.name}",
                        )
            elif op in {"update", "conditional_update"}:
                if not values:
                    _bad("E_BAD_SCRIPT", f"{path}: {op} requires values")
                if table.primary_key in values and values[table.primary_key] != row_key:
                    _bad("E_BAD_SCRIPT", f"{path}: primary key updates are forbidden")
                values.pop(table.primary_key, None)
                if whole_row:
                    missing = [
                        column.name
                        for column in table.non_pk_columns
                        if column.name not in values
                    ]
                    if missing:
                        _bad(
                            "E_BAD_SCRIPT",
                            f"{path}: whole_row update is missing columns {missing}",
                        )
            else:
                if values:
                    _bad("E_BAD_SCRIPT", f"{path}: delete does not accept values")
            assertion = None
            recorded_observation = None
            if op == "conditional_update":
                if "assertion" not in op_raw:
                    _bad("E_BAD_SCRIPT", f"{path}.assertion is required")
                assertion = op_raw["assertion"]
                _validate_assertion(assertion, table, f"{path}.assertion")
                raw_observation = op_raw.get("recorded_observation")
                if raw_observation is not None:
                    if not isinstance(raw_observation, dict):
                        _bad(
                            "E_BAD_SCRIPT",
                            f"{path}.recorded_observation must be an object",
                        )
                    recorded_observation = {}
                    for column_name, value in raw_observation.items():
                        column = table.column_map.get(column_name)
                        if column is None:
                            _bad(
                                "E_BAD_SCRIPT",
                                f"{path}.recorded_observation: unknown column {column_name}",
                            )
                        _validate_value(
                            value,
                            column,
                            "E_BAD_SCRIPT",
                            f"{path}.recorded_observation",
                        )
                        recorded_observation[column_name] = value
            elif "assertion" in op_raw:
                _bad("E_BAD_SCRIPT", f"{path}: assertion is only valid for conditional_update")
            writes.append(
                WriteOp(
                    index=op_index,
                    op=op,
                    tenant=tenant,
                    table=table_name,
                    row_key=row_key,
                    row_key_text=key_text,
                    values=values,
                    assertion=assertion,
                    recorded_observation=recorded_observation,
                    whole_row=whole_row,
                )
            )
            normal_writes.append(
                {
                    "op": op,
                    "table": table_name,
                    "row_key": row_key,
                    "values": values,
                    **({"whole_row": True} if whole_row else {}),
                    **({"assertion": assertion} if assertion is not None else {}),
                    **(
                        {"recorded_observation": recorded_observation}
                        if recorded_observation is not None
                        else {}
                    ),
                }
            )
        payload = {
            "tx_id": tx_id,
            "tenant": tenant,
            "start_clock_ms": start_clock,
            "isolation": isolation,
            "writes": normal_writes,
        }
        transactions.append(
            Transaction(
                tx_id=tx_id,
                tenant=tenant,
                start_clock_ms=start_clock,
                isolation=isolation,
                writes=tuple(writes),
                commit_clock_ms=start_clock + config.logical_clock_step_ms,
                payload=payload,
            )
        )
    script_hash = sha256_json(
        {
            "transactions": [
                transaction.payload_hash
                for transaction in sorted(transactions, key=lambda item: item.tx_id)
            ]
        }
    )
    return Script(tuple(transactions), document, script_hash)


def normalize_transaction(
    transaction: Transaction,
    schema: Schema,
) -> NormalizedTransaction:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    audit_rows: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    for write in transaction.writes:
        table = schema.get_table(write.tenant, write.table)
        if table is None:
            raise AssertionError("validated table disappeared")
        key = (write.tenant, write.table, write.row_key_text)
        state = grouped.setdefault(
            key,
            {
                "kind": None,
                "values": {},
                "staged": {},
                "coverage": set(),
                "whole_row": False,
                "row_key": write.row_key,
            },
        )
        if write.op == "insert":
            state["kind"] = "insert"
            state["whole_row"] = True
            state["coverage"] = set(table.column_names)
            for column_name, value in write.values.items():
                before = state["staged"].get(column_name)
                state["values"][column_name] = value
                state["staged"][column_name] = value
                audit_rows.append(
                    {
                        "tx_id": transaction.tx_id,
                        "op_index": write.index,
                        "op_type": write.op,
                        "tenant": write.tenant,
                        "table": write.table,
                        "row_key_text": write.row_key_text,
                        "column_name": column_name,
                        "before": before,
                        "after": value,
                        "assertion": None,
                        "observation": None,
                    }
                )
            continue
        if write.op == "delete":
            state["kind"] = "delete"
            state["whole_row"] = True
            state["coverage"] = set(table.column_names)
            for column_name in table.column_names:
                before = state["staged"].get(column_name)
                state["staged"].pop(column_name, None)
                audit_rows.append(
                    {
                        "tx_id": transaction.tx_id,
                        "op_index": write.index,
                        "op_type": write.op,
                        "tenant": write.tenant,
                        "table": write.table,
                        "row_key_text": write.row_key_text,
                        "column_name": column_name,
                        "before": before,
                        "after": None,
                        "assertion": None,
                        "observation": None,
                    }
                )
            state["values"] = {}
            continue
        if state["kind"] is None or state["kind"] == "delete":
            state["kind"] = "update"
        if write.whole_row:
            state["whole_row"] = True
            state["coverage"] = set(table.column_names)
        else:
            state["coverage"].update(write.values)
        for column_name, value in write.values.items():
            before = state["staged"].get(column_name)
            state["staged"][column_name] = value
            state["values"][column_name] = value
            audit_rows.append(
                {
                    "tx_id": transaction.tx_id,
                    "op_index": write.index,
                    "op_type": write.op,
                    "tenant": write.tenant,
                    "table": write.table,
                    "row_key_text": write.row_key_text,
                    "column_name": column_name,
                    "before": before,
                    "after": value,
                    "assertion": canonical_json(write.assertion)
                    if write.assertion is not None
                    else None,
                    "observation": canonical_json(write.recorded_observation)
                    if write.recorded_observation is not None
                    else None,
                }
            )
        if write.op == "conditional_update":
            assertions.append(
                {
                    "tx_id": transaction.tx_id,
                    "op_index": write.index,
                    "tenant": write.tenant,
                    "table": write.table,
                    "row_key": write.row_key,
                    "row_key_text": write.row_key_text,
                    "assertion": write.assertion,
                    "assertion_text": canonical_json(write.assertion),
                    "recorded_observation": write.recorded_observation,
                }
            )
    final_rows: list[FinalRowWrite] = []
    for (tenant, table_name, key_text), state in sorted(grouped.items()):
        table = schema.get_table(tenant, table_name)
        if table is None:
            raise AssertionError("validated table disappeared")
        kind = state["kind"]
        if kind is None:
            continue
        if kind == "delete":
            values: dict[str, Any] = {}
            coverage = tuple(table.column_names)
            whole_row = True
        elif kind == "insert":
            values = dict(sorted(state["values"].items()))
            values[table.primary_key] = state["row_key"]
            coverage = tuple(table.column_names)
            whole_row = True
        else:
            values = dict(sorted(state["values"].items()))
            coverage = tuple(sorted(state["coverage"]))
            whole_row = bool(state["whole_row"])
            if whole_row:
                coverage = tuple(table.column_names)
        final_rows.append(
            FinalRowWrite(
                tx_id=transaction.tx_id,
                tenant=tenant,
                table=table_name,
                row_key=state["row_key"],
                row_key_text=key_text,
                final_kind=kind,
                whole_row=whole_row,
                coverage=coverage,
                values=values,
            )
        )
    return NormalizedTransaction(
        transaction=transaction,
        final_rows=final_rows,
        audit_rows=audit_rows,
        assertions=assertions,
    )


def evaluate_assertion(assertion: Any, values: dict[str, Any]) -> bool:
    if "all" in assertion:
        return all(evaluate_assertion(item, values) for item in assertion["all"])
    if "any" in assertion:
        return any(evaluate_assertion(item, values) for item in assertion["any"])
    if "not" in assertion:
        return not evaluate_assertion(assertion["not"], values)
    column = assertion["column"]
    op = assertion["op"]
    actual = values.get(column)
    if op == "is_null":
        return actual is None
    if op == "not_null":
        return actual is not None
    expected = assertion["value"]
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return actual in expected
    if actual is None:
        return False
    if op == "lt":
        return actual < expected
    if op == "le":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "ge":
        return actual >= expected
    raise AssertionError(f"unknown assertion op {op}")


def iter_assertion_columns(assertion: Any) -> Iterable[str]:
    if "all" in assertion:
        for item in assertion["all"]:
            yield from iter_assertion_columns(item)
    elif "any" in assertion:
        for item in assertion["any"]:
            yield from iter_assertion_columns(item)
    elif "not" in assertion:
        yield from iter_assertion_columns(assertion["not"])
    else:
        yield assertion["column"]
