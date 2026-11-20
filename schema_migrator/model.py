from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .errors import error
from .sqlparse import normalize_sql_text, strip_create_view_prefix
from .typesys import TypeInfo, default_compatible, parse_type
from .util import canonical_json, digest_json


NAME_MAX_LENGTH = 128


def parse_version(value: Any, path: str = "version") -> int:
    if isinstance(value, bool):
        raise error("E_VERSION", "version must be an integer", path=path, value=value)
    if isinstance(value, int):
        version = value
    elif isinstance(value, str) and value.strip().lower().startswith("v"):
        try:
            version = int(value.strip()[1:])
        except ValueError:
            raise error("E_VERSION", "invalid version string", path=path, value=value)
    else:
        raise error("E_VERSION", "version must be an integer or vN string", path=path, value=value)
    if version < 0:
        raise error("E_VERSION", "version must be non-negative", path=path, value=value)
    return version


def validate_name(value: Any, kind: str, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > NAME_MAX_LENGTH:
        raise error(
            "E_NAME",
            f"{kind} name must be a non-empty string of at most {NAME_MAX_LENGTH} characters",
            path=path,
            value=value,
        )
    if "\x00" in value or "." in value:
        raise error("E_NAME", f"{kind} name contains an unsupported character", path=path, value=value)
    return value


def _string_list(value: Any, path: str, allow_empty: bool = False) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise error("E_SCHEMA", "expected a string or list of strings", path=path)
    if not values and not allow_empty:
        raise error("E_SCHEMA", "list must not be empty", path=path)
    result: List[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str) or not item:
            raise error("E_SCHEMA", "list entries must be non-empty strings", path=f"{path}[{index}]")
        result.append(item)
    return result


def normalize_reference(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise error("E_DEPENDENCY", "dependency reference must be a non-empty string", path=path)
    if ":" in value:
        kind, name = value.split(":", 1)
        if kind not in ("column", "table", "index", "view", "trigger"):
            raise error("E_DEPENDENCY", "unknown dependency kind", path=path, value=value)
        if not name:
            raise error("E_DEPENDENCY", "dependency name is empty", path=path, value=value)
        if kind == "column":
            if "." not in name:
                raise error("E_DEPENDENCY", "column dependency must be table.column", path=path, value=value)
            table, column = name.split(".", 1)
            if not table or not column or "." in column:
                raise error("E_DEPENDENCY", "invalid column dependency", path=path, value=value)
            return f"column:{table}.{column}"
        return f"{kind}:{name}"
    if "." in value:
        table, column = value.split(".", 1)
        if table and column and "." not in column:
            return f"column:{table}.{column}"
    return f"table:{value}"


def _normalize_references(value: Any, path: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise error("E_DEPENDENCY", "references must be a list", path=path)
    refs = [normalize_reference(item, f"{path}[{index}]") for index, item in enumerate(value)]
    return sorted(set(refs))


def _normalize_column(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_SCHEMA", "column must be an object", path=path)
    name = validate_name(raw.get("name"), "column", f"{path}.name")
    type_info = parse_type(raw.get("type"), f"{path}.type")
    not_null = bool(raw.get("not_null", False))
    primary_key = bool(raw.get("primary_key", False))
    unique = bool(raw.get("unique", False))
    has_default = "default" in raw
    default = raw.get("default")
    if has_default:
        ok, reason = default_compatible(type_info, default)
        if not ok:
            raise error(
                "E_DEFAULT",
                "default value is incompatible with the column type",
                path=f"{path}.default",
                column=name,
                type=type_info.canonical,
                value=default,
                reason=reason,
            )
        if not_null and default is None:
            raise error(
                "E_DEFAULT",
                "NOT NULL column cannot have an explicit NULL default",
                path=f"{path}.default",
                column=name,
            )
    column = {
        "name": name,
        "type": type_info.canonical,
        "not_null": not_null,
        "primary_key": primary_key,
        "unique": unique,
        "has_default": has_default,
    }
    if has_default:
        column["default"] = default
    return column


def _normalize_foreign_key(raw: Dict[str, Any], table_name: str, path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_FOREIGN_KEY", "foreign key must be an object", path=path)
    columns = _string_list(
        raw.get("columns", raw.get("column", raw.get("from_columns"))),
        f"{path}.columns",
    )
    ref_table_raw = raw.get("ref_table", raw.get("references_table", raw.get("table")))
    ref_table = validate_name(ref_table_raw, "table", f"{path}.ref_table")
    ref_columns = _string_list(
        raw.get("ref_columns", raw.get("references_columns", raw.get("ref_column", raw.get("to_columns")))),
        f"{path}.ref_columns",
    )
    if len(columns) != len(ref_columns):
        raise error(
            "E_FOREIGN_KEY",
            "foreign key column lists must have the same length",
            path=path,
            table=table_name,
        )
    on_delete = str(raw.get("on_delete", "NO ACTION")).upper().replace("_", " ")
    allowed = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
    if on_delete not in allowed:
        raise error("E_FOREIGN_KEY", "unsupported ON DELETE action", path=path, value=on_delete)
    return {
        "columns": columns,
        "ref_table": ref_table,
        "ref_columns": ref_columns,
        "on_delete": on_delete,
    }


def _normalize_table(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_SCHEMA", "table must be an object", path=path)
    name = validate_name(raw.get("name"), "table", f"{path}.name")
    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise error("E_SCHEMA", "table columns must be a non-empty list", path=f"{path}.columns")
    columns = [
        _normalize_column(column, f"{path}.columns[{index}]")
        for index, column in enumerate(columns_raw)
    ]
    column_names: Set[str] = set()
    for column in columns:
        if column["name"] in column_names:
            raise error(
                "E_DUPLICATE_NAME",
                "duplicate column name",
                path=f"{path}.columns",
                table=name,
                name=column["name"],
            )
        column_names.add(column["name"])
    fks_raw = raw.get("foreign_keys", [])
    if fks_raw is None:
        fks_raw = []
    if not isinstance(fks_raw, list):
        raise error("E_FOREIGN_KEY", "foreign_keys must be a list", path=f"{path}.foreign_keys")
    foreign_keys = [
        _normalize_foreign_key(fk, name, f"{path}.foreign_keys[{index}]")
        for index, fk in enumerate(fks_raw)
    ]
    seed_rows = raw.get("rows", [])
    if seed_rows is None:
        seed_rows = []
    if not isinstance(seed_rows, list):
        raise error("E_SCHEMA", "table rows must be a list", path=f"{path}.rows")
    return {"name": name, "columns": columns, "foreign_keys": foreign_keys, "rows": seed_rows}


def _normalize_index(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_INDEX", "index must be an object", path=path)
    name = validate_name(raw.get("name"), "index", f"{path}.name")
    table = validate_name(raw.get("table"), "table", f"{path}.table")
    columns = _string_list(raw.get("columns", raw.get("column")), f"{path}.columns")
    return {"name": name, "table": table, "columns": columns, "unique": bool(raw.get("unique", False))}


def _normalize_view(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_VIEW", "view must be an object", path=path)
    name = validate_name(raw.get("name"), "view", f"{path}.name")
    sql = raw.get("sql", raw.get("definition"))
    if not isinstance(sql, str) or not sql.strip():
        raise error("E_VIEW", "view SQL must be a non-empty string", path=f"{path}.sql")
    references = _normalize_references(raw.get("references", raw.get("dependencies")), f"{path}.references")
    output_columns = raw.get("columns")
    if output_columns is not None:
        output_columns = _string_list(output_columns, f"{path}.columns")
    return {
        "name": name,
        "sql": strip_create_view_prefix(sql.strip(), name),
        "references": references,
        "output_columns": output_columns,
    }


def _normalize_trigger(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_TRIGGER", "trigger must be an object", path=path)
    name = validate_name(raw.get("name"), "trigger", f"{path}.name")
    table = validate_name(raw.get("table"), "table", f"{path}.table")
    sql = raw.get("sql", raw.get("definition"))
    if not isinstance(sql, str) or not sql.strip():
        raise error("E_TRIGGER", "trigger SQL must be a non-empty string", path=f"{path}.sql")
    references = _normalize_references(raw.get("references", raw.get("dependencies")), f"{path}.references")
    return {"name": name, "table": table, "sql": sql.strip(), "references": references}


def normalize_schema(raw: Dict[str, Any], path: str = "schema") -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_SCHEMA", "schema version must be an object", path=path)
    version = parse_version(raw.get("version", raw.get("schema_version")), f"{path}.version")
    database = raw.get("database", raw.get("database_id", "local"))
    database = validate_name(database, "database", f"{path}.database")
    tables_raw = raw.get("tables")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise error("E_SCHEMA", "tables must be a non-empty list", path=f"{path}.tables")
    tables = [
        _normalize_table(table, f"{path}.tables[{index}]")
        for index, table in enumerate(tables_raw)
    ]
    indexes_raw = raw.get("indexes", [])
    views_raw = raw.get("views", [])
    triggers_raw = raw.get("triggers", [])
    if not isinstance(indexes_raw, list):
        raise error("E_INDEX", "indexes must be a list", path=f"{path}.indexes")
    if not isinstance(views_raw, list):
        raise error("E_VIEW", "views must be a list", path=f"{path}.views")
    if not isinstance(triggers_raw, list):
        raise error("E_TRIGGER", "triggers must be a list", path=f"{path}.triggers")
    indexes = [_normalize_index(item, f"{path}.indexes[{i}]") for i, item in enumerate(indexes_raw)]
    views = [_normalize_view(item, f"{path}.views[{i}]") for i, item in enumerate(views_raw)]
    triggers = [_normalize_trigger(item, f"{path}.triggers[{i}]") for i, item in enumerate(triggers_raw)]
    seed_rows = raw.get("seed_rows")
    if seed_rows is not None:
        if not isinstance(seed_rows, dict):
            raise error("E_SCHEMA", "seed_rows must be an object keyed by table", path=f"{path}.seed_rows")
        table_map = {table["name"]: table for table in tables}
        for table_name, rows in seed_rows.items():
            if table_name not in table_map:
                raise error(
                    "E_SCHEMA",
                    "seed_rows references an unknown table",
                    path=f"{path}.seed_rows",
                    table=table_name,
                )
            if not isinstance(rows, list):
                raise error("E_SCHEMA", "seed rows must be a list", path=f"{path}.seed_rows.{table_name}")
            table_map[table_name]["rows"] = rows
    schema = {
        "version": version,
        "database": database,
        "description": raw.get("description", ""),
        "tables": tables,
        "indexes": indexes,
        "views": views,
        "triggers": triggers,
    }
    canonicalize_schema_in_place(schema)
    return schema


def canonicalize_schema_in_place(schema: Dict[str, Any]) -> None:
    schema["tables"] = sorted(schema.get("tables", []), key=lambda item: item["name"])
    schema["indexes"] = sorted(schema.get("indexes", []), key=lambda item: item["name"])
    schema["views"] = sorted(schema.get("views", []), key=lambda item: item["name"])
    schema["triggers"] = sorted(schema.get("triggers", []), key=lambda item: item["name"])
    for table in schema["tables"]:
        for column in table.get("columns", []):
            column["has_default"] = bool(column.get("has_default", "default" in column))
            if not column["has_default"]:
                column.pop("default", None)
        table["foreign_keys"] = table.get("foreign_keys", [])
    for item in schema["indexes"]:
        item["columns"] = list(item.get("columns", []))
        item["unique"] = bool(item.get("unique", False))
    for item in schema["views"]:
        item["references"] = sorted(set(item.get("references", [])))
    for item in schema["triggers"]:
        item["references"] = sorted(set(item.get("references", [])))


def schema_for_digest(schema: Dict[str, Any], include_seed: bool = False) -> Dict[str, Any]:
    result = copy.deepcopy(schema)
    result.pop("description", None)
    for table in result.get("tables", []):
        if not include_seed:
            table.pop("rows", None)
        else:
            table["rows"] = list(table.get("rows", []))
    return result


def schema_digest(schema: Dict[str, Any]) -> str:
    return digest_json(schema_for_digest(schema, include_seed=False))


def schema_object_map(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    objects: Dict[str, Dict[str, Any]] = {}
    for table in schema.get("tables", []):
        table_copy = copy.deepcopy(table)
        table_copy.pop("rows", None)
        objects[f"table:{table['name']}"] = {
            "type": "table",
            "name": table["name"],
            "digest": digest_json(table_copy),
        }
        for column in table.get("columns", []):
            objects[f"column:{table['name']}.{column['name']}"] = {
                "type": "column",
                "name": f"{table['name']}.{column['name']}",
                "digest": digest_json({"table": table["name"], **column}),
            }
    for index in schema.get("indexes", []):
        objects[f"index:{index['name']}"] = {
            "type": "index",
            "name": index["name"],
            "digest": digest_json(index),
        }
    for view in schema.get("views", []):
        objects[f"view:{view['name']}"] = {
            "type": "view",
            "name": view["name"],
            "digest": digest_json(view),
        }
    for trigger in schema.get("triggers", []):
        objects[f"trigger:{trigger['name']}"] = {
            "type": "trigger",
            "name": trigger["name"],
            "digest": digest_json(trigger),
        }
    return objects


def _find_table(schema: Dict[str, Any], name: str, path: str) -> Dict[str, Any]:
    for table in schema.get("tables", []):
        if table["name"] == name:
            return table
    raise error("E_OBJECT_NOT_FOUND", "table does not exist", path=path, table=name)


def _find_column(table: Dict[str, Any], name: str, path: str) -> Dict[str, Any]:
    for column in table.get("columns", []):
        if column["name"] == name:
            return column
    raise error(
        "E_OBJECT_NOT_FOUND",
        "column does not exist",
        path=path,
        table=table["name"],
        column=name,
    )


def normalize_operation(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_MIGRATION_OP", "migration operation must be an object", path=path)
    op_name = raw.get("op", raw.get("action", raw.get("type")))
    if not isinstance(op_name, str):
        raise error("E_MIGRATION_OP", "migration operation is missing an op name", path=path)
    op_name = op_name.strip().lower()
    allowed = {
        "add_column",
        "alter_column_type",
        "drop_column",
        "create_index",
        "drop_index",
        "replace_view",
        "replace_trigger",
        "rename_table",
        "rename_column",
    }
    if op_name not in allowed:
        raise error("E_MIGRATION_OP", "unsupported migration operation", path=path, op=op_name)
    operation: Dict[str, Any] = {"op": op_name}
    if op_name == "add_column":
        operation["table"] = validate_name(raw.get("table"), "table", f"{path}.table")
        column = _normalize_column(
            {
                "name": raw.get("column", raw.get("name")),
                "type": raw.get("column_type", raw.get("data_type", raw.get("type"))),
                "not_null": raw.get("not_null", False),
                "primary_key": raw.get("primary_key", False),
                "unique": raw.get("unique", False),
                **({"default": raw["default"]} if "default" in raw else {}),
            },
            f"{path}.column",
        )
        operation["column"] = column
    elif op_name == "alter_column_type":
        operation["table"] = validate_name(raw.get("table"), "table", f"{path}.table")
        operation["column"] = validate_name(raw.get("column"), "column", f"{path}.column")
        new_type = parse_type(raw.get("new_type", raw.get("column_type", raw.get("data_type"))), f"{path}.new_type")
        operation["new_type"] = new_type.canonical
        conversion = raw.get("conversion", {})
        if conversion is None:
            conversion = {}
        if not isinstance(conversion, dict):
            raise error("E_MIGRATION_OP", "conversion must be an object", path=f"{path}.conversion")
        rounding = str(conversion.get("rounding", "ROUND_HALF_EVEN")).upper()
        if rounding not in {"ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"}:
            raise error("E_MIGRATION_OP", "unsupported rounding mode", path=f"{path}.conversion.rounding")
        operation["conversion"] = {
            "source_scale": conversion.get("source_scale"),
            "target_scale": conversion.get("target_scale"),
            "rounding": rounding,
        }
    elif op_name == "drop_column":
        operation["table"] = validate_name(raw.get("table"), "table", f"{path}.table")
        operation["column"] = validate_name(raw.get("column"), "column", f"{path}.column")
    elif op_name == "create_index":
        operation.update(_normalize_index(raw, path))
        operation["op"] = "create_index"
    elif op_name == "drop_index":
        operation["name"] = validate_name(raw.get("name", raw.get("index")), "index", f"{path}.name")
    elif op_name == "replace_view":
        operation.update(_normalize_view(raw, path))
        operation["op"] = "replace_view"
    elif op_name == "replace_trigger":
        operation.update(_normalize_trigger(raw, path))
        operation["op"] = "replace_trigger"
    elif op_name == "rename_table":
        operation["old_name"] = validate_name(raw.get("old_name", raw.get("table")), "table", f"{path}.old_name")
        operation["new_name"] = validate_name(raw.get("new_name"), "table", f"{path}.new_name")
    elif op_name == "rename_column":
        operation["table"] = validate_name(raw.get("table"), "table", f"{path}.table")
        operation["old_name"] = validate_name(raw.get("old_name", raw.get("column")), "column", f"{path}.old_name")
        operation["new_name"] = validate_name(raw.get("new_name"), "column", f"{path}.new_name")
    return operation


def operation_target(operation: Dict[str, Any]) -> str:
    op = operation["op"]
    if op == "add_column":
        return f"column:{operation['table']}.{operation['column']['name']}"
    if op in ("alter_column_type", "drop_column"):
        return f"column:{operation['table']}.{operation['column']}"
    if op in ("create_index", "drop_index"):
        return f"index:{operation['name']}"
    if op in ("replace_view", "drop_view"):
        return f"view:{operation['name']}"
    if op in ("replace_trigger", "drop_trigger"):
        return f"trigger:{operation['name']}"
    if op == "rename_table":
        return f"table:{operation['old_name']}"
    if op == "rename_column":
        return f"column:{operation['table']}.{operation['old_name']}"
    return f"{op}:unknown"


def operation_digest(operation: Dict[str, Any]) -> str:
    return digest_json(operation)


def apply_operation(schema: Dict[str, Any], operation: Dict[str, Any], path: str = "operation") -> Dict[str, Any]:
    result = copy.deepcopy(schema)
    op = operation["op"]
    if op == "add_column":
        table = _find_table(result, operation["table"], path)
        if any(column["name"] == operation["column"]["name"] for column in table["columns"]):
            raise error(
                "E_OBJECT_EXISTS",
                "column already exists",
                path=path,
                table=table["name"],
                column=operation["column"]["name"],
            )
        if operation["column"].get("primary_key"):
            raise error("E_MIGRATION_OP", "add_column cannot add a primary-key column", path=path)
        if operation["column"].get("not_null") and not operation["column"].get("has_default"):
            raise error(
                "E_MIGRATION_OP",
                "SQLite requires a default when adding a NOT NULL column",
                path=path,
                table=table["name"],
                column=operation["column"]["name"],
            )
        table["columns"].append(copy.deepcopy(operation["column"]))
    elif op == "alter_column_type":
        table = _find_table(result, operation["table"], path)
        column = _find_column(table, operation["column"], path)
        column["type"] = operation["new_type"]
    elif op == "drop_column":
        table = _find_table(result, operation["table"], path)
        _find_column(table, operation["column"], path)
        if any(column["name"] == operation["column"] and column.get("primary_key") for column in table["columns"]):
            raise error("E_MIGRATION_OP", "cannot drop a primary-key column", path=path)
        table["columns"] = [column for column in table["columns"] if column["name"] != operation["column"]]
        for fk in table.get("foreign_keys", []):
            if operation["column"] in fk["columns"]:
                raise error(
                    "E_FOREIGN_KEY",
                    "cannot drop a column used by a foreign key",
                    path=path,
                    table=table["name"],
                    column=operation["column"],
                )
        for other_table in result["tables"]:
            for fk in other_table.get("foreign_keys", []):
                if fk["ref_table"] == table["name"] and operation["column"] in fk["ref_columns"]:
                    raise error(
                        "E_FOREIGN_KEY",
                        "cannot drop a column referenced by a foreign key",
                        path=path,
                        table=table["name"],
                        column=operation["column"],
                        referencing_table=other_table["name"],
                    )
    elif op == "create_index":
        if any(item["name"] == operation["name"] for item in result["indexes"]):
            raise error("E_OBJECT_EXISTS", "index already exists", path=path, index=operation["name"])
        _find_table(result, operation["table"], path)
        result["indexes"].append(
            {
                "name": operation["name"],
                "table": operation["table"],
                "columns": list(operation["columns"]),
                "unique": bool(operation.get("unique", False)),
            }
        )
    elif op == "drop_index":
        if not any(item["name"] == operation["name"] for item in result["indexes"]):
            raise error("E_OBJECT_NOT_FOUND", "index does not exist", path=path, index=operation["name"])
        result["indexes"] = [item for item in result["indexes"] if item["name"] != operation["name"]]
    elif op == "replace_view":
        view = {
            "name": operation["name"],
            "sql": operation["sql"],
            "references": list(operation.get("references", [])),
            "output_columns": operation.get("output_columns"),
        }
        found = False
        for index, item in enumerate(result["views"]):
            if item["name"] == operation["name"]:
                result["views"][index] = view
                found = True
                break
        if not found:
            result["views"].append(view)
    elif op == "replace_trigger":
        trigger = {
            "name": operation["name"],
            "table": operation["table"],
            "sql": operation["sql"],
            "references": list(operation.get("references", [])),
        }
        found = False
        for index, item in enumerate(result["triggers"]):
            if item["name"] == operation["name"]:
                result["triggers"][index] = trigger
                found = True
                break
        if not found:
            result["triggers"].append(trigger)
    elif op == "rename_table":
        table = _find_table(result, operation["old_name"], path)
        if any(item["name"] == operation["new_name"] for item in result["tables"]):
            raise error("E_OBJECT_EXISTS", "table name already exists", path=path, table=operation["new_name"])
        table["name"] = operation["new_name"]
        for index in result["indexes"]:
            if index["table"] == operation["old_name"]:
                index["table"] = operation["new_name"]
        for fk_table in result["tables"]:
            for fk in fk_table.get("foreign_keys", []):
                if fk["ref_table"] == operation["old_name"]:
                    fk["ref_table"] = operation["new_name"]
        for trigger in result["triggers"]:
            if trigger["table"] == operation["old_name"]:
                trigger["table"] = operation["new_name"]
    elif op == "rename_column":
        table = _find_table(result, operation["table"], path)
        column = _find_column(table, operation["old_name"], path)
        if any(item["name"] == operation["new_name"] for item in table["columns"]):
            raise error("E_OBJECT_EXISTS", "column name already exists", path=path, column=operation["new_name"])
        column["name"] = operation["new_name"]
        for index in result["indexes"]:
            if index["table"] == table["name"]:
                index["columns"] = [
                    operation["new_name"] if value == operation["old_name"] else value
                    for value in index["columns"]
                ]
        for fk in table.get("foreign_keys", []):
            fk["columns"] = [
                operation["new_name"] if value == operation["old_name"] else value
                for value in fk["columns"]
            ]
        for fk_table in result["tables"]:
            for fk in fk_table.get("foreign_keys", []):
                if fk["ref_table"] == table["name"]:
                    fk["ref_columns"] = [
                        operation["new_name"] if value == operation["old_name"] else value
                        for value in fk["ref_columns"]
                    ]
    canonicalize_schema_in_place(result)
    return result


def normalize_migration(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_MIGRATION", "migration must be an object", path=path)
    migration_id = validate_name(raw.get("id", raw.get("migration_id")), "migration", f"{path}.id")
    from_version = parse_version(raw.get("from_version", raw.get("base_version")), f"{path}.from_version")
    to_version = parse_version(raw.get("to_version", raw.get("version")), f"{path}.to_version")
    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise error("E_MIGRATION", "operations must be a non-empty list", path=f"{path}.operations")
    operations = [
        normalize_operation(operation, f"{path}.operations[{index}]")
        for index, operation in enumerate(operations_raw)
    ]
    mode = raw.get("mode", raw.get("change_kind"))
    if mode is not None:
        mode = str(mode).upper()
        if mode not in {"INCREMENTAL_APPEND", "FULL_REPLACE"}:
            raise error("E_MIGRATION", "unsupported migration mode", path=f"{path}.mode", value=mode)
    return {
        "id": migration_id,
        "from_version": from_version,
        "to_version": to_version,
        "description": raw.get("description", ""),
        "mode": mode,
        "operations": operations,
    }


def normalize_template(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("E_TEMPLATE", "query template must be an object", path=path)
    template_id = validate_name(raw.get("id", raw.get("template_id")), "template", f"{path}.id")
    sql = raw.get("sql", raw.get("template", raw.get("query")))
    if not isinstance(sql, str) or not sql.strip():
        raise error("E_TEMPLATE", "template SQL must be a non-empty string", path=f"{path}.sql")
    references = _normalize_references(raw.get("references", raw.get("dependencies")), f"{path}.references")
    for index, item in enumerate(raw.get("indexes", []) or []):
        if not isinstance(item, str) or not item:
            raise error("E_TEMPLATE", "index dependency must be a non-empty string", path=f"{path}.indexes[{index}]")
        references.append(f"index:{item}")
    tolerance = raw.get("tolerance")
    if tolerance is not None and (
        isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0
    ):
        raise error("E_TEMPLATE", "tolerance must be a non-negative number", path=f"{path}.tolerance")
    return {
        "id": template_id,
        "sql": sql.strip(),
        "description": raw.get("description", ""),
        "references": sorted(set(references)),
        "tolerance": tolerance,
    }


@dataclass
class Bundle:
    schemas: Dict[int, Dict[str, Any]]
    migrations: Dict[str, Dict[str, Any]]
    templates: Dict[str, Dict[str, Any]]
    default_tolerance: float = 0.000001
    source_digests: Optional[Dict[str, str]] = None

    @property
    def first_version(self) -> int:
        return min(self.schemas)

    @property
    def last_version(self) -> int:
        return max(self.schemas)

    def migration_for_target(self, version: int) -> Optional[Dict[str, Any]]:
        for migration in self.migrations.values():
            if migration["to_version"] == version:
                return migration
        return None


def _read_json(path: Path, kind: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise error("E_INPUT_NOT_FOUND", f"{kind} file not found", path=str(path))
    except json.JSONDecodeError as exc:
        raise error(
            "E_JSON",
            f"{kind} file is not valid JSON",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
            detail=exc.msg,
        )
    except OSError as exc:
        raise error("E_INPUT_IO", f"cannot read {kind} file", path=str(path), detail=str(exc))


def load_bundle(
    schemas_path: Path,
    migrations_path: Path,
    queries_path: Path,
    default_tolerance: float = 0.000001,
) -> Bundle:
    schemas_raw = _read_json(schemas_path, "schemas")
    migrations_raw = _read_json(migrations_path, "migrations")
    queries_raw = _read_json(queries_path, "queries")

    if isinstance(schemas_raw, dict) and "versions" in schemas_raw:
        version_items = schemas_raw["versions"]
        if isinstance(version_items, dict):
            version_items = list(version_items.values())
    elif isinstance(schemas_raw, list):
        version_items = schemas_raw
    else:
        raise error("E_SCHEMA", "schemas JSON must contain a versions list", path=str(schemas_path))
    if not isinstance(version_items, list) or not version_items:
        raise error("E_SCHEMA", "versions must be a non-empty list", path=str(schemas_path))
    schemas: Dict[int, Dict[str, Any]] = {}
    for index, raw in enumerate(version_items):
        schema = normalize_schema(raw, f"schemas[{index}]")
        if schema["version"] in schemas:
            raise error("E_DUPLICATE_NAME", "duplicate schema version", version=schema["version"])
        schemas[schema["version"]] = schema

    if isinstance(migrations_raw, dict) and "migrations" in migrations_raw:
        migration_items = migrations_raw["migrations"]
    elif isinstance(migrations_raw, list):
        migration_items = migrations_raw
    else:
        raise error("E_MIGRATION", "migrations JSON must contain a migrations list", path=str(migrations_path))
    if not isinstance(migration_items, list):
        raise error("E_MIGRATION", "migrations must be a list", path=str(migrations_path))
    migrations: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(migration_items):
        migration = normalize_migration(raw, f"migrations[{index}]")
        if migration["id"] in migrations:
            raise error("E_DUPLICATE_NAME", "duplicate migration id", migration=migration["id"])
        migrations[migration["id"]] = migration

    if isinstance(queries_raw, dict) and "templates" in queries_raw:
        template_items = queries_raw["templates"]
        if "tolerance" in queries_raw:
            raw_tolerance = queries_raw["tolerance"]
            if (
                isinstance(raw_tolerance, bool)
                or not isinstance(raw_tolerance, (int, float))
                or raw_tolerance < 0
            ):
                raise error("E_TEMPLATE", "tolerance must be a non-negative number", path=f"{queries_path}.tolerance")
            default_tolerance = float(raw_tolerance)
    elif isinstance(queries_raw, list):
        template_items = queries_raw
    else:
        raise error("E_TEMPLATE", "queries JSON must contain a templates list", path=str(queries_path))
    if not isinstance(template_items, list) or not template_items:
        raise error("E_TEMPLATE", "templates must be a non-empty list", path=str(queries_path))
    templates: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(template_items):
        template = normalize_template(raw, f"templates[{index}]")
        if template["id"] in templates:
            raise error("E_DUPLICATE_NAME", "duplicate template id", template=template["id"])
        templates[template["id"]] = template

    bundle = Bundle(
        schemas=schemas,
        migrations=migrations,
        templates=templates,
        default_tolerance=default_tolerance,
        source_digests={
            "schemas": digest_json(schemas),
            "migrations": digest_json(migrations),
            "templates": digest_json(templates),
        },
    )
    return bundle


def bundle_digest(bundle: Bundle) -> str:
    return digest_json(
        {
            "schemas": bundle.schemas,
            "migrations": bundle.migrations,
            "templates": bundle.templates,
            "default_tolerance": bundle.default_tolerance,
        }
    )


def schema_from_json(text: str) -> Dict[str, Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise error("E_JSON", "stored schema JSON is invalid", detail=str(exc))
    return raw


def view_sql_body(view: Dict[str, Any]) -> str:
    return strip_create_view_prefix(view["sql"], view["name"])


def normalized_sql(sql: str) -> str:
    return normalize_sql_text(sql)


def canonical_schema_text(schema: Dict[str, Any]) -> str:
    return canonical_json(schema_for_digest(schema, include_seed=False))


def logical_equal(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return canonical_schema_text(left) == canonical_schema_text(right)

