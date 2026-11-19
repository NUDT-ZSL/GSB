"""Deterministic block strategy selection and resource adjudication."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .codes import E_CONFIG_INVALID, E_RESOURCE_LIMIT, E_SHAPE_INVALID
from .errors import SparseOperatorError
from .generator import build_operands
from .models import CaseSpec, Operands, OperatorConfig
from .validation import (
    normalize_operands,
    parse_case_spec,
    validate_operands,
)


_UNIT_ROUNDOFF = 2.220446049250313e-16
_UNBOUNDED = 10**18


def _candidate_plan(
    spec: CaseSpec,
    operands: Operands,
    config: OperatorConfig,
    requested_row_block: int,
    *,
    max_memory_bytes: int,
    max_blocks: int,
    max_aggregation_bytes: int,
) -> dict[str, Any]:
    row_block = min(requested_row_block, spec.m)
    k_block = min(spec.k, max(1, requested_row_block))
    row_blocks = math.ceil(spec.m / row_block)
    k_blocks = math.ceil(spec.k / k_block)
    tiles_per_batch = row_blocks * k_blocks
    total_tiles = tiles_per_batch * spec.batch_size
    average_row_nnz = spec.nnz_a / max(1, spec.batch_size * spec.m)
    estimated_tile_a_nnz = average_row_nnz * row_block * (k_block / spec.k)
    estimated_tile_a_nnz = max(0.0, estimated_tile_a_nnz * 1.10)

    dense_tile_bytes = row_block * spec.n * 8
    partial_bytes = total_tiles * dense_tile_bytes
    output_bytes = spec.batch_size * spec.m * spec.n * 8
    overhead_bytes = total_tiles * 128

    if spec.kind == "spspmm":
        average_b_row_nnz = spec.nnz_b / max(1, spec.batch_size * spec.k)
        aggregation_entries = estimated_tile_a_nnz * (
            average_b_row_nnz if average_b_row_nnz > 0.0 else 1.0
        )
    else:
        average_b_row_nnz = float(spec.n)
        aggregation_entries = 0.0
    aggregation_bytes = int(
        math.ceil(aggregation_entries) * config.aggregation_entry_bytes
    )

    desired_aggregation = bool(
        config.allow_aggregation
        and spec.kind == "spspmm"
        and spec.n > 1
        and aggregation_bytes > 0
        and aggregation_bytes < dense_tile_bytes
    )
    if config.forced_aggregation is not None and spec.kind == "spspmm":
        aggregation_enabled = bool(config.forced_aggregation)
    else:
        aggregation_enabled = bool(
            desired_aggregation and aggregation_bytes <= max_aggregation_bytes
        )
    if aggregation_enabled and aggregation_bytes > max_aggregation_bytes:
        aggregation_fit = False
    else:
        aggregation_fit = True

    if config.forced_accumulation_order is not None:
        accumulation_order = config.forced_accumulation_order
    elif config.compensated_sum:
        accumulation_order = "forward"
    elif k_blocks >= 4:
        accumulation_order = "pairwise"
    else:
        accumulation_order = "forward"

    local_terms = max(0.0, average_row_nnz * (k_block / spec.k))
    local_risk = local_terms * (0.25 if config.compensated_sum else 1.0)
    merge_factor = 0.10 if config.compensated_sum else 1.0
    if accumulation_order == "pairwise":
        merge_factor *= 0.5
    merge_risk = k_blocks * merge_factor
    precision_risk = _UNIT_ROUNDOFF * (local_risk + merge_risk)

    aggregation_workspace = aggregation_bytes if aggregation_enabled else 0
    workspace_bytes = max(dense_tile_bytes, aggregation_workspace)
    estimated_peak_bytes = (
        output_bytes + partial_bytes + workspace_bytes + overhead_bytes
    )

    violations: list[dict[str, Any]] = []
    if estimated_peak_bytes > max_memory_bytes:
        violations.append(
            {
                "limit": "max_memory_bytes",
                "estimated": int(estimated_peak_bytes),
                "allowed": int(max_memory_bytes),
            }
        )
    if total_tiles > max_blocks:
        violations.append(
            {
                "limit": "max_blocks",
                "estimated": int(total_tiles),
                "allowed": int(max_blocks),
            }
        )
    if not aggregation_fit:
        violations.append(
            {
                "limit": "max_aggregation_bytes",
                "estimated": int(aggregation_bytes),
                "allowed": int(max_aggregation_bytes),
            }
        )

    return {
        "requested_row_block_size": int(requested_row_block),
        "row_block_size": int(row_block),
        "k_block_size": int(k_block),
        "row_blocks": int(row_blocks),
        "k_blocks": int(k_blocks),
        "tiles_per_batch": int(tiles_per_batch),
        "total_tiles": int(total_tiles),
        "accumulation_order": accumulation_order,
        "local_accumulation_order": (
            "grouped_ascending"
            if aggregation_enabled
            else ("reverse" if accumulation_order == "reverse" else "forward")
        ),
        "compensated_sum": bool(config.compensated_sum),
        "use_aggregation_path": bool(aggregation_enabled),
        "aggregation_requested_by_cost_model": bool(desired_aggregation),
        "cost_estimate": {
            "average_row_nnz": average_row_nnz,
            "estimated_tile_a_nnz": estimated_tile_a_nnz,
            "average_b_row_nnz": average_b_row_nnz,
            "aggregation_entries": aggregation_entries,
            "dense_tile_bytes": int(dense_tile_bytes),
            "partial_buffer_bytes": int(partial_bytes),
            "output_bytes": int(output_bytes),
            "aggregation_buffer_bytes": int(aggregation_bytes),
            "workspace_bytes": int(workspace_bytes),
            "overhead_bytes": int(overhead_bytes),
            "estimated_peak_bytes": int(estimated_peak_bytes),
            "precision_risk": precision_risk,
        },
        "coverage": {
            "rows": "ceil-chunk cover",
            "cols": "ceil-chunk cover",
            "row_coverage_last": int((row_blocks - 1) * row_block + row_block),
            "k_coverage_last": int((k_blocks - 1) * k_block + k_block),
            "all_rows_covered": bool((row_blocks - 1) * row_block < spec.m),
            "all_k_covered": bool((k_blocks - 1) * k_block < spec.k),
        },
        "feasible": not violations,
        "violations": violations,
    }


def _selection_key(plan: dict[str, Any], priority: str) -> tuple[Any, ...]:
    risk = float(plan["cost_estimate"]["precision_risk"])
    memory = int(plan["cost_estimate"]["estimated_peak_bytes"])
    row_block = int(plan["row_block_size"])
    if priority == "memory_first":
        return (memory, risk, -row_block)
    return (risk, memory, -row_block)


def _unique_candidates(spec: CaseSpec) -> list[int]:
    values: list[int] = []
    seen_effective: set[int] = set()
    for requested in sorted(set(spec.block_candidates)):
        effective = min(requested, spec.m)
        if effective not in seen_effective:
            seen_effective.add(effective)
            values.append(requested)
    return values


def _selected_record(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_block_size": plan["row_block_size"],
        "k_block_size": plan["k_block_size"],
        "row_blocks": plan["row_blocks"],
        "k_blocks": plan["k_blocks"],
        "total_tiles": plan["total_tiles"],
        "accumulation_order": plan["accumulation_order"],
        "local_accumulation_order": plan["local_accumulation_order"],
        "compensated_sum": plan["compensated_sum"],
        "use_aggregation_path": plan["use_aggregation_path"],
        "estimated_peak_bytes": plan["cost_estimate"]["estimated_peak_bytes"],
        "estimated_aggregation_bytes": plan["cost_estimate"][
            "aggregation_buffer_bytes"
        ],
        "precision_risk": plan["cost_estimate"]["precision_risk"],
    }


def decide_strategy_for_spec(
    spec: CaseSpec,
    operands: Operands,
    config: OperatorConfig | None = None,
) -> dict[str, Any]:
    config = config or OperatorConfig()
    config.validate()
    if config.forced_row_block is not None:
        requested_candidates = [config.forced_row_block]
    else:
        requested_candidates = _unique_candidates(spec)

    actual_candidates = [
        _candidate_plan(
            spec,
            operands,
            config,
            requested,
            max_memory_bytes=config.max_memory_bytes,
            max_blocks=config.max_blocks,
            max_aggregation_bytes=config.max_aggregation_bytes,
        )
        for requested in requested_candidates
    ]
    feasible = [plan for plan in actual_candidates if plan["feasible"]]
    if not feasible:
        violations: dict[str, Any] = {}
        for plan in actual_candidates:
            for violation in plan["violations"]:
                violations.setdefault(violation["limit"], []).append(
                    {
                        "row_block_size": plan["row_block_size"],
                        "estimated": violation["estimated"],
                        "allowed": violation["allowed"],
                    }
                )
        first_limit = next(iter(violations), "unknown")
        raise SparseOperatorError(
            E_RESOURCE_LIMIT,
            "no block strategy satisfies the configured resource limits",
            case_id=spec.case_id,
            phase="strategy",
            field=first_limit,
            details={
                "limits": {
                    "max_memory_bytes": config.max_memory_bytes,
                    "max_blocks": config.max_blocks,
                    "max_aggregation_bytes": config.max_aggregation_bytes,
                },
                "violations": violations,
            },
        )

    selected = min(
        feasible, key=lambda item: _selection_key(item, config.strategy_priority)
    )

    ideal_candidates = [
        _candidate_plan(
            spec,
            operands,
            config,
            requested,
            max_memory_bytes=_UNBOUNDED,
            max_blocks=_UNBOUNDED,
            max_aggregation_bytes=_UNBOUNDED,
        )
        for requested in _unique_candidates(spec)
    ]
    ideal = min(
        ideal_candidates, key=lambda item: _selection_key(item, config.strategy_priority)
    )

    degraded = False
    degradation: dict[str, Any] = {"degraded": False}
    if (
        selected["row_block_size"] != ideal["row_block_size"]
        or selected["use_aggregation_path"] != ideal["use_aggregation_path"]
        or selected["accumulation_order"] != ideal["accumulation_order"]
    ):
        degraded = True
        if selected["row_block_size"] < ideal["row_block_size"]:
            category = "smaller_row_block"
        elif selected["row_block_size"] > ideal["row_block_size"]:
            category = "larger_row_block"
        elif ideal["use_aggregation_path"] and not selected["use_aggregation_path"]:
            category = "aggregation_disabled"
        else:
            category = "resource_replanned"
        degradation = {
            "degraded": True,
            "category": category,
            "reason": (
                "resource limits required a feasible plan different from the "
                "unbounded precision-first plan"
            ),
            "ideal_plan": _selected_record(ideal),
            "selected_plan": _selected_record(selected),
            "semantics_unchanged": True,
            "tolerance_unchanged": True,
        }
        if (
            config.resource_policy == "error"
            and config.forced_row_block is None
            and config.forced_aggregation is None
            and config.forced_accumulation_order is None
        ):
            raise SparseOperatorError(
                E_RESOURCE_LIMIT,
                "resource_policy=error rejects the required downgrade",
                case_id=spec.case_id,
                phase="strategy",
                field="resource_policy",
                details={
                    "limits": {
                        "max_memory_bytes": config.max_memory_bytes,
                        "max_blocks": config.max_blocks,
                        "max_aggregation_bytes": config.max_aggregation_bytes,
                    },
                    "ideal_plan": _selected_record(ideal),
                    "selected_plan": _selected_record(selected),
                },
            )

    candidates_public = []
    for plan in actual_candidates:
        candidates_public.append(
            {
                **_selected_record(plan),
                "row_blocks": plan["row_blocks"],
                "k_blocks": plan["k_blocks"],
                "tiles_per_batch": plan["tiles_per_batch"],
                "aggregation_requested_by_cost_model": plan[
                    "aggregation_requested_by_cost_model"
                ],
                "feasible": plan["feasible"],
                "violations": plan["violations"],
                "coverage": plan["coverage"],
            }
        )

    if config.strategy_priority == "memory_first":
        priority_order = [
            "hard_shape_and_CSR_validation",
            "hard_resource_limits",
            "unchanged_tolerance_semantics",
            "estimated_peak_bytes",
            "precision_risk",
            "larger_row_block_deterministic_tiebreak",
        ]
    else:
        priority_order = [
            "hard_shape_and_CSR_validation",
            "hard_resource_limits",
            "unchanged_tolerance_semantics",
            "precision_risk",
            "estimated_peak_bytes",
            "larger_row_block_deterministic_tiebreak",
        ]

    decision: dict[str, Any] = {
        "case_id": spec.case_id,
        "kind": spec.kind,
        "shape_mnk": [spec.m, spec.k, spec.n],
        "batch_size": spec.batch_size,
        "policy": {
            "priority_order": priority_order,
            "strategy_priority": config.strategy_priority,
            "resource_policy": config.resource_policy,
        },
        "budget": {
            "max_memory_bytes": config.max_memory_bytes,
            "max_blocks": config.max_blocks,
            "max_aggregation_bytes": config.max_aggregation_bytes,
        },
        **_selected_record(selected),
        "cost_estimate": selected["cost_estimate"],
        "use_aggregation_path_reason": (
            "triplet aggregation estimated smaller than dense tile workspace"
            if selected["use_aggregation_path"]
            else (
                "dense operand requires row accumulator"
                if spec.kind == "spmm"
                else "direct dense tile is smaller or aggregation budget exceeded"
            )
        ),
        "accumulation_order_reason": (
            "compensated sequential Kahan reduction"
            if selected["accumulation_order"] == "forward"
            and config.compensated_sum
            else (
                "uncompensated pairwise merge shortens dependency chain"
                if selected["accumulation_order"] == "pairwise"
                else "explicit deterministic sequential order selected"
            )
        ),
        "coverage": selected["coverage"],
        "candidate_count": len(actual_candidates),
        "candidates_considered": candidates_public,
        "degradation": degradation,
        "decision_basis": (
            "candidate selection is lexicographic over precision risk, estimated "
            "peak memory and larger row block; same metadata plus config produces "
            "the same JSON bytes"
        ),
    }
    canonical = json.dumps(
        decision, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    decision["strategy_sha256"] = hashlib.sha256(canonical).hexdigest()
    return decision


def decide_strategy(
    case: CaseSpec | dict[str, Any],
    operands: Operands | Any | None = None,
    config: OperatorConfig | dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(config, dict):
        config = OperatorConfig.from_mapping(config)
    elif config is not None and not isinstance(config, OperatorConfig):
        raise SparseOperatorError(
            E_CONFIG_INVALID,
            "config must be an OperatorConfig or JSON object",
            phase="configuration",
            field="config",
        )
    if isinstance(case, dict):
        spec = parse_case_spec(case)
    else:
        spec = case
    if operands is None:
        resolved = build_operands(spec)
    elif isinstance(operands, Operands):
        resolved = operands
    else:
        if spec.kind == "spmm":
            if not isinstance(operands, (tuple, list)) or len(operands) != 2:
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "SpMM strategy input must be (A, D)",
                    case_id=spec.case_id,
                    phase="strategy",
                    field="operands",
                )
            resolved = normalize_operands(spec, operands[0], operands[1])
        else:
            if not isinstance(operands, (tuple, list)) or len(operands) != 2:
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "SpSpMM strategy input must be (A, B)",
                    case_id=spec.case_id,
                    phase="strategy",
                    field="operands",
                )
            resolved = normalize_operands(spec, operands[0], operands[1])
    validate_operands(spec, resolved)
    return decide_strategy_for_spec(spec, resolved, config)

