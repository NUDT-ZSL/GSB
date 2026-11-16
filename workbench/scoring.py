from __future__ import annotations

import math
import re
from typing import Any

import numpy as np

from .config import DIMENSION_ORDER, InputBundle
from .errors import WorkbenchError
from .tokenizer import SubwordTokenizer


PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z0-9_.-]+\}")
DTYPES = {"float32": np.float32, "float64": np.float64}
PROPAGATION_STAGES = (
    "dimension_scoring",
    "length_penalty",
    "weight_multiplication",
    "weighted_sum",
)


class PrecisionMath:
    def __init__(self, precision: str) -> None:
        self.precision = precision
        self.dtype = DTYPES[precision]

    def cast(self, value: Any) -> np.generic:
        return self.dtype(value)

    def add(self, left: Any, right: Any) -> np.generic:
        return self.dtype(left + right)

    def sub(self, left: Any, right: Any) -> np.generic:
        return self.dtype(left - right)

    def mul(self, left: Any, right: Any) -> np.generic:
        return self.dtype(left * right)

    def div(self, left: Any, right: Any) -> np.generic:
        if right == 0:
            raise WorkbenchError("E_DIVISION_BY_ZERO", "division by zero", "scoring")
        return self.dtype(left / right)

    def clamp01(self, value: Any) -> np.generic:
        return self.dtype(max(self.cast(0), min(self.cast(1), value)))


def _rule_number(rules: dict[str, Any], name: str, default: float) -> float:
    value = rules.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkbenchError("E_INPUT_INVALID", f"scoring rule {name} must be numeric", "input")
    if not math.isfinite(float(value)):
        raise WorkbenchError("E_INPUT_INVALID", f"scoring rule {name} must be finite", "input")
    return float(value)


def _dimension_weights(candidate: dict[str, Any], scoring_params: dict[str, Any]) -> dict[str, float]:
    weights: dict[str, float] = {}
    overrides = (
        candidate.get("scoring_overrides", {}).get("dimension_weights", {})
        if isinstance(candidate.get("scoring_overrides", {}), dict)
        else {}
    )
    if not isinstance(overrides, dict):
        raise WorkbenchError(
            "E_OUT_OF_RANGE_WEIGHT",
            "scoring_overrides.dimension_weights must be an object",
            "scoring",
            candidate.get("sample_id"),
        )
    for dimension in DIMENSION_ORDER:
        value = overrides.get(dimension, scoring_params["dimensions"][dimension]["weight"])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkbenchError(
                "E_OUT_OF_RANGE_WEIGHT",
                f"weight for {dimension} must be numeric",
                "scoring",
                candidate.get("sample_id"),
            )
        numeric = float(value)
        if not math.isfinite(numeric) or not 0 <= numeric <= 1:
            raise WorkbenchError(
                "E_OUT_OF_RANGE_WEIGHT",
                f"weight for {dimension} must be in [0, 1], got {numeric!r}",
                "scoring",
                candidate.get("sample_id"),
            )
        weights[dimension] = numeric
    return weights


def _required_candidate_fields(candidate: dict[str, Any]) -> None:
    required = ("sample_id", "task_type", "instruction", "reference_answer")
    for field_name in required:
        if field_name not in candidate:
            raise WorkbenchError(
                "E_MISSING_FIELD",
                f"candidate is missing required field {field_name}",
                "scoring",
                candidate.get("sample_id"),
            )


def _length_penalty(
    math_impl: PrecisionMath,
    length: int,
    *,
    soft: float,
    hard: float,
    coefficient: float,
) -> tuple[np.generic, str]:
    dt = math_impl.cast
    length_value = dt(length)
    soft_value = dt(soft)
    hard_value = dt(hard)
    coefficient_value = dt(coefficient)
    if length_value <= soft_value:
        return dt(0), "free"
    if length_value <= hard_value:
        numerator = math_impl.sub(length_value, soft_value)
        denominator = math_impl.sub(hard_value, soft_value)
        fraction = math_impl.div(numerator, denominator)
        return math_impl.clamp01(math_impl.mul(coefficient_value, fraction)), "linear"
    numerator = math_impl.sub(length_value, hard_value)
    denominator = math_impl.sub(hard_value, soft_value)
    fraction = math_impl.div(numerator, denominator)
    remainder = math_impl.sub(dt(1), coefficient_value)
    return math_impl.clamp01(math_impl.add(coefficient_value, math_impl.mul(remainder, fraction))), "saturated"


def _jaccard_ratio(math_impl: PrecisionMath, intersection: int, union: int) -> np.generic:
    if union == 0:
        raise WorkbenchError("E_EMPTY_SUBWORD_SET", "Jaccard union is empty", "scoring")
    return math_impl.div(math_impl.cast(intersection), math_impl.cast(union))


def score_candidate_precision(
    bundle: InputBundle,
    candidate: dict[str, Any],
    tokenizer: SubwordTokenizer,
    precision: str,
    *,
    dimension_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    _required_candidate_fields(candidate)
    math_impl = PrecisionMath(precision)
    dt = math_impl.cast
    scoring_params = bundle.scoring_params
    rules = scoring_params["rules"]
    task_config = bundle.config["tasks"][candidate["task_type"]]

    instruction_subwords = (
        set()
        if candidate.get("injected_empty_subwords")
        else tokenizer.token_set(str(candidate["instruction"]))
    )
    reference_subwords = tokenizer.token_set(str(candidate["reference_answer"]))
    evidence_subwords = tokenizer.token_set(str(candidate.get("answer_evidence", "")))
    if not instruction_subwords:
        raise WorkbenchError(
            "E_EMPTY_SUBWORD_SET",
            "instruction produced an empty subword set",
            "scoring",
            candidate.get("sample_id"),
        )
    if not reference_subwords:
        raise WorkbenchError(
            "E_EMPTY_SUBWORD_SET",
            "reference answer produced an empty subword set",
            "scoring",
            candidate.get("sample_id"),
        )

    slot_values = candidate.get("slot_values", {})
    if not isinstance(slot_values, dict):
        raise WorkbenchError(
            "E_MISSING_FIELD", "slot_values must be an object", "scoring", candidate.get("sample_id")
        )
    placeholders = PLACEHOLDER_PATTERN.findall(str(candidate["instruction"]))
    slot_count = len(slot_values)
    if slot_count == 0:
        filled_ratio = dt(1)
    else:
        filled_slots = sum(1 for value in slot_values.values() if str(value).strip())
        filled_ratio = math_impl.div(dt(filled_slots), dt(slot_count))
    unresolved_ratio = dt(0) if not placeholders else dt(1)
    token_count = len(instruction_subwords)

    evidence_intersection = len(evidence_subwords.intersection(reference_subwords))
    evidence_union = len(evidence_subwords.union(reference_subwords))
    overlap = (
        dt(0)
        if evidence_union == 0
        else _jaccard_ratio(math_impl, evidence_intersection, evidence_union)
    )

    max_tokens = max(1, int(rules.get("length_feature_tokens", 12)))
    length_feature = math_impl.clamp01(
        math_impl.div(dt(token_count), dt(max_tokens))
    )
    answer_quality_weight = dt(task_config["answer_quality_weight"])

    base_scores: dict[str, np.generic] = {}
    probe_offsets = candidate.get("precision_probe_offsets", {})
    if not isinstance(probe_offsets, dict):
        raise WorkbenchError(
            "E_INPUT_INVALID",
            "precision_probe_offsets must be an object",
            "scoring",
            candidate.get("sample_id"),
        )

    def base_dimension(name: str) -> np.generic:
        value = dt(scoring_params["dimensions"][name]["baseline"])
        if name in probe_offsets:
            value = math_impl.add(value, dt(probe_offsets[name]))
        return value

    clarity = base_dimension("instruction_clarity")
    clarity = math_impl.add(
        clarity,
        math_impl.mul(dt(_rule_number(rules, "clarity_slot_bonus", 0.05)), filled_ratio),
    )
    clarity = math_impl.sub(
        clarity,
        math_impl.mul(
            dt(_rule_number(rules, "clarity_unresolved_penalty", 0.25)),
            unresolved_ratio,
        ),
    )
    clarity = math_impl.add(
        clarity,
        math_impl.mul(dt(_rule_number(rules, "clarity_length_bonus", 0.04)), length_feature),
    )
    base_scores["instruction_clarity"] = math_impl.clamp01(clarity)

    correctness = base_dimension("answer_correctness")
    correctness = math_impl.add(
        correctness,
        math_impl.mul(
            math_impl.mul(
                answer_quality_weight,
                dt(_rule_number(rules, "correctness_overlap_bonus", 0.35)),
            ),
            overlap,
        ),
    )
    base_scores["answer_correctness"] = math_impl.clamp01(correctness)

    format_score = base_dimension("format_compliance")
    format_score = math_impl.sub(
        format_score,
        math_impl.mul(
            dt(_rule_number(rules, "format_placeholder_penalty", 0.40)),
            unresolved_ratio,
        ),
    )
    if str(candidate["reference_answer"]).strip():
        format_score = math_impl.add(
            format_score, dt(_rule_number(rules, "format_answer_bonus", 0.03))
        )
    base_scores["format_compliance"] = math_impl.clamp01(format_score)
    base_scores["length_compliance"] = base_dimension("length_compliance")

    length = len(str(candidate["instruction"]))
    penalty, segment = _length_penalty(
        math_impl,
        length,
        soft=float(bundle.config["soft_length"]),
        hard=float(bundle.config["hard_length"]),
        coefficient=float(bundle.config["length_penalty_coefficient"]),
    )
    sensitivities = {
        "instruction_clarity": dt(_rule_number(rules, "penalty_sensitivity_clarity", 0.06)),
        "answer_correctness": dt(_rule_number(rules, "penalty_sensitivity_correctness", 0.08)),
        "format_compliance": dt(_rule_number(rules, "penalty_sensitivity_format", 0.05)),
        "length_compliance": dt(_rule_number(rules, "penalty_sensitivity_length", 1.0)),
    }
    dimension_scores: dict[str, np.generic] = {}
    weighted_components: dict[str, np.generic] = {}
    weights = dimension_weights or _dimension_weights(candidate, scoring_params)
    for dimension in DIMENSION_ORDER:
        adjusted = math_impl.sub(
            base_scores[dimension], math_impl.mul(penalty, sensitivities[dimension])
        )
        dimension_scores[dimension] = math_impl.clamp01(adjusted)
        weighted_components[dimension] = math_impl.mul(
            dimension_scores[dimension], dt(weights[dimension])
        )

    total_score = dt(0)
    for dimension in DIMENSION_ORDER:
        total_score = math_impl.add(total_score, weighted_components[dimension])

    return {
        "precision": precision,
        "length": length,
        "length_penalty": float(penalty),
        "length_penalty_segment": segment,
        "base_dimension_scores": {key: float(value) for key, value in base_scores.items()},
        "dimension_scores": {key: float(value) for key, value in dimension_scores.items()},
        "dimension_weights": {key: float(value) for key, value in weights.items()},
        "weighted_components": {key: float(value) for key, value in weighted_components.items()},
        "weighted_sum_order": list(DIMENSION_ORDER),
        "total_score": float(total_score),
        "subwords": sorted(instruction_subwords),
        "subword_count": len(instruction_subwords),
        "feature_values": {
            "filled_slot_ratio": float(filled_ratio),
            "unresolved_placeholder_ratio": float(unresolved_ratio),
            "evidence_reference_jaccard": float(overlap),
            "length_feature": float(length_feature),
        },
    }


def _difference(left: float, right: float) -> float:
    return abs(float(left) - float(right))


def score_candidate(
    bundle: InputBundle,
    candidate: dict[str, Any],
    tokenizer: SubwordTokenizer,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _required_candidate_fields(candidate)
    weights = _dimension_weights(candidate, bundle.scoring_params)
    fp32 = score_candidate_precision(
        bundle, candidate, tokenizer, "float32", dimension_weights=weights
    )
    fp64 = score_candidate_precision(
        bundle, candidate, tokenizer, "float64", dimension_weights=weights
    )

    total_delta = _difference(fp32["total_score"], fp64["total_score"])
    dimension_deltas = {
        key: _difference(fp32["dimension_scores"][key], fp64["dimension_scores"][key])
        for key in DIMENSION_ORDER
    }
    component_deltas = {
        key: _difference(fp32["weighted_components"][key], fp64["weighted_components"][key])
        for key in DIMENSION_ORDER
    }
    length_penalty_delta = _difference(fp32["length_penalty"], fp64["length_penalty"])
    threshold = bundle.precision_error_threshold
    stage_errors = {
        "dimension_scoring": max(dimension_deltas.values(), default=0.0),
        "length_penalty": length_penalty_delta,
        "weight_multiplication": max(component_deltas.values(), default=0.0),
        "weighted_sum": total_delta,
    }
    propagation = []
    running_max = 0.0
    first_over_threshold: str | None = None
    for stage in PROPAGATION_STAGES:
        stage_error = stage_errors[stage]
        running_max = max(running_max, stage_error)
        exceeded = stage_error > threshold
        if exceeded and first_over_threshold is None:
            first_over_threshold = stage
        propagation.append(
            {
                "stage": stage,
                "stage_error": stage_error,
                "cumulative_max_error": running_max,
                "exceeds_threshold": exceeded,
                "threshold": threshold,
            }
        )
    precision_sensitive = total_delta > threshold
    if not precision_sensitive:
        first_over_threshold = None
    elif first_over_threshold is None:
        first_over_threshold = "weighted_sum"

    boundary_diagnostics: list[dict[str, Any]] = []
    length = fp64["length"]
    for breakpoint_name, breakpoint in (
        ("soft_length", float(bundle.config["soft_length"])),
        ("hard_length", float(bundle.config["hard_length"])),
    ):
        if abs(float(length) - breakpoint) <= bundle.boundary_epsilon:
            triggered32 = fp32["length_penalty"] > 0.0
            triggered64 = fp64["length_penalty"] > 0.0
            diverged = triggered32 != triggered64
            item = {
                "diagnostic_id": f"len:{candidate['sample_id']}:{breakpoint_name}",
                "kind": "length_penalty_boundary",
                "candidate_ids": [candidate["sample_id"]],
                "sample_id": candidate["sample_id"],
                "breakpoint": breakpoint_name,
                "length": length,
                "distance": abs(float(length) - breakpoint),
                "fp32_value": fp32["length_penalty"],
                "fp64_reference_value": fp64["length_penalty"],
                "fp32_triggered": triggered32,
                "fp64_triggered": triggered64,
                "diverged": diverged,
                "decision_precision": "float64",
            }
            if diverged:
                item["business_code"] = "E_PRECISION_DIVERGENCE"
            boundary_diagnostics.append(item)

    score_report = {
        "sample_id": candidate["sample_id"],
        "task_type": candidate["task_type"],
        "stable_task_key": f"{candidate['sample_id']}|scoring",
        "primary_precision": bundle.precision_mode,
        "fp32": fp32,
        "fp64": fp64,
        "delta": {
            "dimension_scores": dimension_deltas,
            "length_penalty": length_penalty_delta,
            "weighted_components": component_deltas,
            "total_score": total_delta,
        },
        "error_propagation": propagation,
        "precision_sensitive": precision_sensitive,
        "first_over_threshold_stage": first_over_threshold,
        "threshold": threshold,
        "length_boundary_diagnostics": boundary_diagnostics,
        "injection_ids": [],
    }
    if bundle.precision_mode == "float32":
        score_report["primary_total_score"] = fp32["total_score"]
    else:
        score_report["primary_total_score"] = fp64["total_score"]
    return score_report, boundary_diagnostics
