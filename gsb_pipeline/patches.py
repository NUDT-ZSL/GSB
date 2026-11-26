"""Patch validation, candidate-plan compilation, and column-level invalidation."""

from __future__ import annotations

from collections import deque
from typing import Any

from .errors import (
    BusinessError,
    E_DEPENDENCY_SELF,
    E_DOWNSTREAM_COLUMN_MISSING,
    E_LOGIC_CLOCK_INVALID,
    E_PATCH_INVALID,
    E_PATCH_TARGET_EXISTS,
    E_PATCH_TARGET_MISSING,
)
from .models import DefinitionSet, build_view_model, normalize_declared_column_dependencies
from .sqlplan import find_dependency_nodes_for_columns, parse_sql_plan
from .utils import normalize_sql, require_nonnegative_int, valid_identifier


def normalize_patch_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BusinessError(E_PATCH_INVALID, "补丁事件必须是 JSON 对象")
    patch_id = raw.get("patch_id", raw.get("event_id"))
    if not isinstance(patch_id, str) or not patch_id.strip():
        raise BusinessError(E_PATCH_INVALID, "补丁缺少非空 patch_id")
    sequence = require_nonnegative_int(
        raw.get("sequence", raw.get("edit_round")),
        "patch.sequence",
        code=E_PATCH_INVALID,
    )
    op = raw.get("op", raw.get("action"))
    if not isinstance(op, str) or op.lower() not in {"add", "modify", "delete", "upsert"}:
        raise BusinessError(
            E_PATCH_INVALID,
            "补丁 op 必须为 add/modify/delete/upsert",
            edit_round=sequence,
            value=op,
        )
    view = raw.get("view", raw.get("view_name"))
    if not isinstance(view, str) or not view:
        raise BusinessError(
            E_PATCH_INVALID,
            "补丁缺少非空 view",
            edit_round=sequence,
        )
    if not valid_identifier(view):
        raise BusinessError(
            E_PATCH_INVALID,
            f"补丁视图名不合法: {view}",
            view=view,
            edit_round=sequence,
        )
    clock_value = raw.get("logical_clock_ms", raw.get("clock_ms"))
    clock_ms = require_nonnegative_int(
        clock_value,
        "patch.logical_clock_ms",
        code=E_LOGIC_CLOCK_INVALID,
        view=view,
    )
    normalized_op = op.lower()
    normalized: dict[str, Any] = {
        "patch_id": patch_id,
        "sequence": sequence,
        "logical_clock_ms": clock_ms,
        "op": normalized_op,
        "view": view,
    }
    sql = raw.get("sql")
    if sql is not None:
        if not isinstance(sql, str) or not sql.strip():
            raise BusinessError(
                E_PATCH_INVALID,
                "补丁 SQL 必须是非空字符串",
                view=view,
                edit_round=sequence,
            )
        normalized["sql"] = normalize_sql(sql).rstrip(";").strip()
    if normalized["op"] == "add" and "sql" not in normalized:
        raise BusinessError(
            E_PATCH_INVALID,
            "新增视图补丁必须提供 SQL",
            view=view,
            edit_round=sequence,
        )
    for source_field, target_field in (
        ("dependencies", "dependencies"),
        ("upstreams", "dependencies"),
        ("upstream_add", "upstream_add"),
        ("upstream_remove", "upstream_remove"),
    ):
        if source_field in raw:
            values = raw[source_field]
            if not isinstance(values, list):
                raise BusinessError(
                    E_PATCH_INVALID,
                    f"{source_field} 必须是数组",
                    view=view,
                    edit_round=sequence,
                )
            normalized[target_field] = []
            for value in values:
                if not isinstance(value, str) or not value:
                    raise BusinessError(
                        E_PATCH_INVALID,
                        f"{source_field} 中的依赖名必须非空",
                        view=view,
                        edit_round=sequence,
                    )
                normalized[target_field].append(value)
    if raw.get("column_dependencies") is not None:
        normalized["column_dependencies"] = raw["column_dependencies"]
    return normalized


def _plan_contract(
    plan: dict[str, Any],
) -> tuple[list[str], dict[str, set[tuple[str, str]]]]:
    return (
        list(plan.get("output_columns", [])),
        {
            column: {
                (item["source"], item["column"])
                for item in dependencies
            }
            for column, dependencies in plan.get("column_dependencies", {}).items()
        },
    )


def contract_changes(
    old_plan: dict[str, Any],
    new_plan: dict[str, Any],
) -> dict[str, list[str]]:
    old_columns, old_dependencies = _plan_contract(old_plan)
    new_columns, new_dependencies = _plan_contract(new_plan)
    old_set = set(old_columns)
    new_set = set(new_columns)
    changed = sorted(
        column
        for column in old_set & new_set
        if old_dependencies.get(column, set()) != new_dependencies.get(column, set())
    )
    return {
        "removed": sorted(old_set - new_set),
        "added": sorted(new_set - old_set),
        "mapping_changed": changed,
    }


def _dependency_contract(
    plan: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    return {
        column: [
            {"source": item["source"], "column": item["column"]}
            for item in dependencies
        ]
        for column, dependencies in plan.get("column_dependencies", {}).items()
    }


def _node_ids(plan: dict[str, Any]) -> list[str]:
    return sorted(node["node_id"] for node in plan.get("node_index", []))


def _build_invalidation_closure(
    *,
    old_model: DefinitionSet,
    direct_view: str,
    op: str,
    old_plan: dict[str, Any] | None,
    new_plan: dict[str, Any] | None,
    changes: dict[str, list[str]],
    patch_id: str,
    sequence: int,
) -> dict[str, Any]:
    affected: dict[str, dict[str, Any]] = {}
    source_ref = {
        "patch_id": patch_id,
        "sequence": sequence,
        "root_view": direct_view,
    }
    if op in {"add", "modify"}:
        assert new_plan is not None
        affected[direct_view] = {
            "view": direct_view,
            "reason": "direct_add" if op == "add" else "direct_modify",
            "dependency_path": [direct_view],
            "source": source_ref,
            "affected_columns": sorted(
                set(changes["removed"])
                | set(changes["added"])
                | set(changes["mapping_changed"])
            ),
            "dirty_nodes": _node_ids(new_plan),
            "plan_recompute": True,
            "sql_recompute": True,
        }
    else:
        affected[direct_view] = {
            "view": direct_view,
            "reason": "direct_delete",
            "dependency_path": [direct_view],
            "source": source_ref,
            "affected_columns": [
                column
                for column in (old_plan or {}).get("output_columns", [])
            ],
            "dirty_nodes": _node_ids(old_plan or {}),
            "plan_recompute": False,
            "sql_recompute": False,
        }

    initial_columns = set(changes["removed"]) | set(changes["mapping_changed"])
    if op == "delete":
        initial_columns = set((old_plan or {}).get("output_columns", []))
    compile_errors: list[dict[str, Any]] = []

    if initial_columns:
        queue: deque[tuple[str, set[str], list[str]]] = deque(
            [(direct_view, initial_columns, [direct_view])]
        )
        visited_signatures: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
        while queue:
            upstream, changed_columns, path = queue.popleft()
            signature = (upstream, tuple(sorted(changed_columns)), tuple(path))
            if signature in visited_signatures:
                continue
            visited_signatures.add(signature)
            for downstream_name in sorted(old_model.views):
                if downstream_name in path:
                    continue
                downstream = old_model.views[downstream_name]
                matching_outputs: set[str] = set()
                matching_nodes: set[str] = set()
                missing: set[str] = set()
                for output_column, dependencies in downstream.column_dependencies.items():
                    for dependency in dependencies:
                        if dependency["source"] != upstream:
                            continue
                        if dependency["column"] not in changed_columns:
                            continue
                        matching_outputs.add(output_column)
                        matching_nodes.update(
                            find_dependency_nodes_for_columns(
                                downstream.plan,
                                upstream,
                                {dependency["column"]},
                            )
                        )
                        missing.add(f"{upstream}.{dependency['column']}")
                if not matching_outputs:
                    continue
                next_path = path + [downstream_name]
                entry = affected.get(downstream_name)
                if entry is None:
                    entry = {
                        "view": downstream_name,
                        "reason": "upstream_column_changed",
                        "dependency_path": next_path,
                        "source": source_ref,
                        "affected_columns": [],
                        "dirty_nodes": [],
                        "plan_recompute": True,
                        "sql_recompute": False,
                    }
                    affected[downstream_name] = entry
                entry["affected_columns"] = sorted(
                    set(entry["affected_columns"]) | matching_outputs
                )
                entry["dirty_nodes"] = sorted(
                    set(entry["dirty_nodes"]) | matching_nodes
                )
                if any(
                    dependency["column"] in changes["removed"]
                    for dependency in downstream.column_dependencies.get(output_column, [])
                    if dependency["source"] == upstream
                ):
                    for missing_column in sorted(missing):
                        compile_errors.append(
                            {
                                "code": E_DOWNSTREAM_COLUMN_MISSING,
                                "view": downstream_name,
                                "missing_column": missing_column,
                                "node_id": (
                                    sorted(matching_nodes)[0]
                                    if matching_nodes
                                    else None
                                ),
                                "reference_path": next_path,
                                "edit_round": sequence,
                            }
                        )
                queue.append((downstream_name, matching_outputs, next_path))

    compile_errors = sorted(
        compile_errors,
        key=lambda item: (
            item["reference_path"],
            item["missing_column"],
            item["view"],
        ),
    )
    reusable = sorted(
        name
        for name in old_model.views
        if name not in affected and name != direct_view
    )
    return {
        "patch_id": patch_id,
        "sequence": sequence,
        "root_view": direct_view,
        "op": op,
        "changed_columns": changes,
        "affected_views": [affected[name] for name in sorted(affected)],
        "reusable_views": reusable,
        "compile_errors": compile_errors,
    }


def _apply_definition_patch(
    old_model: DefinitionSet,
    event: dict[str, Any],
) -> tuple[dict[str, str], dict[str, list[str]], str]:
    name = event["view"]
    op = event["op"]
    sql_map = {view_name: view.sql for view_name, view in old_model.views.items()}
    declared_map = {
        view_name: list(view.declared_upstreams)
        for view_name, view in old_model.views.items()
    }
    effective_op = op
    if effective_op == "upsert":
        effective_op = "modify" if name in sql_map else "add"
    if effective_op == "add" and name in sql_map:
        raise BusinessError(
            E_PATCH_TARGET_EXISTS,
            f"新增视图已存在: {name}",
            view=name,
            edit_round=event["sequence"],
        )
    if effective_op in {"modify", "delete"} and name not in sql_map:
        raise BusinessError(
            E_PATCH_TARGET_MISSING,
            f"补丁目标视图不存在: {name}",
            view=name,
            edit_round=event["sequence"],
        )
    if effective_op == "delete":
        del sql_map[name]
        del declared_map[name]
        return sql_map, declared_map, effective_op

    declared = list(declared_map.get(name, []))
    if "dependencies" in event:
        declared = list(event["dependencies"])
    for dependency in event.get("upstream_add", []):
        if dependency not in declared:
            declared.append(dependency)
    for dependency in event.get("upstream_remove", []):
        declared = [item for item in declared if item != dependency]
    if name in declared:
        raise BusinessError(
            E_DEPENDENCY_SELF,
            f"视图存在自依赖: {name}",
            view=name,
            node_id="dependency",
            dependency_path=[name, name],
            edit_round=event["sequence"],
        )
    if "sql" in event:
        sql_map[name] = event["sql"]
    elif effective_op == "add":
        raise BusinessError(
            E_PATCH_INVALID,
            "新增视图补丁必须提供 SQL",
            view=name,
            edit_round=event["sequence"],
        )
    declared_map[name] = declared
    return sql_map, declared_map, effective_op


def compute_patch_candidate(
    old_model: DefinitionSet,
    event: dict[str, Any],
) -> dict[str, Any]:
    sql_map, declared_map, effective_op = _apply_definition_patch(old_model, event)
    direct_name = event["view"]
    old_view = old_model.views.get(direct_name)
    old_plan = old_view.plan if old_view else None
    schemas = old_model.source_schemas()
    candidate_view_names = set(sql_map)
    new_plan: dict[str, Any] | None = None
    if effective_op != "delete":
        try:
            new_plan = parse_sql_plan(
                direct_name,
                sql_map[direct_name],
                schemas,
                dependency_names=candidate_view_names,
            )
        except BusinessError as exc:
            if exc.edit_round is None:
                exc.edit_round = event["sequence"]
            raise

    changes = (
        contract_changes(old_plan, new_plan)
        if old_plan is not None and new_plan is not None
        else {
            "removed": [],
            "added": list((new_plan or {}).get("output_columns", [])),
            "mapping_changed": [],
        }
    )
    if effective_op == "delete" and old_plan is not None:
        changes = {
            "removed": list(old_plan["output_columns"]),
            "added": [],
            "mapping_changed": [],
        }
    invalidation = _build_invalidation_closure(
        old_model=old_model,
        direct_view=direct_name,
        op=effective_op,
        old_plan=old_plan,
        new_plan=new_plan,
        changes=changes,
        patch_id=event["patch_id"],
        sequence=event["sequence"],
    )
    try:
        candidate_model = build_view_model(
            sources=old_model.sources,
            sql_map=sql_map,
            declared_upstreams=declared_map,
            raw={"patch_id": event["patch_id"]},
        )
    except BusinessError as exc:
        if exc.edit_round is None:
            exc.edit_round = event["sequence"]
        if exc.view != direct_name:
            errors = invalidation["compile_errors"]
            first = errors[0] if errors else None
            raise BusinessError(
                E_DOWNSTREAM_COLUMN_MISSING,
                "补丁使下游视图无法编译",
                view=(first or {}).get("view", exc.view),
                node_id=(first or {}).get("node_id", exc.node_id),
                edit_round=event["sequence"],
                missing_column=(first or {}).get("missing_column"),
                reference_path=(
                    (first or {}).get("reference_path")
                    or [direct_name, exc.view]
                ),
                root_view=direct_name,
                invalidation=invalidation,
            ) from exc
        raise

    # A wildcard or other schema-driven change can alter a downstream plan even
    # when no explicit column reference was removed.  Detect those changes from
    # the freshly compiled candidate and extend the closure deterministically.
    extra_changed = False
    for name in sorted(candidate_model.views):
        if name == direct_name or name not in old_model.views:
            continue
        if old_model.views[name].plan["plan_summary"] == candidate_model.views[name].plan["plan_summary"]:
            continue
        plan_changes = contract_changes(
            old_model.views[name].plan,
            candidate_model.views[name].plan,
        )
        if not any(plan_changes.values()):
            continue
        extra_changed = True
        entry = next(
            (
                item
                for item in invalidation["affected_views"]
                if item["view"] == name
            ),
            None,
        )
        if entry is None:
            entry = {
                "view": name,
                "reason": "upstream_schema_changed",
                "dependency_path": [direct_name, name],
                "source": {
                    "patch_id": event["patch_id"],
                    "sequence": event["sequence"],
                    "root_view": direct_name,
                },
                "affected_columns": [],
                "dirty_nodes": _node_ids(candidate_model.views[name].plan),
                "plan_recompute": True,
                "sql_recompute": False,
            }
            invalidation["affected_views"].append(entry)
        entry["affected_columns"] = sorted(
            set(entry["affected_columns"])
            | set(plan_changes["removed"])
            | set(plan_changes["added"])
            | set(plan_changes["mapping_changed"])
        )
    if extra_changed:
        invalidation["affected_views"].sort(key=lambda item: item["view"])
        invalidation["reusable_views"] = sorted(
            name
            for name in candidate_model.views
            if name not in {item["view"] for item in invalidation["affected_views"]}
        )

    if invalidation["compile_errors"]:
        first = invalidation["compile_errors"][0]
        raise BusinessError(
            E_DOWNSTREAM_COLUMN_MISSING,
            "补丁使下游视图无法编译",
            view=first["view"],
            node_id=first["node_id"],
            edit_round=event["sequence"],
            missing_column=first["missing_column"],
            reference_path=first["reference_path"],
            root_view=direct_name,
            invalidation=invalidation,
        )

    result = {
        "status": "computed",
        "patch_id": event["patch_id"],
        "sequence": event["sequence"],
        "op": effective_op,
        "view": direct_name,
        "logical_clock_ms": event["logical_clock_ms"],
        "changed_columns": changes,
        "invalidation": invalidation,
        "candidate_model": candidate_model,
        "sql_map": sql_map,
        "declared_map": declared_map,
    }
    if "column_dependencies" in event:
        declared_dependencies = normalize_declared_column_dependencies(
            event["column_dependencies"],
            view=direct_name,
        )
        direct_view = candidate_model.views[direct_name]
        for column, expected in declared_dependencies.items():
            actual = {
                (item["source"], item["column"])
                for item in direct_view.column_dependencies.get(column, [])
            }
            expected_set = {
                (item["source"], item["column"]) for item in expected
            }
            if actual != expected_set:
                raise BusinessError(
                    E_PATCH_INVALID,
                    "补丁列依赖摘要与计划不一致",
                    view=direct_name,
                    edit_round=event["sequence"],
                    column=column,
                )
    return result


def invalidation_result(
    computed: dict[str, Any],
    *,
    base_generation: int,
    target_generation: int,
    duplicate: bool = False,
) -> dict[str, Any]:
    invalidation = computed["invalidation"]
    return {
        "status": "committed",
        "duplicate": duplicate,
        "patch_id": computed["patch_id"],
        "sequence": computed["sequence"],
        "op": computed["op"],
        "view": computed["view"],
        "logical_clock_ms": computed["logical_clock_ms"],
        "base_plan_generation": base_generation,
        "plan_generation": target_generation,
        "changed_columns": computed["changed_columns"],
        "invalidation": invalidation,
        "counts": {
            "affected_views": len(invalidation["affected_views"]),
            "reusable_views": len(invalidation["reusable_views"]),
            "compile_errors": len(invalidation["compile_errors"]),
        },
    }

