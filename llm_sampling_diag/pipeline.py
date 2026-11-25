from __future__ import annotations

from typing import Any

from .canonical import sha256_json
from .diagnostics import diagnose
from .model import OperatorConfig, UnifiedTask
from .operators import execute_sampling
from .validation import validate_config, validate_task


def cache_key(task: UnifiedTask, config: OperatorConfig) -> str:
    return sha256_json(
        {
            "schema": "sampling-cache-key-v1",
            "case_id": task.case_id,
            "parameter_version": task.revision,
            "revision_id": task.revision_id,
            "logits": list(task.logits),
            "logits_shape": list(task.logits_shape),
            "k_mode": task.k_mode,
            "k_value": task.k_value,
            "temperature": task.temperature,
            "tie_policy": task.tie_policy,
            "seed": task.seed,
            "excluded_indices": list(task.candidate_mask.excluded_indices),
            "error_budget": task.error_budget,
            "chunk_plan": task.chunk_plan.to_dict(),
            "num_samples": task.num_samples,
            "top_p": task.top_p,
            "config": config.to_dict(),
        }
    )


def run_task(
    task: UnifiedTask,
    config: OperatorConfig,
    *,
    include_reference_output: bool = False,
) -> dict[str, Any]:
    validate_config(config)
    validate_task(task)
    execution = execute_sampling(task, config).to_dict()
    spectrum = diagnose(task, config)
    result: dict[str, Any] = {
        "case_id": task.case_id,
        "parameter_version": task.revision,
        "normalized_task": task.semantic_projection(),
        "config": config.to_dict(),
        "cache_key": cache_key(task, config),
        "candidate_order": execution["candidate_order"],
        "candidates": execution["candidates"],
        "removed_candidates": execution["removed_candidates"],
        "sample_sequence": execution["sample_sequence"],
        "sample_rank_sequence": execution["sample_rank_sequence"],
        "subnormal_events": execution["subnormal_events"],
        "rounding_trace": execution["rounding_trace"],
        "error_spectrum": spectrum,
    }
    if include_reference_output:
        reference_execution = execute_sampling(
            task,
            OperatorConfig(
                mask_priority=config.mask_priority,
                dtype="float64",
                rounding_mode="none",
                subnormal_guard="none",
                tolerance=config.tolerance,
                rng_algorithm=config.rng_algorithm,
            ),
            dtype_override="float64",
            rounding_override="none",
            subnormal_guard_override="none",
        ).to_dict()
        result["reference_output"] = reference_execution
    result["result_checksum"] = sha256_json(
        {
            "cache_key": result["cache_key"],
            "candidate_order": result["candidate_order"],
            "candidates": result["candidates"],
            "sample_sequence": result["sample_sequence"],
            "error_spectrum": result["error_spectrum"],
        }
    )
    return result

