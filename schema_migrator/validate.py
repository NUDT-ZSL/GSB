from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .errors import error
from .model import (
    Bundle,
    apply_operation,
    logical_equal,
    operation_target,
    schema_for_digest,
    view_sql_body,
)
from .sqlparse import KEYWORDS, parse_query_dependencies, tokenize, validate_single_select
from .typesys import default_compatible, parse_type


def _schema_indexes(schema: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    tables = {table["name"]: table for table in schema.get("tables", [])}
    columns = {
        table["name"]: [column["name"] for column in table.get("columns", [])]
        for table in schema.get("tables", [])
    }
    return tables, columns


def _validate_reference_exists(schema: Dict[str, Any], reference: str, path: str) -> None:
    tables, columns = _schema_indexes(schema)
    if reference.startswith("column:"):
        name = reference.split(":", 1)[1]
        table, column = name.split(".", 1)
        if table not in tables or column not in columns.get(table, []):
            raise error(
                "E_DEPENDENCY_MISSING",
                "dependency references a missing column",
                path=path,
                reference=reference,
            )
        return
    kind, name = reference.split(":", 1)
    if kind == "table" and name not in tables:
        raise error("E_DEPENDENCY_MISSING", "dependency references a missing table", path=path, reference=reference)
    if kind == "index" and not any(item["name"] == name for item in schema.get("indexes", [])):
        raise error("E_DEPENDENCY_MISSING", "dependency references a missing index", path=path, reference=reference)
    if kind == "view" and not any(item["name"] == name for item in schema.get("views", [])):
        raise error("E_DEPENDENCY_MISSING", "dependency references a missing view", path=path, reference=reference)
    if kind == "trigger" and not any(item["name"] == name for item in schema.get("triggers", [])):
        raise error("E_DEPENDENCY_MISSING", "dependency references a missing trigger", path=path, reference=reference)


def _foreign_types_comparable(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_type = parse_type(left["type"])
    right_type = parse_type(right["type"])
    if left_type.canonical == right_type.canonical:
        return True
    numeric = {"integer", "real", "numeric", "decimal"}
    if left_type.family in numeric and right_type.family in numeric:
        return True
    text_family = {"text"}
    if left_type.family in text_family and right_type.family in text_family:
        return True
    return False


def _validate_schema_relations(schema: Dict[str, Any], path: str = "schema") -> None:
    tables, columns = _schema_indexes(schema)
    indexes_by_name: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(schema.get("indexes", [])):
        item_path = f"{path}.indexes[{index}]"
        if item["name"] in indexes_by_name:
            raise error("E_DUPLICATE_NAME", "duplicate index name", path=item_path, name=item["name"])
        indexes_by_name[item["name"]] = item
        if item["table"] not in tables:
            raise error(
                "E_INDEX",
                "index references an unknown table",
                path=item_path,
                index=item["name"],
                table=item["table"],
            )
        seen: Set[str] = set()
        for column in item["columns"]:
            if column in seen:
                raise error("E_INDEX", "index repeats a column", path=item_path, column=column)
            seen.add(column)
            if column not in columns.get(item["table"], []):
                raise error(
                    "E_INDEX",
                    "index references an unknown column",
                    path=item_path,
                    index=item["name"],
                    table=item["table"],
                    column=column,
                )

    view_names = {item["name"] for item in schema.get("views", [])}
    trigger_names = {item["name"] for item in schema.get("triggers", [])}
    if len(view_names) != len(schema.get("views", [])):
        raise error("E_DUPLICATE_NAME", "duplicate view name", path=f"{path}.views")
    if len(trigger_names) != len(schema.get("triggers", [])):
        raise error("E_DUPLICATE_NAME", "duplicate trigger name", path=f"{path}.triggers")
    relation_names = set(tables) | view_names
    if len(relation_names) != len(tables) + len(view_names):
        raise error("E_DUPLICATE_NAME", "table and view names share one relation namespace", path=path)
    if relation_names & set(indexes_by_name):
        raise error(
            "E_DUPLICATE_NAME",
            "index name conflicts with a table or view name",
            path=path,
            names=sorted(relation_names & set(indexes_by_name)),
        )

    for index, view in enumerate(schema.get("views", [])):
        view_path = f"{path}.views[{index}]"
        for reference in view.get("references", []):
            _validate_reference_exists(schema, reference, f"{view_path}.references")
        body = view_sql_body(view)
        try:
            derived = parse_query_dependencies(body, schema, f"{view_path}.sql")
        except Exception:
            raise
        for reference in derived["references"]:
            if reference == f"view:{view['name']}":
                raise error("E_DEPENDENCY_CYCLE", "view cannot reference itself", path=view_path, view=view["name"])

    for index, trigger in enumerate(schema.get("triggers", [])):
        trigger_path = f"{path}.triggers[{index}]"
        if trigger["table"] not in tables:
            raise error(
                "E_TRIGGER",
                "trigger references an unknown table",
                path=trigger_path,
                trigger=trigger["name"],
                table=trigger["table"],
            )
        for reference in trigger.get("references", []):
            _validate_reference_exists(schema, reference, f"{trigger_path}.references")
        tokens = tokenize(trigger["sql"])
        if not tokens or tokens[0].upper != "CREATE":
            raise error("E_TRIGGER", "trigger SQL must start with CREATE TRIGGER", path=f"{trigger_path}.sql")
        if not any(token.upper == "TRIGGER" for token in tokens[:6]):
            raise error("E_TRIGGER", "trigger SQL must contain TRIGGER", path=f"{trigger_path}.sql")

    for table in schema.get("tables", []):
        table_columns = {column["name"]: column for column in table["columns"]}
        for fk_index, fk in enumerate(table.get("foreign_keys", [])):
            fk_path = f"{path}.tables[{table['name']}].foreign_keys[{fk_index}]"
            target_table = tables.get(fk["ref_table"])
            if target_table is None:
                raise error(
                    "E_FOREIGN_KEY",
                    "foreign key references an unknown table",
                    path=fk_path,
                    table=table["name"],
                    ref_table=fk["ref_table"],
                )
            target_columns = {column["name"]: column for column in target_table["columns"]}
            for source_name, target_name in zip(fk["columns"], fk["ref_columns"]):
                if source_name not in table_columns:
                    raise error(
                        "E_FOREIGN_KEY",
                        "foreign key source column does not exist",
                        path=fk_path,
                        table=table["name"],
                        column=source_name,
                    )
                if target_name not in target_columns:
                    raise error(
                        "E_FOREIGN_KEY",
                        "foreign key target column does not exist",
                        path=fk_path,
                        table=fk["ref_table"],
                        column=target_name,
                    )
                if not _foreign_types_comparable(table_columns[source_name], target_columns[target_name]):
                    raise error(
                        "E_FOREIGN_KEY_TYPE",
                        "foreign key columns have incomparable types",
                        path=fk_path,
                        source_type=table_columns[source_name]["type"],
                        target_type=target_columns[target_name]["type"],
                    )


def validate_schema(schema: Dict[str, Any], path: str = "schema") -> None:
    _validate_schema_relations(schema, path)
    version = schema["version"]
    if version < 1:
        raise error("E_VERSION", "schema versions must start at 1 or above", path=path, version=version)
    for table in schema.get("tables", []):
        column_map = {column["name"]: column for column in table["columns"]}
        for row_index, row in enumerate(table.get("rows", [])):
            row_path = f"{path}.tables[{table['name']}].rows[{row_index}]"
            if not isinstance(row, dict):
                raise error("E_SEED", "seed row must be an object", path=row_path)
            unknown = sorted(set(row) - set(column_map))
            if unknown:
                raise error(
                    "E_SEED",
                    "seed row contains unknown columns",
                    path=row_path,
                    columns=unknown,
                )
            for column in table["columns"]:
                if column["name"] in row:
                    value = row[column["name"]]
                elif column.get("has_default"):
                    value = column.get("default")
                elif column.get("not_null"):
                    raise error(
                        "E_SEED",
                        "seed row is missing a required column",
                        path=row_path,
                        column=column["name"],
                    )
                else:
                    value = None
                ok, reason = default_compatible(parse_type(column["type"]), value)
                if not ok:
                    raise error(
                        "E_SEED",
                        "seed value is incompatible with its column type",
                        path=f"{row_path}.{column['name']}",
                        value=value,
                        type=column["type"],
                        reason=reason,
                    )
        for column in table["columns"]:
            parse_type(column["type"], f"{path}.{table['name']}.{column['name']}.type")


def _validate_trigger_sql_references(schema: Dict[str, Any], trigger: Dict[str, Any], path: str) -> None:
    # Trigger bodies are intentionally dependency-declared in the sample. The
    # engine still verifies the target table and every declared dependency.
    _validate_reference_exists(schema, f"table:{trigger['table']}", path)
    for reference in trigger.get("references", []):
        _validate_reference_exists(schema, reference, path)


def _validate_after_operation(schema: Dict[str, Any], path: str) -> None:
    # Enforce dependency order: a migration cannot drop an index/column/view
    # that a still-live object declares as a dependency. Replace the dependent
    # object first, then remove the old dependency.
    _validate_schema_relations(schema, path)
    for index, trigger in enumerate(schema.get("triggers", [])):
        _validate_trigger_sql_references(schema, trigger, f"{path}.triggers[{index}]")


def _diff_schema_summary(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    left_tables = {table["name"]: table for table in left.get("tables", [])}
    right_tables = {table["name"]: table for table in right.get("tables", [])}
    table_diff: Dict[str, Any] = {}
    for name in sorted(set(left_tables) | set(right_tables)):
        if name not in left_tables:
            table_diff[name] = {"status": "missing_in_source"}
        elif name not in right_tables:
            table_diff[name] = {"status": "extra_in_source"}
        else:
            left_columns = {column["name"]: column for column in left_tables[name]["columns"]}
            right_columns = {column["name"]: column for column in right_tables[name]["columns"]}
            changed = {}
            for column_name in sorted(set(left_columns) | set(right_columns)):
                if left_columns.get(column_name) != right_columns.get(column_name):
                    changed[column_name] = {
                        "source": left_columns.get(column_name),
                        "target": right_columns.get(column_name),
                    }
            if changed:
                table_diff[name] = {"columns": changed}
    index_diff = {
        "source": sorted(
            [item for item in left.get("indexes", []) if item not in right.get("indexes", [])],
            key=lambda item: item["name"],
        ),
        "target": sorted(
            [item for item in right.get("indexes", []) if item not in left.get("indexes", [])],
            key=lambda item: item["name"],
        ),
    }
    view_diff = {
        "source": sorted(
            [item for item in left.get("views", []) if item not in right.get("views", [])],
            key=lambda item: item["name"],
        ),
        "target": sorted(
            [item for item in right.get("views", []) if item not in left.get("views", [])],
            key=lambda item: item["name"],
        ),
    }
    trigger_diff = {
        "source": sorted(
            [item for item in left.get("triggers", []) if item not in right.get("triggers", [])],
            key=lambda item: item["name"],
        ),
        "target": sorted(
            [item for item in right.get("triggers", []) if item not in left.get("triggers", [])],
            key=lambda item: item["name"],
        ),
    }
    return {
        "tables": table_diff,
        "indexes": index_diff,
        "views": view_diff,
        "triggers": trigger_diff,
    }


def validate_migration_plan(
    migration: Dict[str, Any],
    schemas: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    path = f"migration:{migration['id']}"
    if migration["from_version"] not in schemas:
        raise error(
            "E_MIGRATION_ORDER",
            "migration source version does not exist",
            migration=migration["id"],
            version=migration["from_version"],
        )
    if migration["to_version"] not in schemas:
        raise error(
            "E_MIGRATION_ORDER",
            "migration target version does not exist",
            migration=migration["id"],
            version=migration["to_version"],
        )
    if migration["to_version"] <= migration["from_version"]:
        raise error(
            "E_MIGRATION_ORDER",
            "migration target version must be greater than its source version",
            migration=migration["id"],
        )
    current = copy.deepcopy(schema_for_digest(schemas[migration["from_version"]]))
    targets: Dict[str, str] = {}
    for index, operation in enumerate(migration["operations"]):
        operation_path = f"{path}.operations[{index}]"
        target = operation_target(operation)
        if target in targets:
            raise error(
                "E_MIGRATION_ORDER",
                "two operations target the same object",
                path=operation_path,
                target=target,
                previous=targets[target],
            )
        targets[target] = f"{migration['id']}.{index + 1:03d}"
        try:
            current = apply_operation(current, operation, operation_path)
            _validate_after_operation(current, operation_path)
        except Exception:
            raise
    current["version"] = migration["to_version"]
    target_schema = schema_for_digest(schemas[migration["to_version"]])
    if not logical_equal(current, target_schema):
        raise error(
            "E_MIGRATION_TARGET_MISMATCH",
            "migration operations do not produce the declared target schema",
            migration=migration["id"],
            from_version=migration["from_version"],
            to_version=migration["to_version"],
            diff=_diff_schema_summary(current, target_schema),
        )
    return {
        "migration_id": migration["id"],
        "from_version": migration["from_version"],
        "to_version": migration["to_version"],
        "target_schema": current,
        "operation_ids": [
            f"{migration['id']}.{index + 1:03d}"
            for index in range(len(migration["operations"]))
        ],
    }


def validate_bundle(bundle: Bundle) -> None:
    versions = sorted(bundle.schemas)
    if versions != list(range(versions[0], versions[-1] + 1)):
        raise error(
            "E_VERSION_GAP",
            "schema versions must be contiguous",
            versions=versions,
        )
    for version in versions:
        validate_schema(bundle.schemas[version], f"schemas[{version}]")

    by_from: Dict[int, List[Dict[str, Any]]] = {}
    by_to: Dict[int, List[Dict[str, Any]]] = {}
    for migration in sorted(bundle.migrations.values(), key=lambda item: (item["to_version"], item["id"])):
        by_from.setdefault(migration["from_version"], []).append(migration)
        by_to.setdefault(migration["to_version"], []).append(migration)
        validate_migration_plan(migration, bundle.schemas)
    for version, migrations in by_from.items():
        if len(migrations) > 1:
            raise error(
                "E_MIGRATION_ORDER",
                "multiple migrations start from the same version",
                version=version,
                migrations=[item["id"] for item in migrations],
            )
    for version, migrations in by_to.items():
        if len(migrations) > 1:
            raise error(
                "E_MIGRATION_ORDER",
                "multiple migrations target the same version",
                version=version,
                migrations=[item["id"] for item in migrations],
            )
    for version in versions[:-1]:
        if version not in by_from:
            raise error("E_MIGRATION_GAP", "version has no outgoing migration", version=version)
    if versions[-1] in by_from:
        raise error(
            "E_MIGRATION_ORDER",
            "latest schema version cannot have an outgoing migration",
            version=versions[-1],
        )

    for template in sorted(bundle.templates.values(), key=lambda item: item["id"]):
        path = f"templates[{template['id']}]"
        validate_single_select(template["sql"], f"{path}.sql")
        compatible_versions: List[int] = []
        derived_by_version: Dict[int, List[str]] = {}
        for version in versions:
            schema = bundle.schemas[version]
            try:
                dependencies = parse_query_dependencies(template["sql"], schema, f"{path}.sql")
                for reference in template.get("references", []):
                    _validate_reference_exists(schema, reference, f"{path}.references")
            except Exception:
                continue
            compatible_versions.append(version)
            derived_by_version[version] = dependencies["references"]
        if not compatible_versions:
            raise error(
                "E_TEMPLATE_REFERENCE",
                "query template is not valid against any declared schema version",
                path=path,
                template=template["id"],
            )
        template["_available_from_version"] = compatible_versions[0]
        template["_compatible_versions"] = compatible_versions
        template["_derived_references"] = derived_by_version[compatible_versions[0]]

