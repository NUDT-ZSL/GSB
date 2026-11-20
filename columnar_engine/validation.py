from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from .errors import fail, require
from .expressions import (
    PREDICATE_OPS,
    infer_value_expr_type,
    predicate_refs,
    value_expr_refs,
)
from .model import (
    AGGREGATE_OPS,
    SUPPORTED_NODE_TYPES,
    Batch,
    Dataset,
    ExecutionConfig,
    Plan,
)
from .util import load_json_strict, safe_float, sha256_file


TYPE_ALIASES = {
    "int": "int64",
    "integer": "int64",
    "int64": "int64",
    "long": "int64",
    "float": "float64",
    "double": "float64",
    "float64": "float64",
    "bool": "bool",
    "boolean": "bool",
}
REQUIRED_CONFIG_FIELDS = {
    "vector_width",
    "max_parallel_tasks",
    "scan_memory_budget_mib",
    "round_time_budget_ms",
    "logical_clock_step_ms",
    "fixed_scale",
    "float_tolerance",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_values(values: Any, declared_type: str | None, column: str) -> tuple[np.ndarray, str]:
    if not isinstance(values, list):
        fail("E_BAD_BATCH", f"column {column!r} values must be an array")
    if not values:
        declared = TYPE_ALIASES.get(declared_type or "int64")
        if declared is None:
            fail("E_BAD_BATCH", f"column {column!r} has unsupported type {declared_type!r}")
        return np.asarray([], dtype=np.dtype(declared)), declared

    if declared_type is not None:
        declared = TYPE_ALIASES.get(str(declared_type).lower())
        if declared is None:
            fail("E_BAD_BATCH", f"column {column!r} has unsupported type {declared_type!r}")
    else:
        if all(isinstance(value, bool) for value in values):
            declared = "bool"
        elif all(_is_int(value) for value in values):
            declared = "int64"
        elif all(
            (isinstance(value, (int, float)) and not isinstance(value, bool))
            for value in values
        ):
            declared = "float64"
        else:
            fail(
                "E_BAD_BATCH",
                f"column {column!r} must contain only integers, floats, or booleans",
            )

    if declared == "bool":
        if not all(isinstance(value, bool) for value in values):
            fail("E_BAD_BATCH", f"column {column!r} declared bool but contains non-bool values")
        return np.asarray(values, dtype=np.bool_), declared

    if declared == "int64":
        normalized: list[int] = []
        for value in values:
            if not _is_int(value):
                fail("E_BAD_BATCH", f"column {column!r} declared int64 but contains non-integer values")
            if value < np.iinfo(np.int64).min or value > np.iinfo(np.int64).max:
                fail("E_BAD_BATCH", f"column {column!r} contains an int64 overflow value")
            normalized.append(value)
        return np.asarray(normalized, dtype=np.int64), declared

    normalized_float: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail("E_BAD_BATCH", f"column {column!r} declared float64 but contains non-numeric values")
        number = float(value)
        if math.isinf(number):
            fail("E_BAD_BATCH", f"column {column!r} contains infinite value")
        normalized_float.append(number)
    return np.asarray(normalized_float, dtype=np.float64), declared


def load_dataset(path: str | Path) -> Dataset:
    path = Path(path)
    payload = load_json_strict(path, code="E_BAD_BATCH")
    if not isinstance(payload, dict):
        fail("E_BAD_BATCH", "batch file root must be an object")
    batches_raw = payload.get("batches")
    if not isinstance(batches_raw, list) or not batches_raw:
        fail("E_BAD_BATCH", "batch file must contain a non-empty batches array")

    batches: list[Batch] = []
    all_columns: dict[str, list[np.ndarray]] = {}
    column_types: dict[str, str] = {}
    previous_batch_id: int | None = None
    previous_columns: set[str] | None = None
    total_rows = 0

    for index, raw in enumerate(batches_raw):
        if not isinstance(raw, dict):
            fail("E_BAD_BATCH", f"batch[{index}] must be an object")
        batch_id = raw.get("batch_id")
        if not _is_int(batch_id):
            fail("E_BAD_BATCH", f"batch[{index}].batch_id must be an integer")
        if previous_batch_id is not None and batch_id <= previous_batch_id:
            fail(
                "E_BAD_BATCH",
                "batch_id values must be strictly increasing in file order",
                details={"previous": previous_batch_id, "current": batch_id},
            )
        previous_batch_id = batch_id

        rows = raw.get("rows")
        if not _is_int(rows) or rows <= 0:
            fail("E_BAD_BATCH", f"batch {batch_id} rows must be a positive integer")
        columns_raw = raw.get("columns")
        if not isinstance(columns_raw, dict) or not columns_raw:
            fail("E_BAD_BATCH", f"batch {batch_id} must contain a non-empty columns object")
        raw_names = list(columns_raw.keys())
        if len(raw_names) != len(set(raw_names)):
            fail("E_BAD_BATCH", f"batch {batch_id} contains duplicate column names")
        if previous_columns is None:
            previous_columns = set(raw_names)
        elif set(raw_names) != previous_columns:
            fail(
                "E_BAD_BATCH",
                f"batch {batch_id} column set differs from previous batches",
                details={
                    "missing": sorted(previous_columns - set(raw_names)),
                    "extra": sorted(set(raw_names) - previous_columns),
                },
            )

        columns: dict[str, np.ndarray] = {}
        for name, column_raw in columns_raw.items():
            if not isinstance(name, str) or not name:
                fail("E_BAD_BATCH", f"batch {batch_id} has an invalid column name")
            declared_type: str | None = None
            if isinstance(column_raw, dict):
                if "values" not in column_raw:
                    fail("E_BAD_BATCH", f"column {name!r} object requires values")
                declared_type = column_raw.get("type")
                values = column_raw["values"]
            else:
                values = column_raw
            array, resolved_type = _validate_values(values, declared_type, name)
            if array.size != rows:
                fail(
                    "E_BAD_BATCH",
                    f"column {name!r} length {array.size} does not match batch rows {rows}",
                    details={"batch_id": batch_id, "column": name},
                )
            previous_type = column_types.get(name)
            if previous_type is not None and previous_type != resolved_type:
                fail(
                    "E_BAD_BATCH",
                    f"column {name!r} type changed across batches",
                    details={"before": previous_type, "after": resolved_type},
                )
            column_types[name] = resolved_type
            columns[name] = array
            all_columns.setdefault(name, []).append(array)
        batches.append(Batch(batch_id=batch_id, columns=columns, rows=rows))
        total_rows += rows

    combined: dict[str, np.ndarray] = {}
    for name, arrays in all_columns.items():
        if len(arrays) == 1:
            combined[name] = arrays[0].copy()
        else:
            combined[name] = np.concatenate(arrays)
    return Dataset(
        path=path,
        source_digest=sha256_file(path),
        batches=batches,
        columns=combined,
        rows=total_rows,
        column_types=column_types,
    )


def load_config(path: str | Path) -> ExecutionConfig:
    payload = load_json_strict(path, code="E_BAD_CONFIG")
    if not isinstance(payload, dict):
        fail("E_BAD_CONFIG", "configuration root must be an object")
    missing = sorted(REQUIRED_CONFIG_FIELDS - set(payload))
    if missing:
        fail("E_BAD_CONFIG", "configuration is missing required fields", details={"missing": missing})

    vector_width = payload["vector_width"]
    if not _is_int(vector_width) or vector_width <= 0:
        fail("E_BAD_CONFIG", "vector_width must be a positive integer")
    max_parallel = payload["max_parallel_tasks"]
    if not _is_int(max_parallel) or max_parallel <= 0:
        fail("E_BAD_CONFIG", "max_parallel_tasks must be a positive integer")
    scan_budget = safe_float(
        payload["scan_memory_budget_mib"],
        code="E_BAD_CONFIG",
        what="scan_memory_budget_mib",
    )
    if scan_budget <= 0:
        fail("E_BAD_CONFIG", "scan_memory_budget_mib must be positive")
    global_budget = safe_float(
        payload.get("global_memory_budget_mib", scan_budget * max_parallel),
        code="E_BAD_CONFIG",
        what="global_memory_budget_mib",
    )
    if global_budget <= 0:
        fail("E_BAD_CONFIG", "global_memory_budget_mib must be positive")
    operator_budget = safe_float(
        payload.get("operator_buffer_budget_mib", global_budget),
        code="E_BAD_CONFIG",
        what="operator_buffer_budget_mib",
    )
    if operator_budget <= 0:
        fail("E_BAD_CONFIG", "operator_buffer_budget_mib must be positive")
    round_budget = safe_float(
        payload["round_time_budget_ms"],
        code="E_BAD_CONFIG",
        what="round_time_budget_ms",
    )
    if round_budget <= 0:
        fail("E_BAD_CONFIG", "round_time_budget_ms must be positive")
    clock_step = safe_float(
        payload["logical_clock_step_ms"],
        code="E_BAD_CONFIG",
        what="logical_clock_step_ms",
    )
    if clock_step <= 0:
        fail("E_BAD_CONFIG", "logical_clock_step_ms must be positive")
    fixed_scale = payload["fixed_scale"]
    if not _is_int(fixed_scale) or fixed_scale < 0 or fixed_scale > 18:
        fail("E_BAD_CONFIG", "fixed_scale must be an integer in [0, 18]")
    tolerance = safe_float(
        payload["float_tolerance"],
        code="E_BAD_CONFIG",
        what="float_tolerance",
    )
    if tolerance < 0:
        fail("E_BAD_CONFIG", "float_tolerance must be non-negative")
    verify_rewrite = payload.get("verify_rewrite", True)
    if not isinstance(verify_rewrite, bool):
        fail("E_BAD_CONFIG", "verify_rewrite must be boolean")
    return ExecutionConfig(
        vector_width=vector_width,
        max_parallel_tasks=max_parallel,
        scan_memory_budget_mib=scan_budget,
        global_memory_budget_mib=global_budget,
        round_time_budget_ms=round_budget,
        logical_clock_step_ms=clock_step,
        fixed_scale=fixed_scale,
        float_tolerance=tolerance,
        operator_buffer_budget_mib=operator_budget,
        verify_rewrite=verify_rewrite,
        raw=payload,
    )


def _validate_scale(value: Any, config: ExecutionConfig, node_id: str) -> int:
    if value is None:
        return config.fixed_scale
    if not _is_int(value) or value < 0 or value > 18:
        fail(
            "E_BAD_PLAN",
            "fixed-point scale must be an integer in [0, 18]",
            node_id=node_id,
        )
    return value


def parse_plan(payload: Any, dataset: Dataset, config: ExecutionConfig) -> Plan:
    if not isinstance(payload, dict):
        fail("E_BAD_PLAN", "plan root must be an object")
    root = payload.get("root")
    nodes_raw = payload.get("nodes")
    if not isinstance(root, str) or not root:
        fail("E_BAD_PLAN", "plan.root must be a non-empty string")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        fail("E_BAD_PLAN", "plan.nodes must be a non-empty array")

    nodes: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes_raw):
        if not isinstance(node, dict):
            fail("E_BAD_PLAN", f"node[{index}] must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            fail("E_BAD_PLAN", f"node[{index}].id must be a non-empty string")
        if node_id in nodes:
            fail("E_DUPLICATE_NODE", f"duplicate node id {node_id!r}", node_id=node_id)
        node_type = node.get("type")
        if node_type not in SUPPORTED_NODE_TYPES:
            fail("E_BAD_PLAN", f"unsupported node type {node_type!r}", node_id=node_id)
        nodes[node_id] = node
    if root not in nodes:
        fail("E_PLAN_REF", f"plan root {root!r} does not exist", node_id=root)

    parents: dict[str, str] = {}
    scans: list[str] = []
    for node_id, node in nodes.items():
        node_type = node["type"]
        if node_type == "scan":
            scans.append(node_id)
            if "input" in node:
                fail("E_BAD_PLAN", "scan node must not declare input", node_id=node_id)
            continue
        input_id = node.get("input")
        if not isinstance(input_id, str) or not input_id:
            fail("E_BAD_PLAN", "non-scan node requires input", node_id=node_id)
        if input_id not in nodes:
            fail("E_PLAN_REF", f"input node {input_id!r} does not exist", node_id=node_id)
        if input_id in parents:
            fail(
                "E_BAD_PLAN",
                f"node {input_id!r} has multiple parents; plan must be a tree",
                node_id=node_id,
            )
        parents[input_id] = node_id
    if len(scans) != 1:
        fail("E_BAD_PLAN", "plan must contain exactly one scan node", details={"scan_nodes": scans})

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            fail("E_PLAN_CYCLE", f"cycle detected at node {node_id!r}", node_id=node_id)
        if node_id in visited:
            return
        visiting.add(node_id)
        node = nodes[node_id]
        if node["type"] != "scan":
            visit(node["input"])
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)
    if visited != set(nodes):
        unreachable = sorted(set(nodes) - visited)
        fail("E_BAD_PLAN", "plan contains unreachable nodes", details={"nodes": unreachable})

    chain: list[str] = []
    cursor = root
    while True:
        chain.append(cursor)
        node = nodes[cursor]
        if node["type"] == "scan":
            break
        cursor = node["input"]
    chain.reverse()

    available = dataset.column_types
    schemas: dict[str, dict[str, str]] = {}
    for node_id in chain:
        node = nodes[node_id]
        node_type = node["type"]
        input_schema = schemas[node["input"]] if node_type != "scan" else None
        if node_type == "scan":
            columns = node.get("columns")
            if columns is None:
                columns = list(available.keys())
                node["columns"] = list(columns)
            if not isinstance(columns, list) or not columns:
                fail("E_BAD_PLAN", "scan.columns must be a non-empty array", node_id=node_id)
            if len(columns) != len(set(columns)):
                fail("E_BAD_PLAN", "scan columns must be unique", node_id=node_id)
            for column in columns:
                if not isinstance(column, str) or column not in available:
                    fail("E_PLAN_REF", f"scan references unknown column {column!r}", node_id=node_id)
            predicates = node.get("pushed_predicates", [])
            if not isinstance(predicates, list):
                fail("E_BAD_PLAN", "scan.pushed_predicates must be an array", node_id=node_id)
            for predicate in predicates:
                refs = predicate_refs(predicate, node_id=node_id)
                missing = sorted(refs - set(columns))
                if missing:
                    fail(
                        "E_PLAN_REF",
                        "pushed predicate references columns outside scan column set",
                        node_id=node_id,
                        details={"columns": missing},
                    )
            schemas[node_id] = {name: available[name] for name in columns}
            continue

        assert input_schema is not None
        if node_type == "filter":
            predicate = node.get("predicate")
            if not isinstance(predicate, dict) or predicate.get("op") not in PREDICATE_OPS:
                fail("E_BAD_PLAN", "filter requires a supported predicate object", node_id=node_id)
            missing = sorted(predicate_refs(predicate, node_id=node_id) - set(input_schema))
            if missing:
                fail(
                    "E_PLAN_REF",
                    "filter predicate references unknown columns",
                    node_id=node_id,
                    details={"columns": missing},
                )
            schemas[node_id] = dict(input_schema)
        elif node_type == "project":
            items = node.get("columns")
            if not isinstance(items, list) or not items:
                fail("E_BAD_PLAN", "project.columns must be a non-empty array", node_id=node_id)
            output: dict[str, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    fail("E_BAD_PLAN", "projection item must be an object", node_id=node_id)
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    fail("E_BAD_PLAN", "projection item requires a name", node_id=node_id)
                if name in output:
                    fail("E_BAD_PLAN", f"duplicate projection output {name!r}", node_id=node_id)
                if "source" in item:
                    source = item["source"]
                    if not isinstance(source, str) or source not in input_schema:
                        fail(
                            "E_PLAN_REF",
                            f"projection source {source!r} does not exist",
                            node_id=node_id,
                        )
                    output[name] = input_schema[source]
                    item["expr"] = {"column": source}
                elif "expr" in item:
                    refs = value_expr_refs(item["expr"], node_id=node_id)
                    missing = sorted(refs - set(input_schema))
                    if missing:
                        fail(
                            "E_PLAN_REF",
                            "projection expression references unknown columns",
                            node_id=node_id,
                            details={"columns": missing},
                        )
                    output[name] = infer_value_expr_type(item["expr"], input_schema, node_id=node_id)
                else:
                    fail("E_BAD_PLAN", "projection item requires source or expr", node_id=node_id)
            schemas[node_id] = output
        elif node_type == "aggregate":
            group_by = node.get("group_by", [])
            if not isinstance(group_by, list) or len(group_by) != len(set(group_by)):
                fail("E_BAD_PLAN", "aggregate.group_by must be a unique array", node_id=node_id)
            for column in group_by:
                if not isinstance(column, str) or column not in input_schema:
                    fail(
                        "E_PLAN_REF",
                        f"aggregate group_by column {column!r} does not exist",
                        node_id=node_id,
                    )
            aggregates = node.get("aggregates")
            if not isinstance(aggregates, list) or not aggregates:
                fail("E_BAD_PLAN", "aggregate.aggregates must be a non-empty array", node_id=node_id)
            output = {column: input_schema[column] for column in group_by}
            for aggregate in aggregates:
                if not isinstance(aggregate, dict):
                    fail("E_BAD_PLAN", "aggregate item must be an object", node_id=node_id)
                name = aggregate.get("name")
                op = aggregate.get("op")
                column = aggregate.get("column")
                if not isinstance(name, str) or not name or name in output:
                    fail("E_BAD_PLAN", "aggregate output name is missing or duplicated", node_id=node_id)
                if op not in AGGREGATE_OPS:
                    fail("E_BAD_PLAN", f"unsupported aggregate op {op!r}", node_id=node_id)
                if not isinstance(column, str) or column not in input_schema:
                    fail(
                        "E_PLAN_REF",
                        f"aggregate column {column!r} does not exist",
                        node_id=node_id,
                    )
                if op in {"sum", "sumsq", "avg", "min", "max", "fixed_sum"}:
                    if input_schema[column] not in {"int64", "float64"}:
                        fail("E_BAD_PLAN", f"aggregate op {op} requires a numeric column", node_id=node_id)
                if op == "fixed_sum":
                    aggregate["scale"] = _validate_scale(aggregate.get("scale"), config, node_id)
                if op in {"count", "fixed_sum"}:
                    output[name] = "int64"
                elif op in {"sum", "sumsq", "avg"}:
                    output[name] = "float64"
                else:
                    output[name] = input_schema[column]
            schemas[node_id] = output
        elif node_type == "window":
            partition_by = node.get("partition_by", [])
            if not isinstance(partition_by, list) or len(partition_by) != len(set(partition_by)):
                fail("E_BAD_PLAN", "window.partition_by must be a unique array", node_id=node_id)
            for column in partition_by:
                if not isinstance(column, str) or column not in input_schema:
                    fail(
                        "E_PLAN_REF",
                        f"window partition column {column!r} does not exist",
                        node_id=node_id,
                    )
            order_by = node.get("order_by")
            if not isinstance(order_by, str) or order_by not in input_schema:
                fail("E_PLAN_REF", "window.order_by must reference an input column", node_id=node_id)
            length = node.get("length")
            step = node.get("step")
            if not _is_int(length) or length <= 0:
                fail("E_BAD_PLAN", "window.length must be a positive integer", node_id=node_id)
            if not _is_int(step) or step <= 0:
                fail("E_BAD_PLAN", "window.step must be a positive integer", node_id=node_id)
            if step > length:
                fail("E_BAD_PLAN", "window.step must not exceed window.length", node_id=node_id)
            functions = node.get("functions")
            if not isinstance(functions, list) or not functions:
                fail("E_BAD_PLAN", "window.functions must be a non-empty array", node_id=node_id)
            output = {column: input_schema[column] for column in partition_by}
            output["_window_start"] = "int64"
            output["_window_end"] = "int64"
            output["_window_order_end"] = input_schema[order_by]
            for function in functions:
                if not isinstance(function, dict):
                    fail("E_BAD_PLAN", "window function must be an object", node_id=node_id)
                name = function.get("name")
                op = function.get("op")
                column = function.get("column")
                if not isinstance(name, str) or not name or name in output:
                    fail("E_BAD_PLAN", "window output name is missing or duplicated", node_id=node_id)
                if op not in AGGREGATE_OPS:
                    fail("E_BAD_PLAN", f"unsupported window op {op!r}", node_id=node_id)
                if not isinstance(column, str) or column not in input_schema:
                    fail(
                        "E_PLAN_REF",
                        f"window column {column!r} does not exist",
                        node_id=node_id,
                    )
                if op in {"sum", "sumsq", "avg", "min", "max", "fixed_sum"}:
                    if input_schema[column] not in {"int64", "float64"}:
                        fail("E_BAD_PLAN", f"window op {op} requires a numeric column", node_id=node_id)
                if op == "fixed_sum":
                    function["scale"] = _validate_scale(function.get("scale"), config, node_id)
                if op in {"count", "fixed_sum"}:
                    output[name] = "int64"
                elif op in {"sum", "sumsq", "avg"}:
                    output[name] = "float64"
                else:
                    output[name] = input_schema[column]
            schemas[node_id] = output

    return Plan(root=root, nodes=nodes, chain=chain, schemas=schemas, raw=payload)


def load_plan(path: str | Path, dataset: Dataset, config: ExecutionConfig) -> Plan:
    payload = load_json_strict(path, code="E_BAD_PLAN")
    return parse_plan(payload, dataset, config)


def load_inputs(
    batches_path: str | Path,
    plan_path: str | Path,
    config_path: str | Path,
) -> tuple[Dataset, dict[str, Any], ExecutionConfig]:
    dataset = load_dataset(batches_path)
    config = load_config(config_path)
    plan_payload = load_json_strict(plan_path, code="E_BAD_PLAN")
    parse_plan(plan_payload, dataset, config)
    return dataset, plan_payload, config

