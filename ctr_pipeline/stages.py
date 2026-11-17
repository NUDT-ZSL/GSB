"""Deterministic normalization, quantization, fusion, clipping, and tracing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .config import PipelineConfig
from .errors import (
    E_GROUP_UNKNOWN,
    E_INJECTION_INVALID,
    E_NON_FINITE,
    E_WIDTH_INVALID,
    E_WEIGHT_SHAPE,
    FailureRecord,
    PipelineError,
)
from .io_utils import clean_float, content_digest
from .models import InjectionSpec, ParamsPackage, RequestData


@dataclass
class ComputeOutcome:
    success: bool
    request_report: dict[str, Any] | None
    stage_artifacts: dict[str, dict[str, Any]]
    failure: FailureRecord | None
    injection_hits: list[str]


@dataclass
class _State:
    request: RequestData
    vectors: list[list[float]]
    normalized: list[list[float]] | None = None
    weights: list[float] | None = None
    low_width: int = 0
    high_width: int = 0
    hit_ids: list[str] | None = None


def _round_half_up(value: float, decimals: int) -> float:
    if not math.isfinite(value):
        raise PipelineError(E_NON_FINITE, "round", "cannot round a non-finite value")
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return clean_float(float(rounded))


def _quantize_value(value: float, scale: int) -> int:
    if not math.isfinite(value):
        raise PipelineError(E_NON_FINITE, "quantize", "cannot quantize a non-finite value")
    return int((Decimal(str(value)) * Decimal(scale)).to_integral_value(rounding=ROUND_HALF_UP))


def _validate_width(width: int, cfg: PipelineConfig, stage: str = "quantize") -> None:
    if isinstance(width, bool) or not isinstance(width, int) or width < 0 or width > cfg.max_width:
        raise PipelineError(
            E_WIDTH_INVALID,
            stage,
            f"fixed-point width {width!r} is outside [0, {cfg.max_width}]",
        )


def _candidate_index(state: _State, injection: InjectionSpec) -> int:
    if injection.candidate_id is None:
        return 0
    for index, candidate in enumerate(state.request.candidates):
        if candidate.candidate_id == injection.candidate_id:
            return index
    raise PipelineError(
        E_INJECTION_INVALID,
        injection.stage,
        f"injection {injection.injection_id!r} targets unknown candidate {injection.candidate_id!r}",
    )


def _apply_stage_injections(
    state: _State,
    stage: str,
    injections: Iterable[InjectionSpec],
) -> None:
    assert state.hit_ids is not None
    for injection in injections:
        if injection.request_id != state.request.request_id or injection.stage != stage:
            continue
        state.hit_ids.append(injection.injection_id)
        if injection.fault_type == "non_finite_feature":
            index = _candidate_index(state, injection)
            if stage == "normalize":
                assert state.normalized is None
                target = state.vectors[index]
            elif stage == "quantize":
                if state.normalized is None:
                    raise PipelineError(
                        E_INJECTION_INVALID,
                        stage,
                        "normalized data is unavailable for quantization injection",
                    )
                target = state.normalized[index]
            elif stage == "fusion":
                raise PipelineError(
                    E_NON_FINITE,
                    stage,
                    f"injected non-finite value reached fusion for candidate "
                    f"{state.request.candidates[index].candidate_id!r}",
                )
            else:
                raise PipelineError(
                    E_INJECTION_INVALID,
                    stage,
                    f"non_finite_feature is not supported at stage {stage!r}",
                )
            feature_index = injection.feature_index if injection.feature_index is not None else 0
            if feature_index < 0 or feature_index >= len(target):
                raise PipelineError(
                    E_INJECTION_INVALID,
                    stage,
                    f"injection {injection.injection_id!r} feature_index is outside vector range",
                )
            target[feature_index] = math.nan
        elif injection.fault_type == "weight_length_mismatch":
            if stage != "fusion":
                raise PipelineError(
                    E_INJECTION_INVALID,
                    stage,
                    "weight_length_mismatch is only supported at fusion",
                )
            if state.weights is None:
                raise PipelineError(
                    E_INJECTION_INVALID,
                    stage,
                    "weights are unavailable for fusion injection",
                )
            if state.weights:
                state.weights = state.weights[:-1]
        elif injection.fault_type == "width_invalid":
            if stage != "quantize":
                raise PipelineError(
                    E_INJECTION_INVALID,
                    stage,
                    "width_invalid is only supported at quantize",
                )
            state.low_width = injection.width if injection.width is not None else -1
        elif injection.fault_type == "unknown_feature_group":
            raise PipelineError(
                E_GROUP_UNKNOWN,
                stage,
                f"injected unknown feature group via {injection.injection_id!r}",
            )
        else:
            raise PipelineError(
                E_INJECTION_INVALID,
                stage,
                f"unsupported injection fault {injection.fault_type!r}",
            )


def _normalization_stats(
    vectors: list[list[float]],
    candidate_ids: list[str],
    cfg: PipelineConfig,
) -> tuple[list[list[float]], list[dict[str, Any]], list[list[int]]]:
    if not vectors:
        raise PipelineError("E_REQ_EMPTY", "normalize", "cannot normalize an empty request")
    dimension = len(vectors[0])
    for vector in vectors:
        if len(vector) != dimension:
            raise PipelineError(
                "E_SHAPE_MISMATCH", "normalize", "candidate vectors have inconsistent dimensions"
            )
        for value in vector:
            if not math.isfinite(value):
                raise PipelineError(E_NON_FINITE, "normalize", "normalization input is non-finite")

    normalized = [[0.0 for _ in range(dimension)] for _ in vectors]
    stats: list[dict[str, Any]] = []
    zero_features: list[list[int]] = [[] for _ in vectors]

    for feature_index in range(dimension):
        values = [vector[feature_index] for vector in vectors]
        if cfg.normalization == "minmax":
            minimum = min(values)
            maximum = max(values)
            denominator_zero = maximum == minimum
            denominator = maximum - minimum
            stat: dict[str, Any] = {
                "feature_index": feature_index,
                "method": "minmax",
                "min": clean_float(minimum),
                "max": clean_float(maximum),
                "denominator_zero": denominator_zero,
                "denominator_zero_candidates": list(candidate_ids) if denominator_zero else [],
            }
            for candidate_index, value in enumerate(values):
                if denominator_zero:
                    normalized[candidate_index][feature_index] = 0.0
                    zero_features[candidate_index].append(feature_index)
                else:
                    normalized[candidate_index][feature_index] = clean_float(
                        (value - minimum) / denominator
                    )
        else:
            count = len(values)
            mean = math.fsum(values) / count
            variance = math.fsum((value - mean) ** 2 for value in values) / count
            std = math.sqrt(max(0.0, variance))
            denominator_zero = std == 0.0
            stat = {
                "feature_index": feature_index,
                "method": "zscore",
                "mean": clean_float(mean),
                "std": clean_float(std),
                "denominator_zero": denominator_zero,
                "denominator_zero_candidates": list(candidate_ids) if denominator_zero else [],
            }
            for candidate_index, value in enumerate(values):
                if denominator_zero:
                    normalized[candidate_index][feature_index] = 0.0
                    zero_features[candidate_index].append(feature_index)
                else:
                    normalized[candidate_index][feature_index] = clean_float(
                        (value - mean) / std
                    )
        stats.append(stat)
    return normalized, stats, zero_features


def _quantize_matrix(
    normalized: list[list[float]],
    width: int,
    cfg: PipelineConfig,
) -> tuple[list[list[int]], int, str]:
    _validate_width(width, cfg)
    scale = cfg.quantization_base ** width
    fixed: list[list[int]] = []
    for row_index, row in enumerate(normalized):
        for value in row:
            if not math.isfinite(value):
                raise PipelineError(
                    E_NON_FINITE,
                    "quantize",
                    f"normalized feature row {row_index} contains a non-finite value",
                )
        fixed.append([_quantize_value(value, scale) for value in row])
    fingerprint = content_digest(
        {
            "width": width,
            "base": cfg.quantization_base,
            "scale": scale,
            "fixed_features": fixed,
        }
    )
    return fixed, scale, fingerprint


def _safe_injected_weights(
    weights: list[float],
    dimension: int,
    feature_names: tuple[str, ...],
) -> None:
    if len(weights) != dimension:
        raise PipelineError(
            E_WEIGHT_SHAPE,
            "fusion",
            f"weight length {len(weights)} does not match feature dimension {dimension}",
        )
    if len(feature_names) != dimension:
        raise PipelineError(
            E_WEIGHT_SHAPE,
            "fusion",
            "feature name length does not match feature dimension",
        )
    for index, weight in enumerate(weights):
        if not math.isfinite(weight):
            raise PipelineError(E_NON_FINITE, "fusion", f"weight {index} is non-finite")


def _fuse_width(
    candidate_ids: list[str],
    fixed: list[list[int]],
    width: int,
    cfg: PipelineConfig,
    weights: list[float],
    bias: float,
    groups: dict[str, tuple[int, ...]],
    tie_break_scores: list[float],
) -> dict[str, Any]:
    _validate_width(width, cfg, stage="fusion")
    scale = cfg.quantization_base ** width
    rows: list[dict[str, Any]] = []
    for candidate_index, candidate_id in enumerate(candidate_ids):
        quantized = fixed[candidate_index]
        dimension = len(quantized)
        if dimension != len(weights):
            raise PipelineError(
                E_WEIGHT_SHAPE,
                "fusion",
                f"fixed feature dimension {dimension} does not match weight length {len(weights)}",
            )
        weighted_sum = 0.0
        group_sums: dict[str, float] = {}
        terms: list[float] = []
        for feature_index in range(dimension):
            term = float(quantized[feature_index]) * weights[feature_index]
            if not math.isfinite(term):
                raise PipelineError(
                    E_NON_FINITE,
                    "fusion",
                    f"non-finite weighted term at feature {feature_index}",
                )
            terms.append(term)
            weighted_sum += term
        for group_name, indices in groups.items():
            group_sums[group_name] = math.fsum(terms[index] for index in indices)
        fusion_raw = (bias + weighted_sum / scale) / cfg.temperature
        fusion_score = _round_half_up(fusion_raw, cfg.score_decimals)
        clipped_value = min(max(fusion_score, cfg.clip_min), cfg.clip_max)
        clipped_score = _round_half_up(clipped_value, cfg.score_decimals)
        group_contributions = {
            group_name: _round_half_up(
                group_sums[group_name] / scale / cfg.temperature,
                cfg.score_decimals,
            )
            for group_name in groups
        }
        bias_contribution = _round_half_up(
            bias / cfg.temperature,
            cfg.score_decimals,
        )
        rows.append(
            {
                "candidate_id": candidate_id,
                "fixed_features": quantized,
                "weighted_sum": clean_float(weighted_sum),
                "fusion_score": fusion_score,
                "score": clipped_score,
                "clipped": clipped_score != fusion_score,
                "bias_contribution": bias_contribution,
                "group_contributions": group_contributions,
                "tie_break_recall_score": clean_float(tie_break_scores[candidate_index]),
                "rank": 0,
            }
        )
    ordered = sorted(
        rows,
        key=lambda item: (
            -float(item["score"]),
            -float(item["tie_break_recall_score"]),
            str(item["candidate_id"]),
        ),
    )
    for rank, item in enumerate(ordered, start=1):
        item["rank"] = rank
    ranking = [item["candidate_id"] for item in ordered]
    score_fingerprint = content_digest(
        {
            "width": width,
            "scale": scale,
            "ranking": ranking,
            "scores": [
                {
                    "candidate_id": item["candidate_id"],
                    "score": item["score"],
                    "fusion_score": item["fusion_score"],
                    "rank": item["rank"],
                    "group_contributions": item["group_contributions"],
                }
                for item in ordered
            ],
        }
    )
    return {
        "width": width,
        "scale": scale,
        "candidates": rows,
        "ranking": ranking,
        "score_fingerprint": score_fingerprint,
    }


def _trace_and_boundary(
    candidate_ids: list[str],
    low: dict[str, Any],
    high: dict[str, Any],
    cfg: PipelineConfig,
    normalized_low: list[list[float]],
    normalized_high: list[list[float]],
) -> dict[str, Any]:
    low_by_id = {item["candidate_id"]: item for item in low["candidates"]}
    high_by_id = {item["candidate_id"]: item for item in high["candidates"]}
    low_rank = {candidate_id: rank for rank, candidate_id in enumerate(low["ranking"], start=1)}
    high_rank = {candidate_id: rank for rank, candidate_id in enumerate(high["ranking"], start=1)}
    tolerance = cfg.trace_tolerances
    candidates: list[dict[str, Any]] = []
    stage_values: dict[str, list[float]] = {
        "normalization": [],
        "quantization": [],
        "fusion": [],
        "truncation": [],
    }

    for candidate_index, candidate_id in enumerate(candidate_ids):
        low_item = low_by_id[candidate_id]
        high_item = high_by_id[candidate_id]
        normalization_delta = max(
            (
                abs(normalized_low[candidate_index][feature_index] - normalized_high[candidate_index][feature_index])
                for feature_index in range(len(normalized_low[candidate_index]))
            ),
            default=0.0,
        )
        quantization_delta = max(
            (
                abs(
                    low_item["fixed_features"][feature_index] / low["scale"]
                    - high_item["fixed_features"][feature_index] / high["scale"]
                )
                for feature_index in range(len(low_item["fixed_features"]))
            ),
            default=0.0,
        )
        fusion_delta = abs(float(low_item["fusion_score"]) - float(high_item["fusion_score"]))
        truncation_delta = abs(float(low_item["score"]) - float(high_item["score"]))
        deltas = {
            "normalization": clean_float(normalization_delta),
            "quantization": clean_float(quantization_delta),
            "fusion": clean_float(fusion_delta),
            "truncation": clean_float(truncation_delta),
        }
        first_exceeded = None
        for stage in ("normalization", "quantization", "fusion", "truncation"):
            if deltas[stage] > tolerance[stage]:
                first_exceeded = stage
                break
        for stage, value in deltas.items():
            stage_values[stage].append(value)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "stage_deltas": deltas,
                "first_tolerance_exceeded": first_exceeded,
                "rank_configured": low_rank[candidate_id],
                "rank_high_width": high_rank[candidate_id],
                "rank_changed": low_rank[candidate_id] != high_rank[candidate_id],
            }
        )

    stage_summary: dict[str, Any] = {}
    for stage in ("normalization", "quantization", "fusion", "truncation"):
        values = stage_values[stage]
        stage_summary[stage] = {
            "max_abs_delta": clean_float(max(values, default=0.0)),
            "mean_abs_delta": clean_float(math.fsum(values) / len(values)) if values else 0.0,
            "tolerance": clean_float(tolerance[stage]),
            "exceeded_candidates": sum(1 for value in values if value > tolerance[stage]),
        }

    samples: dict[tuple[str, str], dict[str, Any]] = {}
    low_rank_to_id = {rank: candidate_id for candidate_id, rank in low_rank.items()}
    high_rank_to_id = {rank: candidate_id for candidate_id, rank in high_rank.items()}
    for rank in sorted(low_rank_to_id):
        candidate_id = low_rank_to_id[rank]
        counterpart = high_rank_to_id.get(rank)
        if counterpart is None or counterpart == candidate_id:
            continue
        pair = tuple(sorted((candidate_id, counterpart)))
        if pair in samples:
            continue
        high_winner = min(pair, key=lambda item: high_rank[item])
        contribution_items = high_by_id[high_winner]["group_contributions"]
        dominant_group = max(
            contribution_items,
            key=lambda group: (abs(float(contribution_items[group])), group),
            default=None,
        )
        first, second = pair
        first_groups = high_by_id[first]["group_contributions"]
        second_groups = high_by_id[second]["group_contributions"]
        delta_groups = {
            group: clean_float(
                abs(float(first_groups.get(group, 0.0)) - float(second_groups.get(group, 0.0)))
            )
            for group in sorted(set(first_groups) | set(second_groups))
        }
        flip_group = max(
            delta_groups,
            key=lambda group: (delta_groups[group], group),
            default=None,
        )
        samples[pair] = {
            "candidate_pair": list(pair),
            "configured_order": sorted(pair, key=lambda item: low_rank[item]),
            "high_width_order": sorted(pair, key=lambda item: high_rank[item]),
            "contribution_main_item": {
                "candidate_id": high_winner,
                "dominant_group": dominant_group,
                "contribution": contribution_items.get(dominant_group),
            },
            "flip_contributing_group": flip_group,
            "flip_group_delta": delta_groups.get(flip_group) if flip_group else None,
            "group_contribution_deltas": delta_groups,
            "group_contributions": {
                item: high_by_id[item]["group_contributions"] for item in pair
            },
        }

    return {
        "configured_width": cfg.width,
        "high_width": cfg.high_width,
        "tolerances": {key: clean_float(value) for key, value in tolerance.items()},
        "candidates": candidates,
        "stage_summary": stage_summary,
        "boundary_sensitive_samples": [samples[key] for key in sorted(samples)],
        "ranking_changed": low["ranking"] != high["ranking"],
    }


def _stage_fingerprints(
    normalize_payload: dict[str, Any],
    quantize_payload: dict[str, Any],
    fusion_payload: dict[str, Any],
    truncate_payload: dict[str, Any],
) -> dict[str, str]:
    return {
        "normalize": normalize_payload["fingerprint"],
        "quantize": content_digest(quantize_payload),
        "fusion": content_digest(fusion_payload),
        "truncate": content_digest(truncate_payload),
    }


def compute_request(
    request: RequestData,
    cfg: PipelineConfig,
    params: ParamsPackage,
    injections: Iterable[InjectionSpec],
) -> ComputeOutcome:
    injection_list = list(injections)
    state = _State(
        request=request,
        vectors=[list(candidate.vector) for candidate in request.candidates],
        weights=list(params.feature_weights),
        low_width=cfg.width,
        high_width=cfg.high_width,
        hit_ids=[],
    )
    current_stage = "normalize"
    try:
        _apply_stage_injections(state, "normalize", injection_list)
        current_stage = "normalize"
        normalized, stats, zero_features = _normalization_stats(
            state.vectors,
            [candidate.candidate_id for candidate in request.candidates],
            cfg,
        )
        state.normalized = normalized
        for row in normalized:
            for value in row:
                if not math.isfinite(value):
                    raise PipelineError(E_NON_FINITE, "normalize", "normalized value is non-finite")
        normalize_payload = {
            "request_id": request.request_id,
            "stage": "normalize",
            "method": cfg.normalization,
            "zero_denominator_policy": cfg.zero_denominator_policy,
            "stats": stats,
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "normalized_features": normalized[index],
                    "zero_denominator_features": zero_features[index],
                }
                for index, candidate in enumerate(request.candidates)
            ],
            "fingerprint": content_digest(
                {
                    "request_id": request.request_id,
                    "method": cfg.normalization,
                    "stats": stats,
                    "normalized": normalized,
                    "zero_features": zero_features,
                }
            ),
        }

        current_stage = "quantize"
        _apply_stage_injections(state, "quantize", injection_list)
        _validate_width(state.low_width, cfg)
        _validate_width(state.high_width, cfg)
        low_fixed, low_scale, low_quant_fp = _quantize_matrix(normalized, state.low_width, cfg)
        high_fixed, high_scale, high_quant_fp = _quantize_matrix(normalized, state.high_width, cfg)
        quantize_payload = {
            "request_id": request.request_id,
            "stage": "quantize",
            "configured": {
                "width": state.low_width,
                "scale": low_scale,
                "fixed_features": low_fixed,
                "fingerprint": low_quant_fp,
            },
            "high": {
                "width": state.high_width,
                "scale": high_scale,
                "fixed_features": high_fixed,
                "fingerprint": high_quant_fp,
            },
        }

        current_stage = "fusion"
        _apply_stage_injections(state, "fusion", injection_list)
        if state.weights is None:
            raise PipelineError(E_WEIGHT_SHAPE, "fusion", "weights are unavailable")
        dimension = len(state.vectors[0])
        _safe_injected_weights(state.weights, dimension, params.feature_names)
        tie_break_scores = [math.fsum(candidate.recall_scores) for candidate in request.candidates]
        candidate_ids = [candidate.candidate_id for candidate in request.candidates]
        low_fused = _fuse_width(
            candidate_ids,
            low_fixed,
            state.low_width,
            cfg,
            state.weights,
            params.bias,
            params.feature_groups,
            tie_break_scores,
        )
        high_fused = _fuse_width(
            candidate_ids,
            high_fixed,
            state.high_width,
            cfg,
            state.weights,
            params.bias,
            params.feature_groups,
            tie_break_scores,
        )
        fusion_payload = {
            "request_id": request.request_id,
            "stage": "fusion",
            "feature_weights": state.weights,
            "bias": params.bias,
            "temperature": cfg.temperature,
            "feature_groups": {key: list(value) for key, value in params.feature_groups.items()},
            "configured": low_fused,
            "high": high_fused,
        }

        current_stage = "truncate"
        _apply_stage_injections(state, "truncate", injection_list)
        trace = _trace_and_boundary(
            candidate_ids,
            low_fused,
            high_fused,
            cfg,
            normalized,
            normalized,
        )
        scores_by_id = {
            item["candidate_id"]: {
                "configured_score": item["fusion_score"],
                "configured_final_score": item["score"],
                "high_width_score": high_fused_by_id["fusion_score"],
                "high_width_final_score": high_fused_by_id["score"],
            }
            for item in low_fused["candidates"]
            for high_fused_by_id in [
                next(entry for entry in high_fused["candidates"] if entry["candidate_id"] == item["candidate_id"])
            ]
        }
        truncate_payload = {
            "request_id": request.request_id,
            "stage": "truncate",
            "clip_interval": [cfg.clip_min, cfg.clip_max],
            "configured": low_fused,
            "high": high_fused,
            "scores_by_candidate": scores_by_id,
            "trace": trace,
            "fingerprints": {
                "configured": low_fused["score_fingerprint"],
                "high": high_fused["score_fingerprint"],
            },
        }
        stage_fingerprints = _stage_fingerprints(
            normalize_payload,
            quantize_payload,
            fusion_payload,
            truncate_payload,
        )

        width_results = {}
        for width_label, width_data, width_width in (
            ("configured", low_fused, state.low_width),
            ("high", high_fused, state.high_width),
        ):
            candidates_with_normalization = []
            normalized_by_id = {
                candidate.candidate_id: normalized[index]
                for index, candidate in enumerate(request.candidates)
            }
            for item in width_data["candidates"]:
                enriched = dict(item)
                enriched["normalized_features"] = normalized_by_id[item["candidate_id"]]
                candidates_with_normalization.append(enriched)
            width_results[width_label] = {
                "width": width_width,
                "scale": width_data["scale"],
                "stage_fingerprints": stage_fingerprints,
                "normalization_stats": stats,
                "candidates": candidates_with_normalization,
                "ranking": width_data["ranking"],
                "score_fingerprint": width_data["score_fingerprint"],
            }

        request_report = {
            "request_id": request.request_id,
            "candidate_count": len(request.candidates),
            "source_lines": list(request.source_lines),
            "ranking": low_fused["ranking"],
            "width_results": width_results,
            "error_trace": trace,
            "stage_fingerprints": stage_fingerprints,
        }
        truncate_payload["report"] = request_report
        stage_artifacts = {
            "normalize": normalize_payload,
            "quantize": quantize_payload,
            "fusion": fusion_payload,
            "truncate": truncate_payload,
        }
        return ComputeOutcome(
            success=True,
            request_report=request_report,
            stage_artifacts=stage_artifacts,
            failure=None,
            injection_hits=sorted(state.hit_ids or []),
        )
    except PipelineError as exc:
        failure = FailureRecord(
            request_id=request.request_id,
            stage=exc.stage if exc.stage != "round" else current_stage,
            code=exc.code,
            exception_type=type(exc).__name__,
            message=exc.message,
            injection_ids=sorted(state.hit_ids or []),
            candidate_id=exc.candidate_id,
        )
        return ComputeOutcome(
            success=False,
            request_report=None,
            stage_artifacts={},
            failure=failure,
            injection_hits=sorted(state.hit_ids or []),
        )
    except Exception as exc:  # Defensive isolation: one request must not stop the pool.
        failure = FailureRecord(
            request_id=request.request_id,
            stage=current_stage,
            code="E_INTERNAL",
            exception_type=type(exc).__name__,
            message=str(exc),
            injection_ids=sorted(state.hit_ids or []),
        )
        return ComputeOutcome(
            success=False,
            request_report=None,
            stage_artifacts={},
            failure=failure,
            injection_hits=sorted(state.hit_ids or []),
        )
