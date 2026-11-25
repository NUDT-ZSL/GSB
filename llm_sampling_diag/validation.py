from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

from .errors import DiagnosticError, ErrorCode
from .model import UnifiedTask


def validate_config(config: object) -> None:
    from .model import OperatorConfig

    if not isinstance(config, OperatorConfig):
        raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "config must be OperatorConfig", field="config")
    config.validate()


def validate_task(task: UnifiedTask) -> None:
    if not task.case_id:
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "case_id must not be empty", field="case_id")
    if task.tie_policy is None:
        raise DiagnosticError(
            ErrorCode.E_TIE_POLICY_UNSPECIFIED,
            "tie_policy is not declared",
            field="tie_policy",
            case_id=task.case_id,
        )
    if task.tie_policy != "index_asc":
        raise DiagnosticError(
            ErrorCode.E_TIE_POLICY_UNSPECIFIED,
            f"unsupported tie_policy {task.tie_policy!r}",
            field="tie_policy",
            case_id=task.case_id,
        )
    if task.seed is None:
        raise DiagnosticError(
            ErrorCode.E_SEED_UNSPECIFIED,
            "seed is not declared",
            field="seed",
            case_id=task.case_id,
        )
    if isinstance(task.seed, bool) or not isinstance(task.seed, int) or task.seed < 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "seed must be a non-negative integer",
            field="seed",
            case_id=task.case_id,
        )
    if not task.logits:
        raise DiagnosticError(
            ErrorCode.E_SHAPE_MISMATCH,
            "logits vector is empty",
            field="logits",
            case_id=task.case_id,
        )
    if len(task.logits_shape) != 1:
        raise DiagnosticError(
            ErrorCode.E_SHAPE_MISMATCH,
            "logits shape must be one-dimensional",
            field="logits_shape",
            case_id=task.case_id,
            details={"declared_shape": list(task.logits_shape), "actual_shape": [len(task.logits)]},
        )
    if task.logits_shape[0] != len(task.logits):
        raise DiagnosticError(
            ErrorCode.E_SHAPE_MISMATCH,
            "declared logits shape does not match values",
            field="logits_shape",
            case_id=task.case_id,
            details={"declared_shape": list(task.logits_shape), "actual_shape": [len(task.logits)]},
        )
    for index, value in enumerate(task.logits):
        if not math.isfinite(value):
            raise DiagnosticError(
                ErrorCode.E_FORMAT_CORRUPT,
                "logits contain a non-finite value",
                field=f"logits[{index}]",
                case_id=task.case_id,
            )
    if task.k_mode not in {"top_k", "top_p"}:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "k_mode must be top_k or top_p",
            field="k_mode",
            case_id=task.case_id,
        )
    if task.k_mode == "top_k":
        if not float(task.k_value).is_integer() or task.k_value < 1:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "top_k k_value must be a positive integer",
                field="k_value",
                case_id=task.case_id,
            )
    else:
        if not 0.0 < task.k_value <= 1.0:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "top_p k_value must be in (0, 1]",
                field="k_value",
                case_id=task.case_id,
            )
    if not math.isfinite(task.temperature) or task.temperature <= 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "temperature must be positive",
            field="temperature",
            case_id=task.case_id,
        )
    if task.top_p is not None and not 0.0 < task.top_p <= 1.0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "top_p must be in (0, 1]",
            field="top_p",
            case_id=task.case_id,
        )
    if task.num_samples < 1:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "num_samples must be >= 1",
            field="num_samples",
            case_id=task.case_id,
        )
    if task.revision < 1:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "revision must be >= 1",
            field="revision",
            case_id=task.case_id,
        )
    if not task.revision_id:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "revision_id must not be empty",
            field="revision_id",
            case_id=task.case_id,
        )
    if not math.isfinite(task.error_budget) or task.error_budget < 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "error_budget must be finite and non-negative",
            field="error_budget",
            case_id=task.case_id,
        )
    if task.chunk_plan.chunk_size <= 0:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "chunk_size must be positive",
            field="chunk_plan.chunk_size",
            case_id=task.case_id,
        )
    expected_chunks = (len(task.logits) + task.chunk_plan.chunk_size - 1) // task.chunk_plan.chunk_size
    if task.chunk_plan.chunk_count != expected_chunks:
        raise DiagnosticError(
            ErrorCode.E_SHAPE_MISMATCH,
            "chunk_count does not match logits length and chunk_size",
            field="chunk_plan.chunk_count",
            case_id=task.case_id,
            details={"expected": expected_chunks, "actual": task.chunk_plan.chunk_count},
        )
    for index in task.candidate_mask.excluded_indices:
        if index < 0 or index >= len(task.logits):
            raise DiagnosticError(
                ErrorCode.E_SHAPE_MISMATCH,
                "candidate mask index is outside logits shape",
                field="candidate_mask.excluded_indices",
                case_id=task.case_id,
                details={"index": index, "logits_length": len(task.logits)},
            )


def validate_tasks(tasks: Iterable[UnifiedTask]) -> None:
    task_list = list(tasks)
    for task in task_list:
        validate_task(task)
    assert_unique_case_ids(task_list)


def assert_unique_case_ids(tasks: Iterable[UnifiedTask]) -> None:
    seen: dict[str, UnifiedTask] = {}
    for task in tasks:
        if task.case_id in seen:
            first = seen[task.case_id]
            raise DiagnosticError(
                ErrorCode.E_DUP_CASE,
                f"duplicate case_id {task.case_id!r}",
                field="case_id",
                case_id=task.case_id,
                details={
                    "first_source": first.source_ref,
                    "second_source": task.source_ref,
                    "first_revision": first.revision,
                    "second_revision": task.revision,
                },
            )
        seen[task.case_id] = task


def check_cross_format_semantics(tasks: dict[str, UnifiedTask], *, include_revision: bool = True) -> None:
    if not tasks:
        return
    reference_name = sorted(tasks)[0]
    reference = tasks[reference_name].semantic_projection(include_revision=include_revision)
    for name in sorted(tasks):
        if name == reference_name:
            continue
        candidate = tasks[name].semantic_projection(include_revision=include_revision)
        from .canonical import first_difference

        difference = first_difference(reference, candidate)
        if difference is not None:
            raise DiagnosticError(
                ErrorCode.E_SEMANTIC_INCOMPLETE,
                f"normalized task differs between {reference_name} and {name}",
                field=difference.get("path", "$"),
                case_id=tasks[name].case_id,
                details={"reference_format": reference_name, "candidate_format": name, "difference": difference},
            )


def group_by_case(tasks: Iterable[UnifiedTask]) -> dict[str, list[UnifiedTask]]:
    result: dict[str, list[UnifiedTask]] = defaultdict(list)
    for task in tasks:
        result[task.case_id].append(task)
    return dict(result)

