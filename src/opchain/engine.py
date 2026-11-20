"""Batch and query execution engine tying all modules together."""

import time

from .adjudication import adjudicate, tolerance_profile
from .blocks import run_chunked_chain
from .cache import (
    CacheEntry,
    ContentCache,
    build_cache_key,
    stage_cache_parts,
)
from .errors import ErrorCode, OpChainError
from .hashing import hash_array
from .operations import run_reference_chain
from .policy import build_estimate
from .snapshot import SNAPSHOT_KIND, build_snapshot
from .task import generate_input


def _stage_json(record):
    """Strip nothing; records are already JSON-safe (no ndarray attached)."""
    return record


def execute_case(spec, config, cache=None, latency_budget_ms=None):
    """Run one case: plan -> chunked path -> float64 reference -> adjudication."""
    from .policy import adjudicate_block_size

    case_config = config.with_overrides(spec.policy)
    plan = adjudicate_block_size(spec, case_config)
    block_size = plan["selected_block_size"]
    estimate = build_estimate(spec, block_size, case_config.latency_coeffs)

    tensor = generate_input(spec)
    input_hash = hash_array(tensor)
    # The declared adjudication precision follows the task dtype; reference math
    # itself always runs in float64 regardless of this profile.
    precision = spec.dtype

    cache = cache if cache is not None else ContentCache()
    if cache.size() >= case_config.max_cache_entries:
        raise OpChainError(
            ErrorCode.RESOURCE_LIMIT,
            "cache entry ceiling reached before execution",
            {"cache_size": cache.size(), "max_cache_entries": case_config.max_cache_entries},
        )

    stage_keys = [
        build_cache_key(stage_cache_parts(spec, stage, input_hash, block_size, precision))
        for stage in range(len(spec.op_chain))
    ]

    budget_exceeded = {"flag": False}
    started_perf = time.perf_counter()

    def deadline_check(stage, processed_blocks):
        if latency_budget_ms is None:
            return
        elapsed_ms = (time.perf_counter() - started_perf) * 1000.0
        if elapsed_ms > latency_budget_ms:
            budget_exceeded["flag"] = True
            raise OpChainError(
                ErrorCode.LATENCY_BUDGET,
                "observed wall time exceeded the per-query latency budget",
                {
                    "latency_budget_ms": float(latency_budget_ms),
                    "observed_elapsed_ms": float(elapsed_ms),
                    "completed_stages": list(range(stage)),
                    "current_stage": stage,
                    "processed_blocks": int(processed_blocks),
                },
            )

    chunked = run_chunked_chain(
        tensor, spec, block_size, deadline_check=deadline_check
    )
    observed_ms = (time.perf_counter() - started_perf) * 1000.0

    for stage, record in enumerate(chunked["stages"]):
        key = stage_keys[stage]
        entry = cache.get(key)
        if entry is None:
            cache.put(
                CacheEntry(
                    key=key,
                    case_id=spec.case_id,
                    stage=stage,
                    input_hash=input_hash,
                    chain_digest=spec.chain_digest(),
                    axis_order=tuple(record["semantic_axes"]),
                    block_size=block_size,
                    eps=spec.eps,
                    precision=precision,
                    activation_kind=spec.activation_kind,
                    content_hash=record["content_hash"],
                )
            )

    reference_final, reference_stages = run_reference_chain(tensor, spec)
    adjudication = adjudicate(
        chunked["arrays"],
        reference_stages,
        chunked["final"],
        reference_final,
        precision,
        spec.eps,
    )

    result = {
        "case_id": spec.case_id,
        "status": ErrorCode.OK,
        "spec": spec.to_dict(),
        "execution_plan": {
            "block_adjudication": plan,
            "estimate": estimate,
            "latency_budget_ms": latency_budget_ms,
        },
        "precision": precision,
        "tolerance": tolerance_profile(precision, spec.eps),
        "input_record": chunked["input_record"],
        "stages": [_stage_json(record) for record in chunked["stages"]],
        "final_hash": chunked["final_hash"],
        "final_shape": list(chunked["final"].shape),
        "processed_blocks": chunked["processed_blocks"],
        "adjudication": {
            "stage_reports": [
                {key: value for key, value in report.items() if key != "violations"}
                for report in adjudication["stage_reports"]
            ],
            "final_report": {
                key: value
                for key, value in adjudication["final_report"].items()
                if key != "violations"
            },
        },
        "cache_stage_keys": stage_keys,
        "cache_size_after": cache.size(),
        # Timing is an observation, not deterministic state: keep it outside
        # the persisted snapshot via a side-channel return value.
        "_observed_latency_ms": float(observed_ms),
    }
    return result, chunked["final"], cache


def run_batch(specs, config):
    cache = ContentCache()
    results = []
    observations = []
    for spec in specs:
        result, _, cache = execute_case(spec, config, cache=cache)
        observations.append(
            {
                "case_id": spec.case_id,
                "observed_latency_ms": result.pop("_observed_latency_ms"),
            }
        )
        results.append(result)
    envelope = build_snapshot(
        SNAPSHOT_KIND,
        {
            "results": results,
            "cache_entries": [entry.to_dict() for entry in cache.entries()],
        },
    )
    return envelope, cache, observations


def run_query(spec, config, batch_envelope, cache=None):
    """Interactive case query with observed timing and budget enforcement."""
    budget_ms = spec.policy.get("latency_budget_ms", config.latency_budget_ms)
    result, final_tensor, cache = execute_case(
        spec,
        config,
        cache=cache or ContentCache(),
        latency_budget_ms=float(budget_ms),
    )
    result["query"] = {
        "case_id": spec.case_id,
        "latency_budget_ms": float(budget_ms),
        "observed_latency_ms": result.get("_observed_latency_ms"),
        "estimate": result["execution_plan"]["estimate"],
    }

    consistency = compare_with_batch(spec, result, batch_envelope)
    result["query_consistency"] = consistency
    return result, final_tensor, cache


def compare_with_batch(spec, query_result, batch_envelope):
    """Require identical input summary, axes, chain and declared-precision output."""
    batch_payload = batch_envelope["payload"]
    match = next(
        (
            item
            for item in batch_payload["results"]
            if item["case_id"] == spec.case_id
        ),
        None,
    )
    if match is None:
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            f"case {spec.case_id} is absent from the batch snapshot",
            {"case_id": spec.case_id},
        )

    checks = {
        "input_hash_equal": (
            query_result["input_record"]["content_hash"]
            == match["input_record"]["content_hash"]
        ),
        "axis_order_equal": query_result["spec"]["axis_order"]
        == match["spec"]["axis_order"],
        "op_chain_equal": query_result["spec"]["op_chain"]
        == match["spec"]["op_chain"],
        "final_hash_equal": query_result["final_hash"] == match["final_hash"],
        "precision_equal": query_result["precision"] == match["precision"],
    }
    if not all(checks.values()):
        first_stage, first_block = _first_summary_divergence(query_result, match)
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            "query result diverges from the batch snapshot",
            {
                "checks": checks,
                "first_divergence_stage": first_stage,
                "first_divergence_block": first_block,
            },
        )

    stage_checks = []
    for query_stage, batch_stage in zip(query_result["stages"], match["stages"]):
        stage_checks.append(
            {
                "stage": query_stage["stage"],
                "content_hash_equal": query_stage["content_hash"]
                == batch_stage["content_hash"],
                "merged_summary_equal": query_stage["merged_summary"]
                == batch_stage["merged_summary"],
            }
        )
    return {"checks": checks, "stage_checks": stage_checks}


def _first_summary_divergence(query_result, batch_result):
    for query_stage, batch_stage in zip(
        query_result["stages"], batch_result["stages"]
    ):
        if query_stage["merged_summary"] != batch_stage["merged_summary"]:
            for query_block, batch_block in zip(
                query_stage["blocks"], batch_stage["blocks"]
            ):
                if query_block["summary"] != batch_block["summary"]:
                    return query_stage["stage"], query_block["block"]
            return query_stage["stage"], -1
    return -1, -1
