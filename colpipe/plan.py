"""Logical planning, filter pushdown, column pruning and audit reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .catalog import Column, Dataset, PipelineRequest, Table
from .codes import E_PLAN
from .errors import PipeError
from .query import BoundQuery, BoundRef, Predicate, SelectItem
from .stats import SCALE, build_statistics, estimate_scans
from .util import canonical_json, sha256_json


def _column_dict(table: Table, column: Column) -> dict[str, Any]:
    return {
        "ref": f"{table.name}.{column.name}",
        "column": column.name,
        "type": column.type,
        "nullable": column.nullable,
    }


def _ref_dict(query: BoundQuery, dataset: Dataset, ref: BoundRef) -> dict[str, Any]:
    table = dataset.table(query.table_for_alias[ref.alias])
    column = table.column(ref.column)
    return {
        "ref": ref.text,
        "alias": ref.alias,
        "column": ref.column,
        "type": ref.type,
        "nullable": column.nullable,
    }


def _select_dict(query: BoundQuery, dataset: Dataset, item: SelectItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "alias": item.alias,
        "kind": item.kind,
        "type": item.type,
        "nullable": item.nullable,
    }
    if item.kind == "group" and item.ref is not None:
        payload["ref"] = item.ref.text
    else:
        payload.update(
            {
                "func": item.func,
                "arg": item.arg.text if item.arg else "*",
                "star": item.star,
            }
        )
    return payload


def _stable_unique_refs(refs: Iterable[BoundRef]) -> list[BoundRef]:
    seen: set[str] = set()
    result: list[BoundRef] = []
    for ref in refs:
        if ref.text not in seen:
            seen.add(ref.text)
            result.append(ref)
    return result


def _global_column_order(query: BoundQuery, dataset: Dataset) -> list[BoundRef]:
    result: list[BoundRef] = []
    for alias in query.aliases:
        table = dataset.table(query.table_for_alias[alias])
        result.extend(BoundRef(alias, column.name, column.type) for column in table.columns)
    return result


def _sort_refs(refs: Iterable[BoundRef], order: list[BoundRef]) -> list[BoundRef]:
    wanted = {ref.text for ref in refs}
    return [ref for ref in order if ref.text in wanted]


def _operator(op: str, operator_id: str, attrs: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"op": op, "id": operator_id, "attrs": attrs, "children": children}


def _scan_columns(
    query: BoundQuery,
    dataset: Dataset,
    alias: str,
    required: set[str],
    baseline: bool,
) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    table = dataset.table(query.table_for_alias[alias])
    for column in table.columns:
        ref = BoundRef(alias, column.name, column.type)
        if baseline or ref.text in required:
            columns.append(
                {
                    "ref": ref.text,
                    "alias": alias,
                    "column": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                }
            )
    return columns


def _scan_for_alias(
    query: BoundQuery,
    dataset: Dataset,
    alias: str,
    required: set[str],
    filters: list[Predicate],
    *,
    baseline: bool,
) -> dict[str, Any]:
    table = dataset.table(query.table_for_alias[alias])
    attrs: dict[str, Any] = {
        "alias": alias,
        "table": table.name,
        "schema_version": table.schema_version,
        "row_count": table.row_count,
        "columns": _scan_columns(query, dataset, alias, required, baseline),
        "pushed_filters": [] if baseline else [predicate.to_dict() for predicate in filters],
    }
    scan = _operator("Scan", f"scan:{alias}", attrs, [])
    if not baseline and filters:
        return scan
    if baseline and filters:
        return _operator(
            "Filter",
            f"filter:{alias}",
            {"conditions": [predicate.to_dict() for predicate in filters], "pushed": False},
            [scan],
        )
    return scan


def _build_tree(
    query: BoundQuery,
    dataset: Dataset,
    *,
    baseline: bool,
    scan_columns: Mapping[str, set[str]],
    local_filters: Mapping[str, list[Predicate]],
    join_output_refs: list[BoundRef],
    project_input_refs: list[BoundRef],
) -> dict[str, Any]:
    left_scan = _scan_for_alias(
        query,
        dataset,
        query.from_alias,
        scan_columns[query.from_alias],
        local_filters[query.from_alias],
        baseline=baseline,
    )
    right_scan = _scan_for_alias(
        query,
        dataset,
        query.join_alias,
        scan_columns[query.join_alias],
        local_filters[query.join_alias],
        baseline=baseline,
    )
    if baseline:
        all_join_refs = _global_column_order(query, dataset)
        join_output = [_ref_dict(query, dataset, ref) for ref in all_join_refs]
    else:
        join_output = [_ref_dict(query, dataset, ref) for ref in join_output_refs]
    join_node = _operator(
        "Join",
        "join:inner",
        {
            "join_type": "inner",
            "probe_alias": query.from_alias,
            "build_alias": query.join_alias,
            "conditions": [pair.to_dict() for pair in query.join_pairs],
            "output_columns": join_output,
        },
        [left_scan, right_scan],
    )

    current = join_node
    # The current grammar has only single-column conjuncts, so every WHERE
    # predicate is table-local.  Keeping this branch makes residual predicates
    # explicit and auditable if the grammar grows.
    residual = [predicate for predicate in query.where if predicate.ref.alias not in query.aliases]
    if residual:
        current = _operator(
            "Filter",
            "filter:residual",
            {"conditions": [predicate.to_dict() for predicate in residual], "pushed": False},
            [current],
        )

    project_columns: list[dict[str, Any]] = []
    if baseline:
        for alias in query.aliases:
            table = dataset.table(query.table_for_alias[alias])
            for column in table.columns:
                project_columns.append(
                    {
                        "ref": f"{alias}.{column.name}",
                        "alias": alias,
                        "column": column.name,
                        "type": column.type,
                        "nullable": column.nullable,
                    }
                )
    else:
        project_columns = [_ref_dict(query, dataset, ref) for ref in project_input_refs]
    current = _operator(
        "Project",
        "project:join-input",
        {"purpose": "join-to-aggregate", "columns": project_columns},
        [current],
    )

    group_by = [_ref_dict(query, dataset, ref) for ref in query.group_by]
    aggregates = [
        _select_dict(query, dataset, item) for item in query.select if item.kind == "agg"
    ]
    aggregate_outputs = [_select_dict(query, dataset, item) for item in query.select]
    current = _operator(
        "Aggregate",
        "aggregate:final",
        {
            "group_by": group_by,
            "aggregates": aggregates,
            "output_columns": aggregate_outputs,
        },
        [current],
    )
    current = _operator(
        "Project",
        "project:final",
        {
            "purpose": "aggregate-output",
            "columns": [
                {
                    "ref": item.alias,
                    "alias": item.alias,
                    "column": item.alias,
                    "type": item.type,
                    "nullable": item.nullable,
                }
                for item in query.select
            ],
        },
        [current],
    )
    return current


def _predicate_refs(predicates: Iterable[Predicate]) -> list[BoundRef]:
    return _stable_unique_refs(predicate.ref for predicate in predicates)


def _pushdown_report(query: BoundQuery, local_filters: Mapping[str, list[Predicate]]) -> dict[str, Any]:
    pushed: list[dict[str, Any]] = []
    by_alias: dict[str, list[dict[str, Any]]] = {}
    for alias in query.aliases:
        conditions = local_filters[alias]
        by_alias[alias] = [predicate.to_dict() for predicate in conditions]
        for index, predicate in enumerate(conditions):
            table_name = query.table_for_alias[alias]
            pushed.append(
                {
                    "id": f"push:{alias}:{index}",
                    "scan": f"scan:{alias}",
                    "table": table_name,
                    "condition": predicate.to_dict(),
                    "reason": "every AND conjunct references exactly one base relation; moving it below the join preserves semantics and shrinks scan output",
                }
            )
    return {
        "pushed_conditions": pushed,
        "by_scan": by_alias,
        "residual_conditions": [],
        "derivation": "WHERE is flattened over AND; each bound predicate has one base-relation column reference, so it is safe to push to that relation's scan",
    }


def _pruning_report(
    query: BoundQuery,
    dataset: Dataset,
    scan_required: Mapping[str, set[str]],
    local_filters: Mapping[str, list[Predicate]],
    project_refs: list[BoundRef],
) -> dict[str, Any]:
    upper_reason_refs: dict[str, list[str]] = {}
    for index, ref in enumerate(query.group_by):
        upper_reason_refs.setdefault(ref.text, []).append(f"group_by[{index}]")
    for index, item in enumerate(query.select):
        if item.arg is not None:
            upper_reason_refs.setdefault(item.arg.text, []).append(f"select[{index}].aggregate")
    for index, predicate in enumerate(query.where):
        upper_reason_refs.setdefault(predicate.ref.text, []).append(f"where[{index}]")
    for pair in query.join_pairs:
        upper_reason_refs.setdefault(pair.left.text, []).append("join key")
        upper_reason_refs.setdefault(pair.right.text, []).append("join key")

    dropped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for alias in query.aliases:
        table = dataset.table(query.table_for_alias[alias])
        for column in table.columns:
            ref_text = f"{alias}.{column.name}"
            reasons = upper_reason_refs.get(ref_text, [])
            if ref_text in scan_required[alias]:
                kept.append(
                    {
                        "location": f"scan:{alias}",
                        "column": ref_text,
                        "type": column.type,
                        "nullable": column.nullable,
                        "reasons": sorted(set(reasons)),
                    }
                )
            else:
                dropped.append(
                    {
                        "location": f"scan:{alias}",
                        "column": ref_text,
                        "type": column.type,
                        "nullable": column.nullable,
                        "reason": "not referenced by pushed filters, join keys, group keys, aggregate arguments or final projection",
                    }
                )
    all_join_columns = _global_column_order(query, dataset)
    kept_join = {ref.text for ref in project_refs}
    scan_kept = {text for alias in query.aliases for text in scan_required[alias]}
    join_dropped = [
        {
            "location": "join:inner",
            "column": ref.text,
            "type": ref.type,
            "reason": "not required by aggregate input or final projection",
        }
        for ref in all_join_columns
        if ref.text in scan_kept and ref.text not in kept_join
    ]
    return {
        "kept_columns": kept,
        "dropped_columns": dropped + join_dropped,
        "join_intermediate": {
            "kept": sorted(kept_join, key=lambda text: next(i for i, ref in enumerate(all_join_columns) if ref.text == text)),
            "dropped": [item["column"] for item in join_dropped],
        },
        "projection_input_columns": [ref.text for ref in project_refs],
        "derivation": {
            "upward_requirements": upper_reason_refs,
            "rule": "required = pushed filter refs ∪ join key refs ∪ aggregate/group refs; scan and join columns preserve source schema order, type and null bitmap",
            "null_semantics": "kept columns retain their original type descriptor and null bitmap; pruning never materialises or coerces null values",
        },
    }


def _index_set(query: BoundQuery) -> list[dict[str, Any]]:
    columns = sorted({pair.right.column for pair in query.join_pairs})
    return [
        {
            "name": f"hash-build:{query.join_alias}",
            "kind": "hash_join_build",
            "table": query.join_table,
            "alias": query.join_alias,
            "columns": columns,
            "rebuildable": True,
        }
    ]


@dataclass(frozen=True)
class PlanBundle:
    request: PipelineRequest
    statistics: dict[str, Any]
    optimized_plan: dict[str, Any]
    baseline_plan: dict[str, Any]
    optimized_summary_sha256: str
    baseline_summary_sha256: str
    optimized_summary: str
    baseline_summary: str
    pushdown: dict[str, Any]
    pruning: dict[str, Any]
    estimates: dict[str, Any]
    split_plan: dict[str, Any]
    query_summary_sha256: str
    indexes: list[dict[str, Any]]
    baseline_split_plan: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_summary_sha256": self.query_summary_sha256,
            "optimized": {
                "summary_sha256": self.optimized_summary_sha256,
                "summary": self.optimized_summary,
                "plan": self.optimized_plan,
            },
            "baseline": {
                "summary_sha256": self.baseline_summary_sha256,
                "summary": self.baseline_summary,
                "plan": self.baseline_plan,
            },
            "pushdown": self.pushdown,
            "pruning": self.pruning,
            "estimates": self.estimates,
            "split_plan": self.split_plan,
            "baseline_split_plan": self.baseline_split_plan,
            "indexes": self.indexes,
            "statistics": self.statistics,
        }


def plan_summary(plan: dict[str, Any], query: BoundQuery, optimization: str) -> tuple[dict[str, Any], str]:
    payload = {
        "optimization": optimization,
        "query_summary_sha256": query.summary_sha256,
        "logical_plan": plan,
    }
    summary = canonical_json(payload)
    return payload, sha256_json(payload)


def _full_scan_split(dataset: Dataset, query: BoundQuery, parallelism: int, min_rows_per_task: int) -> dict[str, Any]:
    scans: dict[str, Any] = {}
    task_order: list[str] = []
    for alias in query.aliases:
        table = dataset.table(query.table_for_alias[alias])
        count = min(parallelism, max(1, (table.row_count + min_rows_per_task - 1) // min_rows_per_task))
        chunk = (table.row_count + count - 1) // count if table.row_count else 0
        tasks = []
        for part in range(count):
            start = part * chunk
            end = min(table.row_count, start + chunk)
            task_id = f"scan:{alias}:{part}"
            tasks.append(
                {
                    "task_id": task_id,
                    "part": part,
                    "input_range": [start, end],
                    "input_rows": max(0, end - start),
                }
            )
            task_order.append(task_id)
        scans[alias] = {"alias": alias, "table": table.name, "task_count": count, "tasks": tasks}
    payload = {
        "mode": "baseline-full-scan",
        "parallelism_budget": parallelism,
        "min_rows_per_task": min_rows_per_task,
        "scans": scans,
        "task_order": task_order,
        "total_tasks": len(task_order),
    }
    payload["split_sha256"] = sha256_json(payload)
    return payload


def build_plans(request: PipelineRequest) -> PlanBundle:
    query = request.query
    dataset = request.dataset
    if not isinstance(query, BoundQuery):
        raise PipeError(E_PLAN, "request is missing a bound query", "$.query")

    local_filters: dict[str, list[Predicate]] = {alias: [] for alias in query.aliases}
    residual: list[Predicate] = []
    for predicate in query.where:
        if predicate.ref.alias in local_filters:
            local_filters[predicate.ref.alias].append(predicate)
        else:  # defensive: a single predicate should always bind to one table
            residual.append(predicate)
    for alias in local_filters:
        local_filters[alias].sort(key=lambda item: item.sort_key)

    aggregate_refs = list(query.group_by)
    for item in query.select:
        if item.arg is not None:
            aggregate_refs.append(item.arg)
    aggregate_refs.extend(predicate.ref for predicate in residual)
    aggregate_refs = _stable_unique_refs(aggregate_refs)
    global_order = _global_column_order(query, dataset)
    project_input_refs = _sort_refs(aggregate_refs, global_order)

    join_refs = [pair.left for pair in query.join_pairs] + [pair.right for pair in query.join_pairs]
    scan_required: dict[str, set[str]] = {alias: set() for alias in query.aliases}
    for ref in project_input_refs + _stable_unique_refs(join_refs):
        scan_required[ref.alias].add(ref.text)
    for alias, predicates in local_filters.items():
        for predicate in predicates:
            scan_required[alias].add(predicate.ref.text)

    optimized = _build_tree(
        query,
        dataset,
        baseline=False,
        scan_columns=scan_required,
        local_filters=local_filters,
        join_output_refs=project_input_refs,
        project_input_refs=project_input_refs,
    )
    baseline = _build_tree(
        query,
        dataset,
        baseline=True,
        scan_columns={alias: set() for alias in query.aliases},
        local_filters=local_filters,
        join_output_refs=[],
        project_input_refs=[],
    )
    _, optimized_hash = plan_summary(optimized, query, "optimized")
    _, baseline_hash = plan_summary(baseline, query, "baseline")
    optimized_payload, _ = plan_summary(optimized, query, "optimized")
    baseline_payload, _ = plan_summary(baseline, query, "baseline")
    statistics = build_statistics(dataset)
    estimates = estimate_scans(
        dataset,
        optimized,
        statistics,
        parallelism=request.settings.parallelism,
        min_rows_per_task=request.settings.min_rows_per_task,
    )
    split_plan = estimates
    pushdown = _pushdown_report(query, local_filters)
    pruning = _pruning_report(query, dataset, scan_required, local_filters, project_input_refs)
    indexes = _index_set(query)
    baseline_split = _full_scan_split(
        dataset,
        query,
        request.settings.parallelism,
        request.settings.min_rows_per_task,
    )
    return PlanBundle(
        request,
        statistics,
        optimized,
        baseline,
        optimized_hash,
        baseline_hash,
        canonical_json(optimized_payload),
        canonical_json(baseline_payload),
        pushdown,
        pruning,
        estimates,
        split_plan,
        query.summary_sha256,
        indexes,
        baseline_split,
    )


def walk_plan(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []):
        yield from walk_plan(child)


def find_operator(plan: dict[str, Any], op: str) -> dict[str, Any]:
    for node in walk_plan(plan):
        if node["op"] == op:
            return node
    raise PipeError(E_PLAN, f"plan has no {op} operator")
