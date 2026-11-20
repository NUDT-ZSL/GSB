from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .dependency import compute_invalidation, operation_summaries
from .errors import error
from .model import schema_digest, view_sql_body
from .snapshot import connect_snapshot
from .sqlparse import rewrite_cast_types, rewrite_identifiers
from .typesys import compare_types, parse_type
from .util import canonical_json, canonical_value, decimal_text, digest_json


def _column_type(schema: Dict[str, Any], table_name: str, column_name: str) -> Optional[str]:
    for table in schema.get("tables", []):
        if table["name"] == table_name:
            for column in table["columns"]:
                if column["name"] == column_name:
                    return column["type"]
    return None


def _type_changes(
    migration: Dict[str, Any],
    base_schema: Dict[str, Any],
) -> List[Dict[str, Any]]:
    changes = []
    for summary, operation in zip(operation_summaries(migration), migration["operations"]):
        if operation["op"] == "alter_column_type":
            old_type = _column_type(base_schema, operation["table"], operation["column"])
            if old_type is None:
                continue
            comparison = compare_types(parse_type(old_type), parse_type(operation["new_type"]))
            changes.append(
                {
                    "node": f"column:{operation['table']}.{operation['column']}",
                    "table": operation["table"],
                    "column": operation["column"],
                    "op_id": summary["op_id"],
                    "operation": operation["op"],
                    "original_type": old_type,
                    "new_type": operation["new_type"],
                    "comparison": comparison,
                    "conversion": operation.get("conversion", {}),
                }
            )
        elif operation["op"] == "drop_column":
            old_type = _column_type(base_schema, operation["table"], operation["column"])
            changes.append(
                {
                    "node": f"column:{operation['table']}.{operation['column']}",
                    "table": operation["table"],
                    "column": operation["column"],
                    "op_id": summary["op_id"],
                    "operation": operation["op"],
                    "original_type": old_type,
                    "new_type": None,
                    "comparison": {"status": "INCOMPATIBLE", "reason": "column_removed"},
                    "conversion": {},
                }
            )
    return changes


def _type_change_priority(status: str) -> int:
    return {"SAFE": 0, "PRECISION_LOSS": 1, "INCOMPATIBLE": 2}.get(status, 3)


def _template_dependency_columns(
    template: Dict[str, Any],
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
) -> Set[str]:
    from .sqlparse import parse_query_dependencies

    result: Set[str] = set()
    for schema in (base_schema, target_schema):
        try:
            parsed = parse_query_dependencies(template["sql"], schema, f"template:{template['id']}")
            result.update(parsed["columns"])
        except Exception:
            continue
    for reference in template.get("references", []):
        if reference.startswith("column:"):
            result.add(reference.split(":", 1)[1])
    return result


def _rewrite_sql(
    template: Dict[str, Any],
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
    migration: Dict[str, Any],
) -> str:
    table_renames: Dict[str, str] = {}
    column_renames: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for operation in migration["operations"]:
        if operation["op"] == "rename_table":
            table_renames[operation["old_name"]] = operation["new_name"]
        elif operation["op"] == "rename_column":
            column_renames[(operation["table"], operation["old_name"])] = (
                operation["table"],
                operation["new_name"],
            )
    type_changes: Dict[Tuple[str, str], str] = {}
    for operation in migration["operations"]:
        if operation["op"] == "alter_column_type":
            type_changes[(operation["table"], operation["column"])] = operation["new_type"]
    schema = base_schema
    try:
        from .sqlparse import parse_query_dependencies

        parse_query_dependencies(template["sql"], base_schema, "template")
    except Exception:
        schema = target_schema
    rewritten = template["sql"].strip()
    if table_renames or column_renames:
        rewritten = rewrite_identifiers(rewritten, schema, table_renames, column_renames)
    return rewrite_cast_types(rewritten, target_schema, type_changes)


def _run_sql(
    conn: sqlite3.Connection,
    sql: str,
    template_id: str,
    phase: str,
    max_steps: int = 2_000_000,
) -> Dict[str, Any]:
    counter = {"steps": 0}

    def progress() -> int:
        counter["steps"] += 1
        return 1 if counter["steps"] > max_steps else 0

    conn.set_progress_handler(progress, 10_000)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        columns = [description[0] for description in (cursor.description or [])]
        normalized = []
        for row in rows:
            normalized_row = []
            for value in row:
                if isinstance(value, bool):
                    normalized_row.append(value)
                elif isinstance(value, int):
                    normalized_row.append({"$num": str(value)})
                elif isinstance(value, float):
                    try:
                        normalized_row.append({"$num": _stable_decimal_text(Decimal(str(value)))})
                    except (InvalidOperation, ValueError):
                        normalized_row.append({"$float": repr(value)})
                else:
                    normalized_row.append(canonical_value(value))
            normalized.append(normalized_row)
        return {
            "ok": True,
            "columns": columns,
            "row_count": len(rows),
            "digest": digest_json({"columns": columns, "rows": normalized}),
            "rows": normalized,
            "raw_rows": rows,
        }
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "error": {
                "code": "E_QUERY_EXECUTION",
                "message": str(exc),
                "phase": phase,
                "template": template_id,
            },
        }
    finally:
        conn.set_progress_handler(None, 0)


def _numeric_delta(left: Any, right: Any) -> Tuple[bool, Decimal]:
    if isinstance(left, bool) or isinstance(right, bool):
        return False, Decimal(0)
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False, Decimal(0)
    try:
        return True, abs(Decimal(str(left)) - Decimal(str(right)))
    except InvalidOperation:
        return False, Decimal(0)


def _stable_decimal_text(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    return decimal_text(quantized)


def _compare_rows(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    if not left.get("ok") or not right.get("ok"):
        return {
            "equal": False,
            "comparable": False,
            "max_abs_delta": None,
            "numeric_differences": 0,
        }
    if left["columns"] != right["columns"] or left["row_count"] != right["row_count"]:
        return {
            "equal": False,
            "comparable": False,
            "max_abs_delta": None,
            "numeric_differences": 0,
        }
    left_rows = left["raw_rows"]
    right_rows = right["raw_rows"]
    max_delta = Decimal(0)
    numeric_differences = 0
    non_numeric_difference = False
    for left_row, right_row in zip(left_rows, right_rows):
        if len(left_row) != len(right_row):
            non_numeric_difference = True
            continue
        for left_value, right_value in zip(left_row, right_row):
            if canonical_value(left_value) == canonical_value(right_value):
                continue
            numeric, delta = _numeric_delta(left_value, right_value)
            if numeric:
                numeric_differences += 1
                max_delta = max(max_delta, delta)
            else:
                non_numeric_difference = True
    equal = left["digest"] == right["digest"]
    return {
        "equal": equal,
        "comparable": True,
        "max_abs_delta": None if equal else _stable_decimal_text(max_delta),
        "numeric_differences": numeric_differences,
        "non_numeric_difference": non_numeric_difference,
    }


def _result_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("ok"):
        return {"ok": False, "error": result["error"]}
    return {
        "ok": True,
        "columns": result["columns"],
        "row_count": result["row_count"],
        "digest": result["digest"],
    }


def _conversion_source_chain(
    template_id: str,
    changes: List[Dict[str, Any]],
    conversion_log: Sequence[Dict[str, Any]],
    max_delta: Optional[str],
) -> List[Dict[str, Any]]:
    chain: List[Dict[str, Any]] = []
    changed_columns = {f"{item['table']}.{item['column']}" for item in changes}
    for item in changes:
        chain.append(
            {
                "step": "type_conversion",
                "operation_id": item["op_id"],
                "column": f"{item['table']}.{item['column']}",
                "original_type": item["original_type"],
                "new_type": item["new_type"],
                "source_scale": item.get("conversion", {}).get("source_scale"),
                "target_scale": item.get("conversion", {}).get("target_scale"),
                "rounding": item.get("conversion", {}).get("rounding"),
                "comparison": item["comparison"],
            }
        )
    relevant_log = [
        entry
        for entry in conversion_log
        if f"{entry.get('table')}.{entry.get('column')}" in changed_columns
    ]
    if relevant_log:
        chain.append(
            {
                "step": "value_conversion",
                "entries": len(relevant_log),
                "first": relevant_log[0],
                "last": relevant_log[-1],
            }
        )
    chain.append({"step": "template", "template_id": template_id})
    chain.append({"step": "query_result", "max_abs_delta": max_delta})
    return chain


def analyze_templates(
    migration: Dict[str, Any],
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
    base_snapshot: Path,
    target_snapshot: Path,
    templates: Dict[str, Dict[str, Any]],
    tolerance: float,
    conversion_log: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    base_version = int(migration["from_version"])
    target_version = int(migration["to_version"])
    invalidation = compute_invalidation(
        base_schema, target_schema, migration, templates, target_version
    )
    invalidation_by_template = {
        item["name"]: item for item in invalidation["templates"]
    }
    changes = _type_changes(migration, base_schema)
    change_by_node = {item["node"]: item for item in changes}
    base_conn = connect_snapshot(base_snapshot, read_only=True)
    target_conn = connect_snapshot(target_snapshot, read_only=True)
    report_templates: List[Dict[str, Any]] = []
    precision_pairs: List[Dict[str, Any]] = []
    try:
        for template_id in sorted(templates):
            template = templates[template_id]
            available_from = int(template.get("_available_from_version", 0))
            if available_from > target_version:
                continue
            invalidation_item = invalidation_by_template.get(template_id)
            direct_columns = _template_dependency_columns(template, base_schema, target_schema)
            reachable_roots: Set[str] = set()
            paths: List[Dict[str, Any]] = []
            if invalidation_item:
                for reason in invalidation_item["reasons"]:
                    reachable_roots.add(reason["root"])
                    paths.append(
                        {
                            "operation_id": reason["operation_id"],
                            "operation": reason["operation"],
                            "root": reason["root"],
                            "path": reason["path"],
                        }
                    )
            relevant_changes = [
                item
                for item in changes
                if item["node"] in reachable_roots
                or f"{item['table']}.{item['column']}" in direct_columns
            ]
            relevant_changes.sort(key=lambda item: item["op_id"])
            has_drop = any(item["comparison"]["status"] == "INCOMPATIBLE" for item in relevant_changes)
            worst_status = "SAFE"
            for item in relevant_changes:
                if _type_change_priority(item["comparison"]["status"]) > _type_change_priority(
                    worst_status
                ):
                    worst_status = item["comparison"]["status"]
            if not invalidation_item:
                status = "REUSABLE"
            elif has_drop or worst_status == "INCOMPATIBLE":
                status = "INCOMPATIBLE"
            elif worst_status == "PRECISION_LOSS":
                status = "PRECISION_LOSS"
            else:
                status = "CACHE_INVALIDATED"

            rewritten = _rewrite_sql(template, base_schema, target_schema, migration)
            old_result = _run_sql(base_conn, template["sql"], template_id, "baseline")
            new_result = _run_sql(target_conn, rewritten, template_id, "target")
            comparison = _compare_rows(old_result, new_result)
            tolerance_value = template.get("tolerance")
            effective_tolerance = float(tolerance if tolerance_value is None else tolerance_value)
            max_delta_text = comparison["max_abs_delta"]
            precision_sensitive = False
            if status == "PRECISION_LOSS" and max_delta_text is not None:
                delta = Decimal(max_delta_text)
                precision_sensitive = Decimal(0) < delta <= Decimal(str(effective_tolerance))
            baseline_available = available_from <= base_version and old_result.get("ok", False)
            semantically_equivalent = (
                status != "INCOMPATIBLE"
                and new_result.get("ok", False)
                and (comparison["equal"] if baseline_available else True)
            )
            if status == "REUSABLE":
                if not old_result.get("ok"):
                    raise error(
                        "E_QUERY_EXECUTION",
                        "baseline query failed for an unaffected template",
                        template=template_id,
                        error=old_result.get("error"),
                    )
                if not new_result.get("ok"):
                    raise error(
                        "E_QUERY_EXECUTION",
                        "target query failed for an unaffected template",
                        template=template_id,
                        error=new_result.get("error"),
                    )
                if not comparison["equal"]:
                    raise error(
                        "E_RESULT_DRIFT",
                        "an unaffected template changed rows across the migration",
                        template=template_id,
                        baseline_digest=old_result.get("digest"),
                        target_digest=new_result.get("digest"),
                    )
            elif status != "INCOMPATIBLE" and not new_result.get("ok"):
                raise error(
                    "E_QUERY_EXECUTION",
                    "a compatible rewritten template failed on the target schema",
                    template=template_id,
                    error=new_result.get("error"),
                )

            first_change = relevant_changes[0] if relevant_changes else None
            first_affected = None
            if first_change:
                first_affected = {
                    "column": f"{first_change['table']}.{first_change['column']}",
                    "original_type": first_change["original_type"],
                    "new_type": first_change["new_type"],
                    "change_status": first_change["comparison"]["status"],
                    "reason": first_change["comparison"]["reason"],
                }
            chain = (
                _conversion_source_chain(
                    template_id,
                    relevant_changes,
                    conversion_log,
                    max_delta_text,
                )
                if precision_sensitive
                else []
            )
            item = {
                "id": template_id,
                "status": status,
                "invalidated": invalidation_item is not None,
                "dependency_paths": sorted(
                    paths,
                    key=lambda item: (
                        item["operation_id"],
                        item["root"],
                        len(item["path"]),
                        item["path"],
                    ),
                ),
                "rewrite": {
                    "status": status,
                    "sql": rewritten,
                    "changed": rewritten != template["sql"].strip(),
                    "first_affected_column": first_affected,
                },
                "result_check": {
                    "baseline_available": baseline_available,
                    "baseline": _result_summary(old_result),
                    "target": _result_summary(new_result),
                    "equal": comparison["equal"],
                    "comparable": comparison["comparable"],
                    "max_abs_delta": max_delta_text,
                    "numeric_differences": comparison["numeric_differences"],
                    "semantically_equivalent": semantically_equivalent,
                },
                "tolerance": effective_tolerance,
                "precision_sensitive": precision_sensitive,
                "error_source_chain": chain,
            }
            report_templates.append(item)
    finally:
        base_conn.close()
        target_conn.close()

    sensitive = [item for item in report_templates if item["precision_sensitive"]]
    for left_index, left in enumerate(sensitive):
        for right in sensitive[left_index + 1 :]:
            left_delta = left["result_check"]["max_abs_delta"]
            right_delta = right["result_check"]["max_abs_delta"]
            if left_delta is None or right_delta is None:
                continue
            delta_diff = abs(Decimal(left_delta) - Decimal(right_delta))
            pair_tolerance = min(left["tolerance"], right["tolerance"])
            if delta_diff <= Decimal(str(pair_tolerance)):
                precision_pairs.append(
                    {
                        "templates": [left["id"], right["id"]],
                        "delta_difference": _stable_decimal_text(delta_diff),
                        "tolerance": pair_tolerance,
                    }
                )
    precision_pairs.sort(key=lambda item: item["templates"])

    return {
        "invalidations": invalidation,
        "templates": report_templates,
        "precision_sensitivity_pairs": precision_pairs,
    }

