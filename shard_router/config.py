from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .errors import fail
from .models import (
    Column,
    LoadedSample,
    LogicalTable,
    PhysicalMapping,
    Template,
)
from .util import (
    IDENTIFIER_RE,
    read_json,
    require_dict,
    require_identifier,
    require_int,
    require_list,
    require_str,
)


READ_ONLY_SQL_RE = __import__("re").compile(
    r"^\s*(SELECT|WITH)\b", flags=__import__("re").IGNORECASE
)
FORBIDDEN_SQL_RE = __import__("re").compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    flags=__import__("re").IGNORECASE,
)
PLACEHOLDER_RE = __import__("re").compile(r":([A-Za-z_][A-Za-z0-9_]*)")
SAFE_TYPE_RE = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9_ (),]*$")


def _first_present(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _parse_columns(raw_columns: Any, *, table_name: str) -> tuple[tuple[Column, ...], bool]:
    columns: list[Column] = []
    seen: set[str] = set()
    generated_any = False
    for index, item in enumerate(require_list(raw_columns, label=f"{table_name}.columns")):
        if isinstance(item, str):
            tokens = item.strip().split()
            if len(tokens) < 2:
                fail(
                    "E_VALIDATION",
                    "string column declarations require a name and SQL type",
                    table=table_name,
                    column=item,
                )
            name = require_identifier(tokens[0], label=f"{table_name} column name")
            sql_type = " ".join(tokens[1:2])
            upper = item.upper()
            column = Column(
                name=name,
                sql_type=sql_type,
                primary_key="PRIMARY KEY" in upper,
                not_null="NOT NULL" in upper,
            )
        else:
            obj = require_dict(item, label=f"{table_name}.columns[{index}]")
            name = require_identifier(
                _first_present(obj, "name", "column"), label=f"{table_name} column name"
            )
            sql_type = require_str(
                _first_present(obj, "type", "sql_type", "declared_type", default="TEXT"),
                label=f"{table_name}.{name}.type",
            )
            generated = obj.get("generated")
            expression: str | None = None
            stored = True
            if generated is not None:
                generated_any = True
                if isinstance(generated, str):
                    expression = require_str(
                        generated, label=f"{table_name}.{name}.generated"
                    )
                else:
                    generated_obj = require_dict(
                        generated, label=f"{table_name}.{name}.generated"
                    )
                    expression = require_str(
                        _first_present(generated_obj, "expression", "expr"),
                        label=f"{table_name}.{name}.generated.expression",
                    )
                    stored = bool(generated_obj.get("stored", True))
                if ";" in expression or "--" in expression:
                    fail(
                        "E_VALIDATION",
                        "generated column expression contains forbidden SQL",
                        table=table_name,
                        column=name,
                    )
            column = Column(
                name=name,
                sql_type=sql_type,
                primary_key=bool(obj.get("primary_key", False)),
                not_null=bool(_first_present(obj, "not_null", "notnull", default=False)),
                generated_expression=expression,
                generated_stored=stored,
            )
        if not SAFE_TYPE_RE.fullmatch(column.sql_type):
            fail(
                "E_VALIDATION",
                "column SQL type contains unsupported characters",
                table=table_name,
                column=column.name,
                type=column.sql_type,
            )
        if column.name in seen:
            fail(
                "E_DUPLICATE_LOGICAL_TABLE",
                "duplicate column name",
                table=table_name,
                column=column.name,
            )
        seen.add(column.name)
        columns.append(column)
    if not columns:
        fail("E_VALIDATION", "logical table must declare at least one column", table=table_name)
    return tuple(columns), generated_any


def _validate_boundaries(
    boundaries: Any,
    *,
    shard_count: int,
    label: str,
    key_min: int | None = None,
    key_max: int | None = None,
) -> tuple[int, ...]:
    values = require_list(boundaries, label=label)
    if len(values) != shard_count + 1:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries must contain shard_count + 1 values",
            label=label,
            expected=shard_count + 1,
            actual=len(values),
        )
    parsed = tuple(require_int(value, label=f"{label}[{index}]") for index, value in enumerate(values))
    if any(left >= right for left, right in zip(parsed, parsed[1:])):
        fail("E_BOUNDARIES_INVALID", "range boundaries must be strictly increasing", label=label)
    if key_min is not None and parsed[0] < key_min:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries start below the table shard-key domain",
            label=label,
            minimum=key_min,
            actual=parsed[0],
        )
    if key_max is not None and parsed[-1] > key_max:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries end above the table shard-key domain",
            label=label,
            maximum=key_max,
            actual=parsed[-1],
        )
    return parsed


def load_logical_tables(path: Path) -> tuple[dict[str, LogicalTable], dict[str, list[dict[str, Any]]]]:
    root = read_json(path, label="logical table descriptions")
    if isinstance(root, list):
        items = root
    elif isinstance(root, dict):
        items = root.get("logical_tables", root.get("tables"))
    else:
        items = None
    items = require_list(items, label="logical_tables")
    if not items:
        fail("E_VALIDATION", "at least one logical table is required", path=str(path))

    tables: dict[str, LogicalTable] = {}
    embedded_rows: dict[str, list[dict[str, Any]]] = {}
    for index, raw in enumerate(items):
        obj = require_dict(raw, label=f"logical_tables[{index}]")
        name = require_identifier(
            _first_present(obj, "logical_table", "name"), label=f"logical_tables[{index}].name"
        )
        if name in tables:
            fail("E_DUPLICATE_LOGICAL_TABLE", "logical table names must be unique", table=name)
        shard_key = require_identifier(
            _first_present(obj, "shard_key", "partition_key"),
            label=f"{name}.shard_key",
        )
        rule_type = require_str(
            _first_present(obj, "rule_type", "shard_rule_type", "partition_type"),
            label=f"{name}.rule_type",
        ).lower()
        if rule_type not in {"modulo", "range"}:
            fail(
                "E_RULE_TYPE_INVALID",
                "rule_type must be 'modulo' or 'range'",
                table=name,
                actual=rule_type,
            )
        raw_shard_count = _first_present(obj, "shard_count", "shards")
        if isinstance(raw_shard_count, bool) or not isinstance(raw_shard_count, int):
            fail(
                "E_VALIDATION",
                f"{name}.shard_count must be an integer",
            )
        shard_count = raw_shard_count
        if shard_count <= 0:
            fail("E_MODULUS_INVALID", "shard_count must be positive", table=name)
        generated_flag = bool(
            _first_present(
                obj,
                "generated_columns_enabled",
                "generated_columns",
                "enable_generated_columns",
                default=False,
            )
        )
        columns, generated_any = _parse_columns(
            _first_present(obj, "columns", "schema"),
            table_name=name,
        )
        if shard_key not in {column.name for column in columns}:
            fail(
                "E_SHARD_KEY_MISSING",
                "shard key column is absent from the logical table schema",
                table=name,
                shard_key=shard_key,
            )
        if generated_any and not generated_flag:
            fail(
                "E_VALIDATION",
                "generated columns are declared but generated_columns_enabled is false",
                table=name,
            )
        key_min_raw = _first_present(obj, "shard_key_min", "key_min", "min_key")
        key_max_raw = _first_present(obj, "shard_key_max", "key_max", "max_key")
        key_min = (
            require_int(key_min_raw, label=f"{name}.shard_key_min")
            if key_min_raw is not None
            else None
        )
        key_max = (
            require_int(key_max_raw, label=f"{name}.shard_key_max")
            if key_max_raw is not None
            else None
        )
        if key_min is not None and key_max is not None and key_min >= key_max:
            fail(
                "E_BOUNDARIES_INVALID",
                "shard key domain minimum must be below maximum",
                table=name,
            )
        boundaries_raw = _first_present(obj, "range_boundaries", "boundaries")
        boundaries = (
            _validate_boundaries(
                boundaries_raw,
                shard_count=shard_count,
                label=f"{name}.range_boundaries",
                key_min=key_min,
                key_max=key_max,
            )
            if boundaries_raw is not None
            else None
        )
        table = LogicalTable(
            name=name,
            shard_key=shard_key,
            rule_type=rule_type,
            shard_count=shard_count,
            generated_columns_enabled=generated_flag,
            columns=columns,
            key_min=key_min,
            key_max=key_max,
            range_boundaries=boundaries,
        )
        tables[name] = table
        rows = _first_present(obj, "rows", "seed_rows", "sample_rows")
        if rows is not None:
            embedded_rows[name] = require_list(rows, label=f"{name}.rows")
    return tables, embedded_rows


def _database_is_openable(path: Path, *, table: str, shard_no: int) -> None:
    if path.exists():
        if not path.is_file():
            fail(
                "E_SHARD_DB_UNOPENABLE",
                "physical SQLite path is not a file",
                table=table,
                shard_no=shard_no,
                path=str(path),
            )
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                connection.execute("PRAGMA schema_version").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            fail(
                "E_SHARD_DB_UNOPENABLE",
                "SQLite file cannot be opened read-only",
                table=table,
                shard_no=shard_no,
                path=str(path),
                error=str(exc),
            )
        return
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        fail(
            "E_SHARD_DB_UNOPENABLE",
            "parent directory of physical SQLite file does not exist",
            table=table,
            shard_no=shard_no,
            path=str(path),
        )
    if not __import__("os").access(parent, __import__("os").R_OK | __import__("os").W_OK):
        fail(
            "E_SHARD_DB_UNOPENABLE",
            "parent directory of physical SQLite file is not writable",
            table=table,
            shard_no=shard_no,
            path=str(path),
        )


def load_mappings(
    path: Path,
    *,
    data_dir: Path,
    tables: dict[str, LogicalTable],
) -> dict[str, tuple[PhysicalMapping, ...]]:
    root = read_json(path, label="physical table mappings")
    if isinstance(root, dict):
        raw_mappings = root.get("logical_tables", root.get("mappings", root.get("tables", root)))
    else:
        fail("E_VALIDATION", "physical mappings must be an object")
    raw_mappings = require_dict(raw_mappings, label="physical mappings")

    extra_tables = sorted(set(raw_mappings) - set(tables))
    if extra_tables:
        fail(
            "E_MAPPING_INCOMPLETE",
            "physical mappings reference unknown logical tables",
            tables=extra_tables,
        )

    result: dict[str, tuple[PhysicalMapping, ...]] = {}
    for table_name, table in tables.items():
        raw = raw_mappings.get(table_name)
        if isinstance(raw, dict) and "shards" in raw:
            raw = raw["shards"]
        entries = require_list(raw, label=f"mappings.{table_name}")
        by_shard: dict[int, PhysicalMapping] = {}
        seen_databases: set[Path] = set()
        for index, item in enumerate(entries):
            obj = require_dict(item, label=f"mappings.{table_name}[{index}]")
            shard_no = require_int(
                _first_present(obj, "shard_no", "shard", "shard_id"),
                label=f"mappings.{table_name}[{index}].shard_no",
                minimum=0,
            )
            if shard_no >= table.shard_count:
                fail(
                    "E_MAPPING_INCOMPLETE",
                    "mapping shard number is outside the table shard range",
                    table=table_name,
                    shard_no=shard_no,
                    shard_count=table.shard_count,
                )
            if shard_no in by_shard:
                fail(
                    "E_MAPPING_INCOMPLETE",
                    "a shard must have exactly one physical mapping",
                    table=table_name,
                    shard_no=shard_no,
                )
            database_ref = require_str(
                _first_present(obj, "database", "database_file", "path", "sqlite_file"),
                label=f"mappings.{table_name}[{index}].database",
            )
            database = Path(database_ref)
            if not database.is_absolute():
                database = (data_dir / database).resolve()
            else:
                database = database.resolve()
            physical_table = require_identifier(
                _first_present(obj, "physical_table", "table", "table_name"),
                label=f"mappings.{table_name}[{index}].physical_table",
            )
            raw_role = _first_present(obj, "role", "replica_role", "primary_replica")
            if raw_role is None and "is_primary" in obj:
                raw_role = "primary" if obj["is_primary"] else "replica"
            role = require_str(raw_role if raw_role is not None else "primary", label="role").lower()
            if role not in {"primary", "replica", "standby", "backup"}:
                fail(
                    "E_VALIDATION",
                    "mapping role must be primary/replica/standby/backup",
                    table=table_name,
                    shard_no=shard_no,
                    role=role,
                )
            _database_is_openable(database, table=table_name, shard_no=shard_no)
            if database in seen_databases:
                fail(
                    "E_MAPPING_INCOMPLETE",
                    "two shards cannot share one SQLite database file",
                    table=table_name,
                    database=str(database),
                )
            seen_databases.add(database)
            by_shard[shard_no] = PhysicalMapping(
                logical_table=table_name,
                shard_no=shard_no,
                database=database,
                database_ref=database_ref,
                physical_table=physical_table,
                role=role,
            )
        missing = sorted(set(range(table.shard_count)) - set(by_shard))
        if missing:
            fail(
                "E_MAPPING_INCOMPLETE",
                "physical mappings do not cover every shard",
                table=table_name,
                missing_shards=missing,
            )
        if not any(mapping.role == "primary" for mapping in by_shard.values()):
            fail(
                "E_MAPPING_INCOMPLETE",
                "each table must designate at least one primary shard mapping",
                table=table_name,
            )
        result[table_name] = tuple(by_shard[index] for index in range(table.shard_count))
    return result


def load_templates(path: Path, *, tables: dict[str, LogicalTable]) -> dict[str, Template]:
    root = read_json(path, label="query templates")
    items = root.get("templates") if isinstance(root, dict) else root
    items = require_list(items, label="templates")
    result: dict[str, Template] = {}
    for index, raw in enumerate(items):
        obj = require_dict(raw, label=f"templates[{index}]")
        template_id = require_identifier(
            _first_present(obj, "id", "template_id", "name"),
            label=f"templates[{index}].id",
        )
        if template_id in result:
            fail("E_TEMPLATE_INVALID", "template ids must be unique", template=template_id)
        table_name = require_str(
            _first_present(obj, "logical_table", "table"), label=f"{template_id}.logical_table"
        )
        if table_name not in tables:
            fail(
                "E_TEMPLATE_INVALID",
                "template references an unknown logical table",
                template=template_id,
                table=table_name,
            )
        kind = require_str(
            _first_present(obj, "kind", "query_kind", "route_type"),
            label=f"{template_id}.kind",
        ).lower()
        aliases = {"eq": "equal", "equal": "equal", "range": "range", "in": "in", "all": "all", "full": "all", "all_route": "all"}
        kind = aliases.get(kind, kind)
        if kind not in {"equal", "range", "in", "all"}:
            fail(
                "E_TEMPLATE_INVALID",
                "template kind must be equal, range, in, or all",
                template=template_id,
                kind=kind,
            )
        logical_sql = require_str(
            _first_present(obj, "logical_sql", "sql", "query"),
            label=f"{template_id}.logical_sql",
        ).strip()
        if logical_sql.endswith(";"):
            logical_sql = logical_sql[:-1].rstrip()
        if ";" in logical_sql:
            fail(
                "E_TEMPLATE_INVALID",
                "template SQL must contain one read-only statement",
                template=template_id,
            )
        if not READ_ONLY_SQL_RE.match(logical_sql) or FORBIDDEN_SQL_RE.search(logical_sql):
            fail(
                "E_TEMPLATE_INVALID",
                "template SQL must be read-only SELECT/WITH",
                template=template_id,
            )
        if not __import__("re").search(
            rf"\b{__import__('re').escape(table_name)}\b", logical_sql
        ):
            fail(
                "E_TEMPLATE_INVALID",
                "template SQL does not reference its logical table",
                template=template_id,
                table=table_name,
            )
        sql_parameters = tuple(dict.fromkeys(PLACEHOLDER_RE.findall(logical_sql)))
        declared_parameters = obj.get("parameters")
        parameters = (
            tuple(require_str(item, label=f"{template_id}.parameters") for item in require_list(declared_parameters, label=f"{template_id}.parameters"))
            if declared_parameters is not None
            else sql_parameters
        )
        if set(parameters) != set(sql_parameters):
            fail(
                "E_TEMPLATE_INVALID",
                "declared parameters do not match SQL named parameters",
                template=template_id,
                declared=list(parameters),
                sql=list(sql_parameters),
            )
        shard_key_param = _first_present(obj, "shard_key_param", "key_param")
        if shard_key_param is not None:
            shard_key_param = require_str(shard_key_param, label=f"{template_id}.shard_key_param")
        shard_key_params_raw = _first_present(
            obj, "shard_key_params", "key_params", "in_params", "range_params"
        )
        shard_key_params: tuple[str, ...] = ()
        if isinstance(shard_key_params_raw, dict):
            keys = sorted(shard_key_params_raw)
            shard_key_params = tuple(
                require_str(shard_key_params_raw[key], label=f"{template_id}.shard_key_params.{key}")
                for key in keys
            )
        elif shard_key_params_raw is not None:
            shard_key_params = tuple(
                require_str(item, label=f"{template_id}.shard_key_params")
                for item in require_list(shard_key_params_raw, label=f"{template_id}.shard_key_params")
            )
        if kind == "equal":
            if shard_key_param is None:
                if len(parameters) != 1:
                    fail(
                        "E_TEMPLATE_INVALID",
                        "equal templates require shard_key_param or exactly one parameter",
                        template=template_id,
                    )
                shard_key_param = parameters[0]
            if shard_key_param not in parameters:
                fail(
                    "E_TEMPLATE_INVALID",
                    "equal shard key parameter is not declared",
                    template=template_id,
                    parameter=shard_key_param,
                )
        elif kind == "range":
            if not shard_key_params:
                shard_key_params = parameters[:2]
            if len(shard_key_params) != 2:
                fail(
                    "E_TEMPLATE_INVALID",
                    "range templates require exactly two shard key parameters",
                    template=template_id,
                )
            if any(item not in parameters for item in shard_key_params):
                fail(
                    "E_TEMPLATE_INVALID",
                    "range shard key parameters are not declared",
                    template=template_id,
                )
        elif kind == "in":
            if not shard_key_params:
                shard_key_params = parameters
            if not shard_key_params:
                fail(
                    "E_TEMPLATE_INVALID",
                    "IN templates require at least one shard key parameter",
                    template=template_id,
                )
            if any(item not in parameters for item in shard_key_params):
                fail(
                    "E_TEMPLATE_INVALID",
                    "IN shard key parameters are not declared",
                    template=template_id,
                )
        elif shard_key_params:
            fail(
                "E_TEMPLATE_INVALID",
                "all-route templates cannot declare shard key parameters",
                template=template_id,
            )
        order_by_raw = _first_present(obj, "order_by", "sort_by", default=[])
        order_by = (
            tuple(
                require_identifier(item, label=f"{template_id}.order_by")
                for item in require_list(order_by_raw, label=f"{template_id}.order_by")
            )
            if not isinstance(order_by_raw, str)
            else (require_identifier(order_by_raw, label=f"{template_id}.order_by"),)
        )
        aggregate_raw = obj.get("aggregate", {})
        aggregate = require_dict(aggregate_raw, label=f"{template_id}.aggregate") if aggregate_raw else {}
        aggregate_count = bool(aggregate.get("count", True))
        aggregate_sum_raw = aggregate.get("sum")
        aggregate_sum = (
            require_identifier(aggregate_sum_raw, label=f"{template_id}.aggregate.sum")
            if aggregate_sum_raw is not None
            else None
        )
        table = tables[table_name]
        if aggregate_sum is not None and aggregate_sum not in table.column_names:
            fail(
                "E_TEMPLATE_INVALID",
                "aggregate sum column is absent from the table",
                template=template_id,
                column=aggregate_sum,
            )
        if any(column not in table.column_names for column in order_by):
            fail(
                "E_TEMPLATE_INVALID",
                "order_by column is absent from the table",
                template=template_id,
            )
        result[template_id] = Template(
            template_id=template_id,
            logical_table=table_name,
            kind=kind,
            logical_sql=logical_sql,
            parameters=parameters,
            shard_key_params=shard_key_params,
            shard_key_param=shard_key_param,
            order_by=order_by,
            aggregate_count=aggregate_count,
            aggregate_sum=aggregate_sum,
            description=str(obj.get("description", "")),
        )
    return result


def validate_seed_rows(
    rows_by_table: dict[str, list[dict[str, Any]]],
    *,
    tables: dict[str, LogicalTable],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table_name, rows in rows_by_table.items():
        if table_name not in tables:
            fail("E_VALIDATION", "seed rows reference an unknown table", table=table_name)
        table = tables[table_name]
        seen_primary: set[Any] = set()
        primary_columns = [column.name for column in table.non_generated_columns if column.primary_key]
        parsed_rows: list[dict[str, Any]] = []
        for index, raw in enumerate(rows):
            row = require_dict(raw, label=f"seed.{table_name}[{index}]")
            unknown = sorted(set(row) - table.column_names)
            if unknown:
                fail(
                    "E_VALIDATION",
                    "seed row contains unknown columns",
                    table=table_name,
                    row=index,
                    columns=unknown,
                )
            generated = {
                column.name
                for column in table.columns
                if column.generated_expression is not None
            }
            inserted_generated = sorted(generated & set(row))
            if inserted_generated:
                fail(
                    "E_VALIDATION",
                    "seed rows cannot assign generated columns",
                    table=table_name,
                    row=index,
                    columns=inserted_generated,
                )
            if table.shard_key not in row:
                fail(
                    "E_SHARD_KEY_MISSING",
                    "seed row is missing the shard key",
                    table=table_name,
                    row=index,
                    shard_key=table.shard_key,
                )
            require_int(
                row[table.shard_key],
                label=f"seed.{table_name}[{index}].{table.shard_key}",
            )
            for name in primary_columns:
                if name in row:
                    if row[name] in seen_primary:
                        fail(
                            "E_VALIDATION",
                            "seed primary key values must be unique",
                            table=table_name,
                            column=name,
                            value=row[name],
                        )
                    seen_primary.add(row[name])
            parsed_rows.append(dict(row))
        result[table_name] = parsed_rows
    for table_name in tables:
        result.setdefault(table_name, [])
    return result


def load_seed_data(path: Path, *, tables: dict[str, LogicalTable], embedded: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    rows = dict(embedded)
    if path.exists():
        root = read_json(path, label="seed rows")
        if isinstance(root, dict):
            raw_rows = root.get("rows", root.get("seed_rows", root))
        else:
            fail("E_VALIDATION", "seed data must be an object")
        raw_rows = require_dict(raw_rows, label="seed rows")
        for table_name, table_rows in raw_rows.items():
            rows[table_name] = require_list(table_rows, label=f"seed.{table_name}")
    return validate_seed_rows(rows, tables=tables)


def load_injection_file(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    root = read_json(path, label="injection manifest")
    items = root.get("injections") if isinstance(root, dict) else root
    items = require_list(items, label="injections")
    result: list[dict[str, Any]] = []
    allowed_stages = {"route", "rewrite", "execute", "merge"}
    default_codes = {
        "rule_version_conflict": "E_RULE_VERSION_CONFLICT",
        "shard_key_out_of_range": "E_SHARD_KEY_OUT_OF_RANGE",
        "physical_table_missing": "E_PHYSICAL_TABLE_MISSING",
        "shard_result_digest_mismatch": "E_SHARD_RESULT_DIGEST_MISMATCH",
        "execution_failed": "E_EXECUTION_FAILED",
    }
    allowed_actions = {
        "raise",
        "fail_shard",
        "abandon_shard",
        "corrupt_digest",
        "crash",
        "delay",
        "none",
    }
    for index, raw in enumerate(items):
        obj = require_dict(raw, label=f"injections[{index}]")
        if not bool(obj.get("enabled", True)):
            continue
        stage = require_str(obj.get("stage"), label=f"injections[{index}].stage").lower()
        if stage not in allowed_stages:
            fail(
                "E_INJECTION_INVALID",
                "injection stage must be route/rewrite/execute/merge",
                injection=index,
            )
        error_type = require_str(
            obj.get("error_type"), label=f"injections[{index}].error_type"
        )
        if error_type not in default_codes:
            fail(
                "E_INJECTION_INVALID",
                "unknown injection error_type",
                injection=index,
                error_type=error_type,
            )
        action = str(obj.get("action", "raise")).lower()
        if action not in allowed_actions:
            fail(
                "E_INJECTION_INVALID",
                "unknown injection action",
                injection=index,
                action=action,
            )
        if stage == "execute" and action not in {
            "fail_shard",
            "abandon_shard",
            "crash",
            "delay",
            "raise",
        }:
            fail(
                "E_INJECTION_INVALID",
                "execute-stage injection action is incompatible with the stage",
                injection=index,
                action=action,
            )
        if stage == "merge" and action not in {"raise", "corrupt_digest", "crash", "delay"}:
            fail(
                "E_INJECTION_INVALID",
                "merge-stage injection action is incompatible with the stage",
                injection=index,
                action=action,
            )
        shard_no = obj.get("shard_no")
        if shard_no is not None:
            shard_no = require_int(shard_no, label=f"injections[{index}].shard_no", minimum=0)
        delay_ms = obj.get("delay_ms", 0)
        delay_ms = require_int(delay_ms, label=f"injections[{index}].delay_ms", minimum=0)
        result.append(
            {
                "id": str(obj.get("id", f"injection-{index}")),
                "enabled": True,
                "stage": stage,
                "error_type": error_type,
                "code": str(obj.get("code", default_codes[error_type])),
                "action": action,
                "template_id": obj.get("template_id"),
                "query_id": obj.get("query_id"),
                "shard_no": shard_no,
                "position": str(obj.get("position", "before")),
                "params": obj.get("params"),
                "delay_ms": delay_ms,
                "message": str(obj.get("message", f"injected {error_type}")),
                "details": obj.get("details", {}),
            }
        )
    return result


def load_sample(
    data_dir: Path,
    *,
    injection_path: Path | None = None,
    v1_base_clock_ms: int | None = None,
    source_filter: str = "merged",
    load_rule_snapshots: bool = True,
) -> LoadedSample:
    from .rules import build_rule_models, load_v1_rules, load_v2_rules

    data_dir = data_dir.resolve()
    tables_path = data_dir / "logical_tables.json"
    mappings_path = data_dir / "physical_mapping.json"
    if not mappings_path.exists():
        alternate = data_dir / "physical_mappings.json"
        if alternate.exists():
            mappings_path = alternate
    templates_path = data_dir / "templates.json"
    v1_path = data_dir / "rules" / "v1.json"
    v2_dir = data_dir / "rules" / "v2"

    tables, embedded_rows = load_logical_tables(tables_path)
    mappings = load_mappings(mappings_path, data_dir=data_dir, tables=tables)
    templates = load_templates(templates_path, tables=tables)
    if source_filter not in {"merged", "v1", "v2"}:
        fail("E_VALIDATION", "source_filter must be merged, v1, or v2", actual=source_filter)

    if load_rule_snapshots:
        v1 = (
            load_v1_rules(
                v1_path,
                tables=tables,
                base_clock_override_ms=v1_base_clock_ms,
            )
            if source_filter in {"merged", "v1"}
            else []
        )
        v2 = (
            load_v2_rules(v2_dir, tables=tables)
            if source_filter in {"merged", "v2"}
            else []
        )
        merged, v1_models, v2_models = build_rule_models(v1, v2, tables=tables)
        if not merged:
            fail("E_RULE_NOT_FOUND", "no routing rule versions were loaded")
    else:
        v1 = []
        v2 = []
        merged = []
        v1_models = []
        v2_models = []
    seed_rows = load_seed_data(
        data_dir / "seed_data.json",
        tables=tables,
        embedded=embedded_rows,
    )
    injections = load_injection_file(injection_path)
    if load_rule_snapshots:
        initial_clock = (
            v1_base_clock_ms
            if v1_base_clock_ms is not None
            else min(
                (candidate.effective_clock_ms for candidate in v1),
                default=min(rule.effective_clock_ms for rule in merged),
            )
        )
    else:
        initial_clock = v1_base_clock_ms if v1_base_clock_ms is not None else 0
    if initial_clock < 0:
        fail("E_V1_EFFECT_INVALID", "initial logical clock must be non-negative")
    return LoadedSample(
        data_dir=data_dir,
        tables=tables,
        mappings=mappings,
        templates=templates,
        merged_rules=merged,
        v1_versions=v1_models,
        v2_versions=v2_models,
        injections=injections,
        initial_clock_ms=initial_clock,
        seed_rows=seed_rows,
    )
