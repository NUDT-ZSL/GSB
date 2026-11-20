from __future__ import annotations

import copy
from typing import Any

from .errors import fail
from .expressions import predicate_refs, value_expr_refs
from .model import Dataset, ExecutionConfig, Plan, RewriteReport, STATEFUL_NODE_TYPES
from .validation import parse_plan


def _node_refs(node: dict[str, Any]) -> set[str]:
    node_id = str(node.get("id", ""))
    node_type = node["type"]
    refs: set[str] = set()
    if node_type == "scan":
        for predicate in node.get("pushed_predicates", []):
            refs.update(predicate_refs(predicate, node_id=node_id))
    elif node_type == "filter":
        refs.update(predicate_refs(node["predicate"], node_id=node_id))
    elif node_type == "project":
        for item in node["columns"]:
            refs.update(value_expr_refs(item["expr"], node_id=node_id))
    elif node_type == "aggregate":
        refs.update(node.get("group_by", []))
        refs.update(item["column"] for item in node["aggregates"])
    elif node_type == "window":
        refs.update(node.get("partition_by", []))
        refs.add(node["order_by"])
        refs.update(item["column"] for item in node["functions"])
    return refs


def _prune_scan_columns(plan: Plan, dataset: Dataset) -> tuple[list[str], list[str], list[str]]:
    scan_id = plan.chain[0]
    scan = plan.nodes[scan_id]
    declared = list(scan["columns"])
    required: set[str] = set()
    for node in plan.nodes.values():
        required.update(_node_refs(node))
    after = [column for column in declared if column in required]
    if not after:
        fail(
            "E_BAD_PLAN",
            "column pruning removed every scan column; the plan has no data dependency",
            node_id=scan_id,
        )
    removed = [column for column in declared if column not in set(after)]
    scan["columns"] = after
    return declared, after, removed


def _resolve_input(node_id: str, nodes: dict[str, dict[str, Any]], redirects: dict[str, str]) -> str:
    cursor = node_id
    seen: set[str] = set()
    while cursor in redirects:
        if cursor in seen:
            fail("E_BAD_PLAN", "filter rewrite produced an input cycle", node_id=node_id)
        seen.add(cursor)
        cursor = redirects[cursor]
    if cursor not in nodes:
        fail("E_PLAN_REF", f"rewritten input {cursor!r} does not exist", node_id=node_id)
    return cursor


def rewrite_plan(payload: dict[str, Any], dataset: Dataset, config: ExecutionConfig) -> tuple[Plan, RewriteReport]:
    rewritten_raw = copy.deepcopy(payload)
    working = parse_plan(rewritten_raw, dataset, config)
    original_root = working.root
    final_output_columns_before = list(working.schemas[original_root])
    scan_id = working.chain[0]
    before_columns, after_columns, removed_columns = _prune_scan_columns(working, dataset)

    scan = working.nodes[scan_id]
    pushed = list(scan.get("pushed_predicates", []))
    pushed_reports: list[dict[str, Any]] = []
    redirects: dict[str, str] = {}
    removed_filters: list[str] = []

    scan_columns = set(scan["columns"])
    for node_id in working.chain[1:]:
        node = working.nodes[node_id]
        if node["type"] in STATEFUL_NODE_TYPES:
            break
        if node["type"] != "filter":
            continue
        refs = predicate_refs(node["predicate"], node_id=node_id)
        if refs <= scan_columns:
            pushed.append(copy.deepcopy(node["predicate"]))
            pushed_reports.append(
                {
                    "from_node": node_id,
                    "to_node": scan_id,
                    "location": "before_scan_apply_predicate",
                    "predicate": copy.deepcopy(node["predicate"]),
                }
            )
            redirects[node_id] = node["input"]
            removed_filters.append(node_id)

    if pushed:
        scan["pushed_predicates"] = pushed
    for node_id in removed_filters:
        working.nodes.pop(node_id)
    for node_id, node in working.nodes.items():
        if "input" in node:
            node["input"] = _resolve_input(node["input"], working.nodes, redirects)
    if working.root in redirects:
        working.root = _resolve_input(working.root, working.nodes, redirects)
    rewritten_raw["root"] = working.root
    rewritten_raw["nodes"] = list(working.nodes.values())

    rewritten_plan = parse_plan(copy.deepcopy(rewritten_raw), dataset, config)
    after_final = rewritten_plan.schemas.get(rewritten_plan.root, {})
    report = RewriteReport(
        before_columns=before_columns,
        after_columns=after_columns,
        removed_columns=removed_columns,
        pushed_predicates=pushed_reports,
        rewritten_plan=copy.deepcopy(rewritten_raw),
        final_output_columns_before=final_output_columns_before,
        final_output_columns_after=list(after_final),
        original_root=original_root,
        rewritten_root=rewritten_plan.root,
        removed_filter_nodes=removed_filters,
    )
    return rewritten_plan, report


def report_as_dict(report: RewriteReport, rewritten_plan: Plan) -> dict[str, Any]:
    report.final_output_columns_after = list(rewritten_plan.schemas[rewritten_plan.root])
    return report.to_dict()

