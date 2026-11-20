from __future__ import annotations

import itertools
import json
import math
from typing import Any

import numpy as np

from .model import ExecutionConfig, Plan, Relation
from .ops import (
    NAN_POLICY,
    ROUNDING_MODE,
    SUM_ORDER,
    relation_from_wire,
)


def _scalar(value: Any) -> Any:
    return value.item() if isinstance(value, np.generic) else value


def _key_for_row(relation: Relation, index: int, node: dict[str, Any]) -> dict[str, Any]:
    key: dict[str, Any] = {}
    if node["type"] == "aggregate":
        for column in node.get("group_by", []):
            key[column] = _scalar(relation.columns[column][index])
    elif node["type"] == "window":
        for column in node.get("partition_by", []):
            key[column] = _scalar(relation.columns[column][index])
        for column in ("_window_start", "_window_end"):
            if column in relation.columns:
                key[column] = _scalar(relation.columns[column][index])
    return key


def _producer_columns(node: dict[str, Any]) -> list[dict[str, Any]]:
    if node["type"] == "aggregate":
        return list(node["aggregates"])
    if node["type"] == "window":
        return list(node["functions"])
    return []


def build_error_spectrum(
    *,
    run_id: str,
    plan: Plan,
    config: ExecutionConfig,
    result: Relation,
    connection: Any,
) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT node_id, shard_id, output_json FROM node_outputs
        WHERE run_id=? ORDER BY node_id, shard_id
        """,
        (run_id,),
    ).fetchall()
    outputs: dict[str, list[Relation]] = {}
    for row in rows:
        node_id = row["node_id"]
        if plan.nodes.get(node_id, {}).get("type") not in {"aggregate", "window"}:
            continue
        outputs.setdefault(node_id, []).append(
            relation_from_wire(json.loads(row["output_json"]))
        )

    spectrum: dict[str, Any] = {
        "fixed_scale": config.fixed_scale,
        "float_tolerance": config.float_tolerance,
        "rounding_mode": ROUNDING_MODE,
        "nan_policy": NAN_POLICY,
        "aggregation_order": SUM_ORDER,
        "nodes": {},
        "precision_sensitive_count": 0,
        "dual_path_checks": [],
    }
    sensitive_count = 0
    for node_id, parts in outputs.items():
        relation = _concat(parts)
        node = plan.nodes[node_id]
        producer_names = {
            item["name"] for item in _producer_columns(node)
        }
        entries: list[dict[str, Any]] = []
        by_column: dict[str, list[dict[str, Any]]] = {}
        for row_index in range(relation.row_count):
            row_key = _key_for_row(relation, row_index, node)
            for column in relation.columns:
                if column not in relation.error_bounds:
                    continue
                value = _scalar(relation.columns[column][row_index])
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                bound = float(relation.error_bounds[column][row_index])
                entry = {
                    "row": row_index,
                    "key": row_key,
                    "column": column,
                    "value": value,
                    "error_bound": bound,
                    "sources": relation.error_sources.get(column) or [
                        f"{node_id}:{column}",
                        "no_error_source_recorded",
                    ],
                }
                entries.append(entry)
                by_column.setdefault(column, []).append(entry)

        sensitive: list[dict[str, Any]] = []
        for column, values in by_column.items():
            if column not in producer_names:
                continue
            for left, right in itertools.combinations(values, 2):
                if not (
                    isinstance(left["value"], (int, float))
                    and isinstance(right["value"], (int, float))
                ):
                    continue
                if not (math.isfinite(float(left["value"])) and math.isfinite(float(right["value"]))):
                    continue
                difference = abs(float(left["value"]) - float(right["value"]))
                if difference < config.float_tolerance:
                    item = {
                        "kind": "two_groups" if node["type"] == "aggregate" else "two_windows",
                        "column": column,
                        "left_row": left["row"],
                        "right_row": right["row"],
                        "left_key": left["key"],
                        "right_key": right["key"],
                        "difference": difference,
                        "combined_error_bound": left["error_bound"] + right["error_bound"],
                        "decision_ambiguous": True,
                        "source_chain": list(
                            dict.fromkeys(left["sources"] + right["sources"])
                        ),
                    }
                    sensitive.append(item)
                    if len(sensitive) >= 1000:
                        break
            if len(sensitive) >= 1000:
                break
        spectrum["nodes"][node_id] = {
            "type": node["type"],
            "rows": relation.row_count,
            "results": entries,
            "precision_sensitive": sensitive,
        }
        sensitive_count += len(sensitive)

        spectrum["dual_path_checks"].extend(
            _dual_path_checks(node_id, node, relation, config)
        )
    spectrum["precision_sensitive_count"] = sensitive_count
    spectrum["final_result_digest"] = _relation_digest(result)
    return spectrum


def _dual_path_checks(
    node_id: str,
    node: dict[str, Any],
    relation: Relation,
    config: ExecutionConfig,
) -> list[dict[str, Any]]:
    producers = _producer_columns(node)
    fixed = [item for item in producers if item["op"] == "fixed_sum"]
    floating = [item for item in producers if item["op"] == "sum"]
    checks: list[dict[str, Any]] = []
    if not fixed or not floating:
        return checks
    for fixed_item in fixed:
        for float_item in floating:
            if fixed_item["column"] != float_item["column"]:
                continue
            scale = int(fixed_item.get("scale", config.fixed_scale))
            multiplier = float(10**scale)
            fixed_values = relation.columns[fixed_item["name"]]
            float_values = relation.columns[float_item["name"]]
            for index in range(relation.row_count):
                normalized = float(fixed_values[index]) / multiplier
                difference = abs(normalized - float(float_values[index]))
                checks.append(
                    {
                        "node_id": node_id,
                        "row": index,
                        "fixed_column": fixed_item["name"],
                        "float_column": float_item["name"],
                        "scale": scale,
                        "fix_multiplier": int(10**scale),
                        "normalized_fixed": normalized,
                        "float_value": float(float_values[index]),
                        "difference": difference,
                        "fixed_error_bound": 0.0,
                        "float_error_bound": float(
                            relation.error_bounds.get(
                                float_item["name"],
                                np.zeros(relation.row_count, dtype=np.float64),
                            )[index]
                        ),
                        "precision_sensitive": difference < config.float_tolerance,
                        "source_chain": [
                            ROUNDING_MODE,
                            NAN_POLICY,
                            f"scale={scale}",
                            SUM_ORDER,
                        ],
                    }
                )
    return checks


def _concat(parts: list[Relation]) -> Relation:
    if len(parts) == 1:
        return parts[0]
    columns = {
        column: np.concatenate([part.columns[column] for part in parts])
        for column in parts[0].columns
    }
    bounds = {
        column: np.concatenate(
            [
                part.error_bounds.get(
                    column, np.zeros(part.row_count, dtype=np.float64)
                )
                for part in parts
            ]
        )
        for column in columns
    }
    sources = {
        column: list(
            dict.fromkeys(
                source
                for part in parts
                for source in part.error_sources.get(column, [])
            )
        )
        for column in columns
    }
    return Relation(
        columns=columns,
        row_count=sum(part.row_count for part in parts),
        error_bounds=bounds,
        error_sources=sources,
        source_shards=sorted({shard for part in parts for shard in part.source_shards}),
    )


def _relation_digest(relation: Relation) -> str:
    from .ops import relation_to_wire
    from .util import canonical_json, sha256_bytes

    return sha256_bytes(canonical_json(relation_to_wire(relation)).encode("utf-8"))

