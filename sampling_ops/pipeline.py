"""Unified four-stage TopK/sampling pipeline and self-check integration."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .bias import adjudicate_bias, self_check
from .decision import decide_route
from .errors import BusinessError, fail
from .generator import make_rng
from .injection import InjectionController, STAGES
from .sampling import (
    ScaledCandidates,
    SampleResult,
    TopPResult,
    draw_samples,
    scale_candidates,
    truncate_top_p,
)
from .topk import TopKResult, topk, validate_k, verify_route_consistency
from .types import ROUTES, TaskSpec, normalize_tie_policy, resolve_dtype


CHUNK_REFERENCE_ROUTE = "full_sort_float64_reference"


def _positive_int_field(
    value: Any,
    *,
    field_name: str,
    code: str,
    case_id: str,
) -> int:
    if isinstance(value, bool):
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    if isinstance(value, float) and not value.is_integer():
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    if converted <= 0:
        fail(code, f"{field_name} must be positive", field=field_name, case_id=case_id)
    return converted


def _prepare_logits(
    params: Mapping[str, Any],
    spec: TaskSpec,
    *,
    batch_size: int,
    vocab_size: int,
    dtype: np.dtype,
    source_logits: Any | None,
) -> np.ndarray:
    if source_logits is not None:
        array = np.asarray(source_logits)
        if array.ndim == 1:
            if batch_size != 1 or array.size != vocab_size:
                fail(
                    "E_SHAPE_INVALID",
                    "1-D logits are only accepted for batch_size=1 and vocab_size elements",
                    field="logits",
                    case_id=spec.case_id,
                    details={
                        "actual_shape": list(array.shape),
                        "expected_shape": [batch_size, vocab_size],
                "fields": ["batch_size", "vocab_size", "logits"],
                    },
                )
            array = array.reshape(1, -1)
        if array.ndim != 2 or array.shape != (batch_size, vocab_size):
            fail(
                "E_SHAPE_INVALID",
                "logits shape does not match batch_size and vocab_size",
                field="logits",
                case_id=spec.case_id,
                details={
                    "actual_shape": list(array.shape),
                    "expected_shape": [batch_size, vocab_size],
                },
            )
        if not (
            np.issubdtype(array.dtype, np.floating)
            or np.issubdtype(array.dtype, np.integer)
        ):
            fail(
                "E_DTYPE_UNSUPPORTED",
                "logits must be a real numeric array",
                field="logits",
                case_id=spec.case_id,
                details={"actual_dtype": str(array.dtype)},
            )
        return array.astype(dtype, copy=True)

    seed_value = params.get("seed")
    try:
        generation_seed = int(seed_value)
    except (TypeError, ValueError, OverflowError):
        generation_seed = 0
    if isinstance(seed_value, bool) or seed_value is None:
        generation_seed = 0
    rng = make_rng(generation_seed)
    values = rng.standard_normal((batch_size, vocab_size), dtype=np.float64)
    quantum = spec.logits_quantum
    if quantum is not None:
        try:
            quantum_value = float(quantum)
        except (TypeError, ValueError, OverflowError):
            fail(
                "E_SHAPE_INVALID",
                "logits_quantum must be a finite positive number",
                field="logits_quantum",
                case_id=spec.case_id,
            )
        if not np.isfinite(quantum_value) or quantum_value <= 0:
            fail(
                "E_SHAPE_INVALID",
                "logits_quantum must be a finite positive number",
                field="logits_quantum",
                case_id=spec.case_id,
                details={"actual": quantum},
            )
        values = np.round(values / quantum_value) * quantum_value
    return values.astype(dtype, copy=False)


def _stage_state() -> dict[str, dict[str, Any]]:
    return {
        stage: {
            "stage": stage,
            "status": "pending",
            "business_code": None,
            "field": None,
            "injected": False,
            "detail": "not started",
        }
        for stage in STAGES
    }


def _error_record(
    error: BusinessError,
    *,
    stage: str,
    case_id: str,
    injected: bool,
    injection_ids: list[str] | None = None,
) -> dict[str, Any]:
    record = error.to_record(stage=stage, case_id=case_id, injected=injected)
    if injection_ids:
        record["injection_ids"] = injection_ids
    return record


def _internal_error(stage: str, case_id: str, error: Exception) -> BusinessError:
    return BusinessError(
        code="E_INTERNAL",
        message=f"unexpected {type(error).__name__}: {error}",
        field=None,
        case_id=case_id,
        stage=stage,
    )


def _selected_route_payload(
    selected_route: str | None,
    route_results: Mapping[str, TopKResult],
    reference: TopKResult | None,
    consistency: Mapping[str, Any] | None,
    tie_policy: str | None,
) -> dict[str, Any]:
    selected = route_results.get(selected_route) if selected_route else None
    return {
        "selected_route": selected_route,
        "tie_policy": tie_policy,
        "candidates": selected.to_dict() if selected is not None else None,
        "per_route": {route: result.to_dict() for route, result in route_results.items()},
        "reference_route": CHUNK_REFERENCE_ROUTE,
        "reference": (
            {
                "indices": reference.indices.tolist(),
                "scores": reference.scores.astype(np.float64, copy=False).tolist(),
            }
            if reference is not None
            else None
        ),
        "cross_route_consistency": dict(consistency) if consistency is not None else None,
    }


def _top_p_payload(truncated: TopPResult) -> dict[str, Any]:
    return {
        "top_p": float(truncated.top_p),
        "retained_counts": [int(value) for value in truncated.retained_counts],
        "dropped_counts": [int(value) for value in truncated.dropped_counts],
        "cutoff_cumulative": [float(value) for value in truncated.cutoff_cumulative],
        "retained": [
            {
                "row": row,
                "positions": positions.tolist(),
                "indices": indices.tolist(),
            }
            for row, (positions, indices) in enumerate(
                zip(truncated.retained_positions, truncated.retained_indices)
            )
        ],
        "boundary_rule": truncated.rule,
    }


def _sampling_payload(
    *,
    selected_route: str | None,
    params: Mapping[str, Any],
    scaled: Mapping[str, ScaledCandidates],
    truncated: Mapping[str, TopPResult],
    samples: Mapping[str, SampleResult],
) -> dict[str, Any]:
    selected_scaled = scaled.get(selected_route) if selected_route else None
    selected_truncated = truncated.get(selected_route) if selected_route else None
    selected_samples = samples.get(selected_route) if selected_route else None
    stage_outputs: dict[str, Any] = {}
    if selected_scaled is not None:
        stage_outputs["temperature"] = {
            "temperature": float(selected_scaled.temperature),
            "probabilities": selected_scaled.probabilities.tolist(),
            "cumulative_probabilities": selected_scaled.cumulative_probabilities.tolist(),
        }
    if selected_truncated is not None:
        stage_outputs["top_p"] = _top_p_payload(selected_truncated)
    return {
        "selected_route": selected_route,
        "parameters": {
            "seed": params.get("seed"),
            "temperature": params.get("temperature"),
            "top_p": params.get("top_p"),
            "group_size": params.get("group_size"),
        },
        "samples": selected_samples.samples.tolist() if selected_samples is not None else None,
        "random_values": (
            selected_samples.random_values.tolist() if selected_samples is not None else None
        ),
        "per_route": {route: result.to_dict() for route, result in samples.items()},
        "stage_outputs": stage_outputs,
        "cdf_equality_rule": (
            selected_samples.cdf_equality_rule if selected_samples is not None else None
        ),
    }


def run_task(
    task: Mapping[str, Any] | TaskSpec,
    logits: Any | None = None,
    *,
    run_all_routes: bool = True,
    extra_injections: list[Mapping[str, Any]] | None = None,
    fallback_case_id: str = "case",
) -> dict[str, Any]:
    """Run candidate, temperature, top-p, and sample stages for one task.

    Expected business failures are returned as structured records.  They are
    never converted into global exceptions or global random state.
    """

    try:
        return _run_task_impl(
            task,
            logits,
            run_all_routes=run_all_routes,
            extra_injections=extra_injections,
            fallback_case_id=fallback_case_id,
        )
    except Exception as error:  # Last-resort attribution for unexpected bugs.
        case_id = (
            task.case_id
            if isinstance(task, TaskSpec)
            else str(task.get("case_id", fallback_case_id))
        )
        business_error = (
            error
            if isinstance(error, BusinessError)
            else _internal_error("unattributed", case_id, error)
        )
        return {
            "schema_version": "1.0",
            "case_id": case_id,
            "status": "ERROR",
            "business_code": business_error.code,
            "business_codes": [business_error.code],
            "input": task.public_input() if isinstance(task, TaskSpec) else dict(task),
            "decision": None,
            "topk": None,
            "sampling": None,
            "conflict_resolution": None,
            "bias": None,
            "self_check": {"status": "skipped", "reason": "pipeline exception"},
            "stage_trace": list(_stage_state().values()),
            "injections": [],
            "errors": [],
            "unattributed_errors": [
                {
                    "case_id": case_id,
                    "stage": business_error.stage,
                    "business_code": business_error.code,
                    "field": business_error.field,
                    "message": business_error.message,
                }
            ],
            "warnings": [],
        }


def _run_task_impl(
    task: Mapping[str, Any] | TaskSpec,
    logits: Any | None,
    *,
    run_all_routes: bool,
    extra_injections: list[Mapping[str, Any]] | None,
    fallback_case_id: str,
) -> dict[str, Any]:
    spec = task if isinstance(task, TaskSpec) else TaskSpec.from_mapping(task, fallback_case_id=fallback_case_id)
    all_injections = [dict(item) for item in spec.injections]
    for item in extra_injections or []:
        all_injections.append(dict(item))
    controller = InjectionController(spec.case_id, all_injections)
    params: dict[str, Any] = {
        "dtype": spec.dtype,
        "vocab_size": spec.vocab_size,
        "batch_size": spec.batch_size,
        "k_value": spec.k_value,
        "k_mode": spec.k_mode,
        "temperature": spec.temperature,
        "top_p": spec.top_p,
        "seed": spec.seed,
        "group_size": spec.group_size,
        "tie_policy": spec.tie_policy,
        "resource_profile": spec.resource_profile,
    }
    stage_state = _stage_state()
    reached_stages: set[str] = set()
    errors: list[dict[str, Any]] = []
    unattributed_errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    decision: dict[str, Any] | None = None
    selected_route: str | None = None
    route_results: dict[str, TopKResult] = {}
    reference: TopKResult | None = None
    consistency: dict[str, Any] | None = None
    scaled: dict[str, ScaledCandidates] = {}
    truncated: dict[str, TopPResult] = {}
    samples: dict[str, SampleResult] = {}
    bias_report: dict[str, Any] | None = None
    self_check_report: dict[str, Any] | None = None
    normalized_tie: str | None = None
    resolved_dtype: np.dtype | None = None
    prepared_logits: np.ndarray | None = None

    def mark_ok(stage: str, detail: str) -> None:
        for injection_record in controller.stage_records(stage):
            if injection_record.get("business_code") is None:
                injection_record["business_code"] = "OK"
        stage_state[stage].update(
            {
                "status": "ok",
                "business_code": None,
                "field": None,
                "injected": controller.stage_injected(stage),
                "detail": detail,
            }
        )

    def mark_error(stage: str, error: Exception) -> BusinessError:
        business_error = error if isinstance(error, BusinessError) else _internal_error(stage, spec.case_id, error)
        injected = controller.stage_injected(stage)
        injection_ids = [record["injection_id"] for record in controller.stage_records(stage)]
        if injected:
            controller.mark_error(stage, business_error.code, business_error.field)
        stage_state[stage].update(
            {
                "status": "error",
                "business_code": business_error.code,
                "field": business_error.field,
                "injected": injected,
                "detail": business_error.message,
            }
        )
        errors.append(
            _error_record(
                business_error,
                stage=stage,
                case_id=spec.case_id,
                injected=injected,
                injection_ids=injection_ids,
            )
        )
        return business_error

    def mark_skipped(stage: str, reason: str) -> None:
        stage_state[stage].update(
            {
                "status": "skipped",
                "business_code": None,
                "field": None,
                "injected": False,
                "detail": reason,
            }
        )

    # ---- Stage 1: candidate filtering / route decision / exact TopK ----------
    reached_stages.add("candidate_filter")
    stage_ok = False
    try:
        controller.apply("candidate_filter", params, None)
        resolved_dtype = resolve_dtype(params.get("dtype"), case_id=spec.case_id)
        batch_size = _positive_int_field(
            params.get("batch_size"),
            field_name="batch_size",
            code="E_SHAPE_INVALID",
            case_id=spec.case_id,
        )
        vocab_size = _positive_int_field(
            params.get("vocab_size"),
            field_name="vocab_size",
            code="E_SHAPE_INVALID",
            case_id=spec.case_id,
        )
        validate_k(params.get("k_value"), vocab_size, case_id=spec.case_id)
        normalized_tie = normalize_tie_policy(params.get("tie_policy"), case_id=spec.case_id)
        decision = decide_route(
            vocab_size,
            batch_size,
            int(params["k_value"]),
            params.get("dtype"),
            k_mode=params.get("k_mode", "auto"),
            tie_policy=normalized_tie,
            resource_profile=params.get("resource_profile"),
            case_id=spec.case_id,
        )
        selected_route = str(decision["selected_route"])
        chunk_size = int(decision["observation"]["chunk_size"])
        prepared_logits = _prepare_logits(
            params,
            spec,
            batch_size=batch_size,
            vocab_size=vocab_size,
            dtype=resolved_dtype,
            source_logits=logits,
        )
        prepared_logits = controller.apply_logits(prepared_logits)
        route_names = list(ROUTES) if run_all_routes else [selected_route]
        for route_name in route_names:
            result = topk(
                prepared_logits,
                params.get("k_value"),
                route=route_name,
                tie_policy=normalized_tie,
                chunk_size=chunk_size,
                case_id=spec.case_id,
            )
            result.comparison_estimate = int(decision["estimates"][route_name]["comparisons"])
            result.metadata["peak_memory_estimate_bytes"] = int(
                decision["estimates"][route_name]["peak_memory_bytes"]
            )
            route_results[route_name] = result

        reference = topk(
            np.asarray(prepared_logits, dtype=np.float64),
            params.get("k_value"),
            route="full_sort",
            tie_policy="index_asc",
            case_id=spec.case_id,
        )
        consistency = verify_route_consistency(route_results, reference, case_id=spec.case_id)
        if not consistency["ok"]:
            failure = consistency["first_failure"]
            fail(
                "E_TOPK_INCONSISTENT",
                "TopK routes disagree on candidate membership or order",
                field="candidates",
                case_id=spec.case_id,
                details={"case_id": spec.case_id, "first_deviation": failure},
            )
        mark_ok(
            "candidate_filter",
            f"selected={selected_route}; routes_checked={','.join(route_results)}; exact candidates verified",
        )
        stage_ok = True
        if not decision["selected_within_limits"]:
            warnings.append("W_RESOURCE_OVERRUN")
    except Exception as error:
        mark_error("candidate_filter", error)

    if not stage_ok:
        for stage in STAGES[1:]:
            mark_skipped(stage, "upstream candidate_filter failure")

    # ---- Stage 2: temperature scaling ---------------------------------------
    if stage_ok:
        reached_stages.add("temperature_scale")
        try:
            controller.apply("temperature_scale", params, None)
            for route_name, result in route_results.items():
                scaled[route_name] = scale_candidates(
                    result.scores,
                    params.get("temperature"),
                    case_id=spec.case_id,
                )
            mark_ok(
                "temperature_scale",
                f"temperature={params.get('temperature')}; probabilities normalized in float64",
            )
        except Exception as error:
            mark_error("temperature_scale", error)
            stage_ok = False
            for stage in STAGES[2:]:
                mark_skipped(stage, "upstream temperature_scale failure")

    # ---- Stage 3: inclusive top-p truncation --------------------------------
    if stage_ok:
        reached_stages.add("top_p_filter")
        try:
            controller.apply("top_p_filter", params, None)
            for route_name, result in route_results.items():
                truncated[route_name] = truncate_top_p(
                    scaled[route_name],
                    params.get("top_p"),
                    result.indices,
                    case_id=spec.case_id,
                )
            mark_ok(
                "top_p_filter",
                f"top_p={params.get('top_p')}; inclusive cumulative boundary; TopK candidates preserved",
            )
        except Exception as error:
            mark_error("top_p_filter", error)
            stage_ok = False
            for stage in STAGES[3:]:
                mark_skipped(stage, "upstream top_p_filter failure")

    # ---- Stage 4: deterministic local-RNG sample ----------------------------
    if stage_ok:
        reached_stages.add("sample")
        try:
            controller.apply("sample", params, None)
            for route_name in route_results:
                samples[route_name] = draw_samples(
                    truncated[route_name],
                    seed=params.get("seed"),
                    group_size=params.get("group_size"),
                    case_id=spec.case_id,
                )
            mark_ok(
                "sample",
                "local PCG64; row-major draws; no global RNG state",
            )
        except Exception as error:
            mark_error("sample", error)
            stage_ok = False

    # ---- Reference adjudication and deterministic replay self-check ---------
    if stage_ok and reference is not None and run_all_routes:
        try:
            bias_report = adjudicate_bias(
                spec.case_id,
                prepared_logits,
                int(params["k_value"]),
                route_results,
                temperature=params.get("temperature"),
                top_p=params.get("top_p"),
                seed=params.get("seed"),
                group_size=params.get("group_size"),
                route_samples=samples,
                tolerance=spec.tolerance,
            )
            if bias_report["business_code"] != "OK":
                first = bias_report["violations"][0] if bias_report["violations"] else {}
                errors.append(
                    {
                        "case_id": spec.case_id,
                        "stage": "bias_adjudication",
                        "business_code": "E_TOLERANCE_EXCEEDED",
                        "field": "candidates",
                        "message": "float64 full-sort reference tolerance exceeded",
                        "injected": False,
                        "details": {"first_violation": first},
                    }
                )
            self_check_report = self_check(
                spec.case_id,
                route_results,
                samples,
                temperature=params.get("temperature"),
                top_p=params.get("top_p"),
                seed=params.get("seed"),
                group_size=params.get("group_size"),
                tolerance=spec.tolerance,
            )
            if self_check_report["business_code"] != "OK":
                errors.append(
                    {
                        "case_id": spec.case_id,
                        "stage": "self_check",
                        "business_code": "E_INCONSISTENT",
                        "field": "candidates_or_samples",
                        "message": "cross-route candidate or sample replay self-check failed",
                        "injected": False,
                        "details": {"first_failure": self_check_report.get("first_failure")},
                    }
                )
        except Exception as error:
            business_error = (
                error
                if isinstance(error, BusinessError)
                else _internal_error("bias_adjudication", spec.case_id, error)
            )
            errors.append(
                _error_record(
                    business_error,
                    stage="bias_adjudication",
                    case_id=spec.case_id,
                    injected=False,
                )
            )
            if not isinstance(error, BusinessError):
                unattributed_errors.append(
                    {
                        "case_id": spec.case_id,
                        "stage": "bias_adjudication",
                        "business_code": business_error.code,
                        "message": business_error.message,
                    }
                )
    elif not run_all_routes:
        bias_report = {"status": "skipped", "reason": "run_all_routes=false", "business_code": "OK"}
        self_check_report = {
            "status": "skipped",
            "reason": "run_all_routes=false",
            "business_code": "OK",
        }
    else:
        bias_report = {"status": "skipped", "reason": "upstream stage failure"}
        self_check_report = {"status": "skipped", "reason": "upstream stage failure"}

    injection_records = controller.finalize(reached_stages)
    business_codes = []
    for error in errors:
        code = error["business_code"]
        if code not in business_codes:
            business_codes.append(code)
    if not business_codes:
        business_codes = ["OK"]
    if errors:
        status = "INJECTED_ERROR" if all(error.get("injected") for error in errors) else "ERROR"
    else:
        status = "OK"

    route_conflicts = decision.get("conflict_resolution", []) if decision else []
    selected_truncated = truncated.get(selected_route) if selected_route else None
    if selected_truncated is not None:
        total_dropped = int(sum(selected_truncated.dropped_counts))
        top_p_conflict: dict[str, Any] = {
            "conflict": "top_p_quality_vs_candidate_retention",
            "chosen_policy": "inclusive_cumulative_top_p",
            "topk_candidate_output_preserved": True,
            "retained_counts": [int(value) for value in selected_truncated.retained_counts],
            "dropped_counts": [int(value) for value in selected_truncated.dropped_counts],
            "total_sampling_candidates_dropped": total_dropped,
            "degradation_category": "TOP_P_CANDIDATE_TRUNCATION" if total_dropped else None,
            "reason": (
                "TopK candidates remain available for audit; top-p only restricts the "
                "nucleus used by the sampling stage."
            ),
        }
        if total_dropped:
            warnings.append("W_TOP_P_TRUNCATION")
    else:
        top_p_conflict = None

    return {
        "schema_version": "1.0",
        "case_id": spec.case_id,
        "status": status,
        "business_code": business_codes[0],
        "business_codes": business_codes,
        "input": spec.public_input(),
        "decision": decision,
        "topk": _selected_route_payload(
            selected_route,
            route_results,
            reference,
            consistency,
            normalized_tie,
        ),
        "sampling": _sampling_payload(
            selected_route=selected_route,
            params=params,
            scaled=scaled,
            truncated=truncated,
            samples=samples,
        ),
        "conflict_resolution": {
            "route_conflicts": route_conflicts,
            "top_p_truncation": top_p_conflict,
        },
        "bias": bias_report,
        "self_check": self_check_report,
        "stage_trace": [stage_state[stage] for stage in STAGES],
        "injections": injection_records,
        "errors": errors,
        "unattributed_errors": unattributed_errors,
        "warnings": warnings,
    }

