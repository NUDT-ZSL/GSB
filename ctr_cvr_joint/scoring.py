"""Joint CTR/CVR scoring with independently replayable contribution traces."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

from .assembly import AssemblyResult
from .errors import E_FORMAT_INVALID, fail
from .hashing import sha256_obj
from .models import FeatureSpec, ThresholdConfig
from .statistics import LoadedStatistics


SCORING_PROTOCOL = "ctr_cvr_joint_logit_v1"
LOGIT_CLAMP = 60.0
MIN_STABLE_PROBABILITY = 1e-15


@dataclass(frozen=True)
class ScoreResult:
    rows: tuple[dict[str, Any], ...]
    score_hash: str
    probability_hash: str
    statistics_version: int
    statistics_checksum: str
    scoring_config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_hash": self.score_hash,
            "probability_hash": self.probability_hash,
            "statistics_version": self.statistics_version,
            "statistics_checksum": self.statistics_checksum,
            "scoring_config": self.scoring_config,
            "rows": list(self.rows),
        }


def score_samples(
    assembly: AssemblyResult,
    stats: LoadedStatistics,
    spec: FeatureSpec,
    threshold: ThresholdConfig,
    *,
    joint_click_scale: float = 0.25,
) -> ScoreResult:
    if not math.isfinite(joint_click_scale) or not 0.0 <= joint_click_scale <= 1.0:
        fail(
            E_FORMAT_INVALID,
            "joint_click_scale must be finite and in [0, 1]",
            field="joint_click_scale",
        )
    rows: list[dict[str, Any]] = []
    probability_rows: list[dict[str, Any]] = []
    for assembled in assembly.rows:
        click = _score_metric(
            assembled,
            spec,
            stats,
            threshold,
            metric="ctr",
            probability_threshold=threshold.click_threshold,
            joint_logit=None,
        )
        convert = _score_metric(
            assembled,
            spec,
            stats,
            threshold,
            metric="cvr",
            probability_threshold=threshold.cvr_threshold,
            joint_logit=(
                click["logit_before_clamp"],
                joint_click_scale,
                "click_logit",
            ),
        )
        expected_conversion = _quantize_probability(
            click["probability"] * convert["probability"], threshold.stable_digits
        )
        probability_payload = {
            "click": click["probability"],
            "convert": convert["probability"],
            "expected_conversion": expected_conversion,
        }
        probability_hash = sha256_obj(probability_payload)
        probability_rows.append(probability_payload)
        row = {
            "request_id": assembled.request_id,
            "timestamp_ms": assembled.timestamp_ms,
            "protocol": SCORING_PROTOCOL,
            "threshold_digits": int(threshold.stable_digits),
            "click": click,
            "convert": convert,
            "expected_conversion": expected_conversion,
            "probability_hash": probability_hash,
        }
        row["score_hash"] = sha256_obj(row)
        rows.append(row)
    score_hash = sha256_obj(rows)
    probability_hash = sha256_obj(probability_rows)
    config = {
        "protocol": SCORING_PROTOCOL,
        "joint_click_scale": float(joint_click_scale),
        "threshold": threshold.to_dict(),
    }
    return ScoreResult(
        rows=tuple(rows),
        score_hash=score_hash,
        probability_hash=probability_hash,
        statistics_version=stats.version,
        statistics_checksum=stats.checksum,
        scoring_config=config,
    )


def _score_metric(
    assembled: Any,
    spec: FeatureSpec,
    stats: LoadedStatistics,
    threshold: ThresholdConfig,
    *,
    metric: str,
    probability_threshold: float,
    joint_logit: tuple[float, float, str] | None,
) -> dict[str, Any]:
    prior = stats.global_rate(metric)
    global_smoothing = stats.global_smoothing(metric)
    base_logit = _logit(prior)
    rate_key = "click_rate" if metric == "ctr" else "convert_rate"
    contributions: list[dict[str, Any]] = []
    numeric_map = spec.numeric_map()
    categorical_map = spec.categorical_map()
    cross_map = spec.cross_map()
    for detail in assembled.details:
        kind = detail["kind"]
        if kind == "numeric":
            name = detail["feature"].split(".", 1)[1]
            item = numeric_map[name]
            entry = stats.numeric_entry(name)
            rate = _entry_rate(entry, rate_key, prior)
            count = _entry_count(entry)
            smoothed = _smoothed_rate(rate, count, prior, global_smoothing)
            delta = _logit(smoothed) - base_logit
            contribution = item.weight * float(detail["encoded_value"]) * delta
            contributions.append(
                {
                    "feature": detail["feature"],
                    "kind": "numeric",
                    "encoded_value": float(detail["encoded_value"]),
                    "weight": item.weight,
                    "rate": rate,
                    "smoothed_rate": smoothed,
                    "count": count,
                    "delta_logit": delta,
                    "contribution": _clean(contribution),
                }
            )
        elif kind == "categorical":
            name = detail["feature"].split(".", 1)[1]
            item = categorical_map[name]
            entry = stats.categorical_entry(name, str(detail["raw_value"]))
            rate = _entry_rate(entry, rate_key, prior)
            count = _entry_count(entry)
            smoothed = _smoothed_rate(rate, count, prior, global_smoothing)
            delta = _logit(smoothed) - base_logit
            sign = 2.0 * float(detail["encoded_value"]) - 1.0
            contribution = item.weight * sign * delta
            contributions.append(
                {
                    "feature": detail["feature"],
                    "kind": "categorical",
                    "encoded_value": float(detail["encoded_value"]),
                    "bucket": int(detail.get("bucket", 0)),
                    "weight": item.weight,
                    "rate": rate,
                    "smoothed_rate": smoothed,
                    "count": count,
                    "delta_logit": delta,
                    "contribution": _clean(contribution),
                }
            )
        else:
            parts = detail["feature"].split(".")
            name = parts[1]
            item = cross_map[name]
            entry = stats.cross_entry(name)
            rate = _entry_rate(entry, rate_key, prior)
            count = _entry_count(entry)
            smoothed = _smoothed_rate(rate, count, prior, global_smoothing)
            delta = _logit(smoothed) - base_logit
            slot_weight = item.weight / max(1, len(item.slots))
            contribution = slot_weight * float(detail["encoded_value"]) * delta
            contributions.append(
                {
                    "feature": detail["feature"],
                    "kind": "cross",
                    "slot": parts[2],
                    "encoded_value": float(detail["encoded_value"]),
                    "weight": slot_weight,
                    "rate": rate,
                    "smoothed_rate": smoothed,
                    "count": count,
                    "delta_logit": delta,
                    "contribution": _clean(contribution),
                }
            )
    contributions.sort(key=lambda item: item["feature"])
    if joint_logit is not None:
        joint_input, scale, name = joint_logit
        contributions.append(
            {
                "feature": name,
                "kind": "joint",
                "encoded_value": joint_input,
                "weight": scale,
                "contribution": _clean(joint_input * scale),
                "source": "ctr_logit_before_clamp",
            }
        )
    before_clamp = math.fsum(
        [base_logit] + [float(item["contribution"]) for item in contributions]
    )
    clamped_logit = max(-LOGIT_CLAMP, min(LOGIT_CLAMP, before_clamp))
    raw_probability = _sigmoid(clamped_logit)
    probability, protection = _protect_probability(
        raw_probability, threshold.stable_digits
    )
    decision = compare_probability(
        probability,
        probability_threshold,
        policy=threshold.policy,
        tolerance=threshold.tolerance,
        stable_digits=threshold.stable_digits,
    )
    return {
        "metric": metric,
        "base_probability": prior,
        "base_logit": base_logit,
        "contributions": contributions,
        "logit_before_clamp": before_clamp,
        "logit": clamped_logit,
        "raw_probability": raw_probability,
        "probability": probability,
        "expected_conversion": None,
        "probability_protection": protection,
        "threshold": float(probability_threshold),
        "threshold_policy": threshold.policy,
        "tolerance": float(threshold.tolerance),
        "decision": decision["decision"],
        "comparison": decision["comparison"],
        "decision_margin": decision["margin"],
        "threshold_quantized": decision["threshold_quantized"],
    }


def _entry_rate(entry: dict[str, Any], key: str, fallback: float) -> float:
    value = entry.get(key)
    if value is None:
        return fallback
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(rate) or not 0.0 < rate < 1.0:
        return fallback
    return rate


def _entry_count(entry: dict[str, Any]) -> float:
    value = entry.get("count", 0.0)
    try:
        count = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(count) or count < 0.0:
        return 0.0
    return count


def _smoothed_rate(
    rate: float, count: float, prior: float, smoothing: float
) -> float:
    denominator = count + smoothing
    if denominator <= 0.0:
        return prior
    value = (rate * count + prior * smoothing) / denominator
    return min(max(value, 1e-15), 1.0 - 1e-15)


def _logit(probability: float) -> float:
    bounded = min(max(float(probability), 1e-15), 1.0 - 1e-15)
    return math.log(bounded / (1.0 - bounded))


def _sigmoid(logit: float) -> float:
    if logit >= 0.0:
        return 1.0 / (1.0 + math.exp(-logit))
    exponential = math.exp(logit)
    return exponential / (1.0 + exponential)


def _protect_probability(
    probability: float, stable_digits: int
) -> tuple[float, dict[str, Any]]:
    minimum = max(MIN_STABLE_PROBABILITY, 10.0 ** (-int(stable_digits)))
    protection: dict[str, Any] = {
        "subnormal_floor": minimum,
        "subnormal_ceiling": 1.0 - minimum,
        "applied": None,
    }
    value = float(probability)
    if value <= 0.0 or value < minimum:
        value = minimum
        protection["applied"] = "floor"
    elif value >= 1.0 or value > 1.0 - minimum:
        value = 1.0 - minimum
        protection["applied"] = "ceiling"
    quantized = _quantize_probability(value, stable_digits)
    if quantized <= 0.0:
        quantized = minimum
    elif quantized >= 1.0:
        quantized = 1.0 - minimum
    return quantized, protection


def _quantize_probability(value: float, stable_digits: int) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        fail(
            E_FORMAT_INVALID,
            "probability must be finite and in [0, 1]",
            field="probability",
        )
    quantum = Decimal(1).scaleb(-int(stable_digits))
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return float(rounded)


def compare_probability(
    probability: float,
    threshold: float,
    *,
    policy: str,
    tolerance: float,
    stable_digits: int,
) -> dict[str, Any]:
    config = ThresholdConfig(
        policy=policy,
        click_threshold=threshold,
        cvr_threshold=threshold,
        tolerance=tolerance,
        stable_digits=stable_digits,
    )
    quantum = Decimal(1).scaleb(-int(config.stable_digits))
    probability_decimal = Decimal(str(_quantize_probability(probability, config.stable_digits)))
    threshold_decimal = Decimal(str(_quantize_probability(threshold, config.stable_digits)))
    delta = probability_decimal - threshold_decimal
    if config.policy == "strict":
        decision = probability_decimal > threshold_decimal
        comparison = "above" if decision else "at_or_below"
    else:
        tolerance_decimal = Decimal(str(config.tolerance)).quantize(
            quantum, rounding=ROUND_HALF_EVEN
        )
        if delta > tolerance_decimal:
            decision = True
            comparison = "above_tolerance"
        elif delta < -tolerance_decimal:
            decision = False
            comparison = "below_tolerance"
        else:
            # A deterministic tie rule in the tolerance band prevents platform
            # rounding from flipping the business decision.
            decision = probability_decimal >= threshold_decimal
            comparison = (
                "within_tolerance_ge"
                if decision
                else "within_tolerance_lt"
            )
    return {
        "decision": bool(decision),
        "comparison": comparison,
        "margin": float(delta),
        "probability_quantized": float(probability_decimal),
        "threshold_quantized": float(threshold_decimal),
    }


def recompute_score_row(row: dict[str, Any]) -> dict[str, Any]:
    """Recompute both probabilities solely from a persisted trace."""

    result: dict[str, Any] = {}
    click_logit = 0.0
    for metric in ("click", "convert"):
        block = row[metric]
        terms = [float(block["base_logit"])] + [
            float(item["contribution"]) for item in block["contributions"]
        ]
        before_clamp = math.fsum(terms)
        clamped = max(-LOGIT_CLAMP, min(LOGIT_CLAMP, before_clamp))
        probability, _ = _protect_probability(
            _sigmoid(clamped), int(row["threshold_digits"])
        )
        if metric == "click":
            click_logit = before_clamp
        result[metric] = {
            "probability": probability,
            "logit_before_clamp": before_clamp,
            "logit": clamped,
        }
    expected = _quantize_probability(
        result["click"]["probability"] * result["convert"]["probability"],
        int(row["threshold_digits"]),
    )
    result["expected_conversion"] = expected
    return result


def _clean(value: float) -> float:
    if not math.isfinite(value):
        fail(
            E_FORMAT_INVALID,
            "score contribution is not finite",
            field="contribution",
        )
    return 0.0 if value == 0.0 else float(value)


__all__ = [
    "SCORING_PROTOCOL",
    "ScoreResult",
    "score_samples",
    "compare_probability",
    "recompute_score_row",
]

