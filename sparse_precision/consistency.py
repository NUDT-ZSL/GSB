"""Cross-mode checks that guard against silent path divergence."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
from scipy import sparse

from .kernel import ExecutionOutcome, _reduce_partials, execute_batches
from .models import CaseSpec, Operands, OperatorConfig
from .strategy import decide_strategy_for_spec


def _compare_arrays(
    left: np.ndarray,
    right: np.ndarray,
    config: OperatorConfig,
) -> tuple[bool, float, float]:
    reference = np.asarray(left, dtype=np.float64)
    actual = np.asarray(right, dtype=np.float64)
    abs_error = np.abs(actual - reference)
    max_abs = float(abs_error.max()) if abs_error.size else 0.0
    allowed = config.atol + config.rtol * np.abs(reference)
    excess = abs_error - allowed
    max_excess = float(excess.max()) if excess.size else 0.0
    return bool(np.all(abs_error <= allowed)), max_abs, max_excess


def _quiet_config(config: OperatorConfig) -> OperatorConfig:
    return dataclasses.replace(
        config,
        diagnose=False,
        enable_self_check=False,
        max_memory_bytes=10**15,
        max_blocks=10**9,
        max_aggregation_bytes=10**15,
        forced_row_block=None,
        forced_accumulation_order=None,
        forced_aggregation=None,
    )


def _run_internal(
    spec: CaseSpec,
    operands: Operands,
    config: OperatorConfig,
) -> tuple[ExecutionOutcome, dict[str, Any]]:
    plan = decide_strategy_for_spec(spec, operands, config)
    outcome = execute_batches(
        spec, operands, plan, config, reference=None
    )
    return outcome, plan


def _degenerate_spmm_check(
    spec: CaseSpec,
    operands: Operands,
    config: OperatorConfig,
) -> dict[str, Any]:
    dense_b = []
    sparse_b = []
    for index in range(spec.batch_size):
        if spec.kind == "spmm":
            assert operands.b_dense is not None
            vector = np.asarray(
                operands.b_dense[index][:, :1], dtype=np.float64
            )
            sparse_vector = sparse.csr_matrix(vector)
        else:
            assert operands.b_sparse is not None
            sparse_vector = operands.b_sparse[index][:, :1].tocsr()
            vector = np.asarray(sparse_vector.toarray(), dtype=np.float64)
        dense_b.append(vector)
        sparse_b.append(sparse_vector)

    dense_spec = dataclasses.replace(
        spec,
        kind="spmm",
        shape_mnk=(spec.m, spec.k, 1),
        nnz_b=spec.batch_size * spec.k,
        b_storage="dense",
        checksums={},
    )
    sparse_spec = dataclasses.replace(
        spec,
        kind="spspmm",
        shape_mnk=(spec.m, spec.k, 1),
        nnz_b=sum(int(item.nnz) for item in sparse_b),
        b_storage="sparse",
        checksums={},
    )
    dense_operands = Operands(
        a=operands.a, b_dense=tuple(dense_b)
    )
    sparse_operands = Operands(
        a=operands.a, b_sparse=tuple(sparse_b)
    )
    sparse_plan = decide_strategy_for_spec(
        sparse_spec, sparse_operands, config
    )
    dense_outcome = execute_batches(
        dense_spec, dense_operands, sparse_plan, config, reference=None
    )
    sparse_outcome = execute_batches(
        sparse_spec, sparse_operands, sparse_plan, config, reference=None
    )
    passed, max_abs, max_excess = _compare_arrays(
        sparse_outcome.result, dense_outcome.result, config
    )
    return {
        "category": "spmm_vs_degenerate_spspmm",
        "passed": passed,
        "max_abs_diff": max_abs,
        "max_tolerance_excess": max_excess,
        "compared_shapes": list(sparse_outcome.result.shape),
        "detail": "A@D with dense D of width 1 versus sparse B=A@D structure",
    }


def _transpose_check(
    spec: CaseSpec,
    operands: Operands,
    outcome: ExecutionOutcome,
    config: OperatorConfig,
) -> dict[str, Any]:
    transposed_a = []
    transposed_b = []
    for index in range(spec.batch_size):
        if spec.kind == "spspmm":
            assert operands.b_sparse is not None
            transposed_a.append(operands.b_sparse[index].T.tocsr())
        else:
            assert operands.b_dense is not None
            transposed_a.append(
                sparse.csr_matrix(operands.b_dense[index].T)
            )
        transposed_b.append(operands.a[index].T.tocsr())
    transposed_spec = dataclasses.replace(
        spec,
        kind="spspmm",
        shape_mnk=(spec.n, spec.k, spec.m),
        nnz_a=sum(int(item.nnz) for item in transposed_a),
        nnz_b=sum(int(item.nnz) for item in transposed_b),
        b_storage="sparse",
        checksums={},
    )
    transposed_operands = Operands(
        a=tuple(transposed_a), b_sparse=tuple(transposed_b)
    )
    transposed_outcome, _ = _run_internal(
        transposed_spec, transposed_operands, config
    )
    expected = outcome.result.transpose(0, 2, 1)
    passed, max_abs, max_excess = _compare_arrays(
        expected, transposed_outcome.result, config
    )
    return {
        "category": "transpose_semantics",
        "passed": passed,
        "max_abs_diff": max_abs,
        "max_tolerance_excess": max_excess,
        "detail": "(A@B)^T == B^T@A^T under the same tolerance",
    }


def _layout_view_check(
    spec: CaseSpec,
    operands: Operands,
    plan: dict[str, Any],
    outcome: ExecutionOutcome,
    config: OperatorConfig,
) -> dict[str, Any]:
    a_view = tuple(item.tocsc().tocsr() for item in operands.a)
    if spec.kind == "spmm":
        assert operands.b_dense is not None
        view_operands = Operands(a=a_view, b_dense=operands.b_dense)
    else:
        assert operands.b_sparse is not None
        view_operands = Operands(
            a=a_view,
            b_sparse=tuple(item.tocsc().tocsr() for item in operands.b_sparse),
        )
    viewed = execute_batches(
        spec, view_operands, plan, config, reference=None
    )
    passed, max_abs, max_excess = _compare_arrays(
        outcome.result, viewed.result, config
    )
    return {
        "category": "row_major_column_major_view",
        "passed": passed,
        "max_abs_diff": max_abs,
        "max_tolerance_excess": max_excess,
        "detail": "CSR and CSC round-trip views produce the same result",
    }


def _reduction_recompute_check(
    spec: CaseSpec,
    outcome: ExecutionOutcome,
    plan: dict[str, Any],
    config: OperatorConfig,
) -> dict[str, Any]:
    max_diff = 0.0
    reconstructed = np.zeros(
        (spec.batch_size, spec.m, spec.n), dtype=np.float64
    )
    for batch_index, row_blocks in enumerate(outcome.partials):
        for row_block_index, partials in enumerate(row_blocks):
            recomputed = _reduce_partials(
                partials,
                order=str(plan["accumulation_order"]),
                compensated=bool(plan["compensated_sum"]),
            )
            recorded = outcome.block_outputs[batch_index][row_block_index]
            if recomputed.size:
                max_diff = max(
                    max_diff, float(np.max(np.abs(recomputed - recorded)))
                )
            row_start = row_block_index * int(plan["row_block_size"])
            row_stop = min(spec.m, row_start + int(plan["row_block_size"]))
            reconstructed[batch_index, row_start:row_stop, :] = recomputed
    if reconstructed.size:
        max_diff = max(
            max_diff, float(np.max(np.abs(reconstructed - outcome.result)))
        )
    passed = max_diff <= config.atol
    return {
        "category": "partial_reduction_recompute",
        "passed": passed,
        "max_abs_diff": max_diff,
        "detail": "recorded k-tile partials recompute each block and final matrix",
    }


def run_consistency_checks(
    spec: CaseSpec,
    operands: Operands,
    outcome: ExecutionOutcome,
    plan: dict[str, Any],
    config: OperatorConfig,
) -> dict[str, Any]:
    if not config.enable_self_check:
        return {"enabled": False, "passed": True, "checks": []}
    element_count = spec.batch_size * spec.m * spec.n
    if element_count > config.max_self_check_elements:
        return {
            "enabled": True,
            "passed": True,
            "skipped": True,
            "skip_reason": (
                "output element count exceeds max_self_check_elements"
            ),
            "checks": [],
            "failed_categories": [],
        }
    quiet = _quiet_config(config)
    checks = [
        _degenerate_spmm_check(spec, operands, quiet),
        _transpose_check(spec, operands, outcome, quiet),
        _layout_view_check(spec, operands, plan, outcome, quiet),
        _reduction_recompute_check(spec, outcome, plan, config),
    ]
    failed = [item["category"] for item in checks if not item["passed"]]
    return {
        "enabled": True,
        "passed": not failed,
        "checks": checks,
        "failed_categories": failed,
    }

