from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import fail
from .model import Dataset, ExecutionConfig, Plan, Relation, STATEFUL_NODE_TYPES
from .ops import (
    apply_filter,
    apply_project,
    apply_scan,
    run_node_chain,
)


@dataclass
class MemoryResult:
    root: Relation
    node_outputs: dict[str, Relation] = field(default_factory=dict)
    shards: int = 0
    last_states: dict[str, dict[str, Any]] = field(default_factory=dict)


def shard_ranges(row_count: int, width: int) -> list[tuple[int, int]]:
    return [
        (start, min(start + width, row_count))
        for start in range(0, row_count, width)
    ]


def materialize_prefix(
    plan: Plan,
    dataset: Dataset,
    start: int,
    end: int,
    shard_id: int,
) -> tuple[Relation, str | None]:
    scan_node = plan.nodes[plan.chain[0]]
    relation = apply_scan(dataset, scan_node, start, end, shard_id)
    for node_id in plan.chain[1:]:
        node = plan.nodes[node_id]
        if node["type"] in STATEFUL_NODE_TYPES:
            return relation, node_id
        if node["type"] == "filter":
            relation = apply_filter(relation, node["predicate"])
        elif node["type"] == "project":
            relation = apply_project(relation, node["columns"])
    return relation, None


def execute_memory(plan: Plan, dataset: Dataset, config: ExecutionConfig) -> MemoryResult:
    ranges = shard_ranges(dataset.rows, config.vector_width)
    states: dict[str, dict[str, Any]] = {}
    root_parts: list[Relation] = []
    collected: dict[str, list[Relation]] = {}
    last_states: dict[str, dict[str, Any]] = {}

    for shard_id, (start, end) in enumerate(ranges):
        boundary, first_stateful = materialize_prefix(plan, dataset, start, end, shard_id)
        finalize = shard_id == len(ranges) - 1
        if first_stateful is None:
            relation = boundary
        else:
            states, relation, node_outputs = run_node_chain(
                plan,
                boundary,
                first_stateful,
                states,
                config,
                shard_id=shard_id,
                finalize=finalize,
            )
            for node_id, output in node_outputs.items():
                node = plan.nodes[node_id]
                if node["type"] in STATEFUL_NODE_TYPES:
                    collected.setdefault(node_id, []).append(output)
        if relation is not None and relation.row_count > 0:
            root_parts.append(relation)
        if finalize:
            last_states = states

    if not root_parts:
        from .ops import empty_relation

        root = empty_relation(plan.schemas[plan.root])
    elif plan.nodes[plan.root]["type"] == "aggregate" or any(
        plan.nodes[node_id]["type"] == "aggregate" for node_id in plan.chain
    ):
        root = root_parts[-1]
    else:
        root = _concat_outputs(root_parts)

    node_outputs: dict[str, Relation] = {}
    for node_id, outputs in collected.items():
        node_outputs[node_id] = _concat_outputs(outputs) if len(outputs) > 1 else outputs[0]
    return MemoryResult(
        root=root,
        node_outputs=node_outputs,
        shards=len(ranges),
        last_states=last_states,
    )


def _concat_outputs(parts: list[Relation]) -> Relation:
    if len(parts) == 1:
        return parts[0].copy()
    names = list(parts[0].columns)
    return Relation(
        columns={
            name: (
                np.concatenate([part.columns[name] for part in parts])
                if any(part.row_count for part in parts)
                else parts[0].columns[name].copy()
            )
            for name in names
        },
        row_count=sum(part.row_count for part in parts),
        error_bounds={
            name: (
                np.concatenate(
                    [
                        part.error_bounds.get(
                            name, np.zeros(part.row_count, dtype=np.float64)
                        )
                        for part in parts
                    ]
                )
                if any(part.row_count for part in parts)
                else np.asarray([], dtype=np.float64)
            )
            for name in names
        },
        error_sources={
            name: list(
                dict.fromkeys(
                    source
                    for part in parts
                    for source in part.error_sources.get(name, [])
                )
            )
            for name in names
        },
        source_shards=sorted({shard for part in parts for shard in part.source_shards}),
    )


def relation_diff(
    left: Relation,
    right: Relation,
    *,
    node_id: str,
) -> dict[str, Any] | None:
    if left.row_count != right.row_count:
        return {
            "kind": "row_count",
            "node_id": node_id,
            "left": left.row_count,
            "right": right.row_count,
        }
    if list(left.columns) != list(right.columns):
        return {
            "kind": "column_set",
            "node_id": node_id,
            "left": list(left.columns),
            "right": list(right.columns),
        }
    for name in left.columns:
        left_array = left.columns[name]
        right_array = right.columns[name]
        if left_array.dtype != right_array.dtype:
            return {
                "kind": "dtype",
                "node_id": node_id,
                "column": name,
                "left": str(left_array.dtype),
                "right": str(right_array.dtype),
            }
        if left_array.tobytes() != right_array.tobytes():
            mismatches = np.flatnonzero(left_array != right_array)
            first = int(mismatches[0]) if mismatches.size else 0
            return {
                "kind": "value",
                "node_id": node_id,
                "column": name,
                "row": first,
                "left": left_array[first].item(),
                "right": right_array[first].item(),
                "bitwise": True,
            }
    return None


def verify_equivalence(
    original_plan: Plan,
    rewritten_plan: Plan,
    dataset: Dataset,
    config: ExecutionConfig,
) -> dict[str, Any]:
    original = execute_memory(original_plan, dataset, config)
    rewritten = execute_memory(rewritten_plan, dataset, config)
    diff = relation_diff(original.root, rewritten.root, node_id=rewritten_plan.root)
    if diff is not None:
        for node_id in rewritten_plan.chain:
            if node_id in original.node_outputs and node_id in rewritten.node_outputs:
                node_diff = relation_diff(
                    original.node_outputs[node_id],
                    rewritten.node_outputs[node_id],
                    node_id=node_id,
                )
                if node_diff is not None:
                    fail(
                        "E_REWRITE_MISMATCH",
                        "rewritten plan differs from original plan",
                        node_id=node_id,
                        details=node_diff,
                    )
        fail(
            "E_REWRITE_MISMATCH",
            "rewritten plan root differs from original plan",
            node_id=rewritten_plan.root,
            details=diff,
        )
    for node_id, output in original.node_outputs.items():
        if node_id in rewritten.node_outputs:
            node_diff = relation_diff(output, rewritten.node_outputs[node_id], node_id=node_id)
            if node_diff is not None:
                fail(
                    "E_REWRITE_MISMATCH",
                    "stateful operator output differs after rewrite",
                    node_id=node_id,
                    details=node_diff,
                )
    return {
        "equivalent": True,
        "bitwise": True,
        "rows_compared": original.root.row_count,
        "shards_compared": original.shards,
        "columns_compared": list(original.root.columns),
        "stateful_nodes_compared": sorted(
            set(original.node_outputs) & set(rewritten.node_outputs)
        ),
    }

