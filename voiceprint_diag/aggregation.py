"""Deterministic fixed-point speaker-level statistics aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .canonical import (
    derived_key,
    round_half_away_ratio,
    round_significant,
    sha256_obj,
)
from .config import Config
from .features import FeatureSet, quantized_vector


class AggregationError(ValueError):
    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AggregateResult:
    sample: str
    bits: int
    valid_frame_indices: tuple[int, ...]
    valid_count: int
    statistics: dict[str, Any]
    stat_vector_q: tuple[int, ...]
    fingerprint: str
    max_absolute_accumulator: int
    overflow_risk: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "bits": self.bits,
            "valid_frame_indices": list(self.valid_frame_indices),
            "valid_count": self.valid_count,
            "statistics": self.statistics,
            "stat_vector_q": list(self.stat_vector_q),
            "aggregate_fingerprint": self.fingerprint,
            "max_absolute_accumulator": self.max_absolute_accumulator,
            "overflow_risk": self.overflow_risk,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AggregateResult":
        return cls(
            sample=payload["sample"],
            bits=int(payload["bits"]),
            valid_frame_indices=tuple(int(item) for item in payload["valid_frame_indices"]),
            valid_count=int(payload["valid_count"]),
            statistics=payload["statistics"],
            stat_vector_q=tuple(int(item) for item in payload["stat_vector_q"]),
            fingerprint=payload["aggregate_fingerprint"],
            max_absolute_accumulator=int(payload["max_absolute_accumulator"]),
            overflow_risk=bool(payload["overflow_risk"]),
        )

    def frame_range(self) -> list[int] | None:
        indices = self.valid_frame_indices
        if not indices:
            return None
        return [indices[0], indices[-1]]


def aggregation_cache_key(feature_key: str, config: Config, bits: int) -> str:
    return derived_key("aggregation/v3", feature_key, config.aggregation_hash(), {"fractional_bits": bits})


def percentile_type7(sorted_values: list[int], probability: Fraction) -> int:
    """Exact type-7 linear interpolation on integer Q values."""
    count = len(sorted_values)
    if count == 0:
        raise ValueError("cannot take a percentile of an empty sequence")
    if count == 1:
        return sorted_values[0]
    if probability <= 0:
        return sorted_values[0]
    if probability >= 1:
        return sorted_values[-1]
    position = (count - 1) * probability
    lower_index = position.numerator // position.denominator
    remainder = position - lower_index
    if remainder == 0:
        return sorted_values[lower_index]
    lower = sorted_values[lower_index]
    upper = sorted_values[lower_index + 1]
    return lower + round_half_away_ratio((upper - lower) * remainder.numerator, remainder.denominator)


def _fixed_column_statistics(values: list[int], bits: int, probabilities: tuple[Fraction, ...]) -> tuple[dict[str, Any], tuple[int, int, tuple[int, ...]]]:
    count = len(values)
    if count == 0:
        raise ValueError("no values")
    total = 0
    for value in values:
        total += int(value)
    mean_q = round_half_away_ratio(total, count)
    sum_square = 0
    for value in values:
        difference = int(value) - mean_q
        sum_square += difference * difference
    variance_q = round_half_away_ratio(sum_square, count * (1 << bits))
    ordered = sorted(int(item) for item in values)
    quantiles = tuple(percentile_type7(ordered, probability) for probability in probabilities)
    payload = {
        "count": count,
        "mean_q": mean_q,
        "variance_q": variance_q,
        "quantiles_q": list(quantiles),
    }
    return payload, (mean_q, variance_q, quantiles)


def aggregate_features(features: FeatureSet, bits: int, config: Config) -> AggregateResult:
    q_vectors = [quantized_vector(frame, bits) for frame in features.frames]
    valid_indices = tuple(
        index
        for index, frame in enumerate(features.frames)
        if float(frame["log_energy"]) >= config.silence_energy_threshold
    )
    if len(valid_indices) < config.min_valid_frames:
        raise AggregationError(
            "E_INSUFFICIENT_FRAMES",
            "insufficient frames above the energy threshold",
            detail={
                "sample": features.sample,
                "valid_frames": len(valid_indices),
                "minimum_required": config.min_valid_frames,
                "threshold": config.silence_energy_threshold,
            },
        )
    selected = [q_vectors[index] for index in valid_indices]
    # Vector order: energy, zcr, f0, c1..cN. Statistics are reported for f0,
    # log energy and cepstra; zcr remains available in the feature artifact.
    columns: dict[str, list[int]] = {
        "log_energy": [row[0] for row in selected],
        "f0_hz": [row[2] for row in selected],
    }
    for coefficient in range(config.cepstral_order):
        columns[f"cepstral_{coefficient + 1}"] = [row[3 + coefficient] for row in selected]
    probabilities = config.quantile_fractions
    statistics: dict[str, Any] = {}
    stat_vector: list[int] = []
    max_abs_accumulator = 0
    for name, values in columns.items():
        payload, fixed = _fixed_column_statistics(values, bits, probabilities)
        total_abs = sum(abs(int(item)) for item in values)
        max_abs_accumulator = max(max_abs_accumulator, total_abs)
        mean_q, variance_q, quantiles = fixed
        statistics[name] = {
            "count": payload["count"],
            "mean": round_significant(mean_q / float(1 << bits), config.feature_significant_digits),
            "variance": round_significant(variance_q / float(1 << bits), config.feature_significant_digits),
            "quantiles": {
                f"p{int(round(probability * 100)):02d}": round_significant(
                    quantiles[index] / float(1 << bits), config.feature_significant_digits
                )
                for index, probability in enumerate(config.quantile_probabilities)
            },
            "mean_q": mean_q,
            "variance_q": variance_q,
            "quantiles_q": list(quantiles),
        }
        if name == "f0_hz":
            stat_vector.extend([mean_q, variance_q])
    stat_vector.extend([statistics["log_energy"]["mean_q"], statistics["log_energy"]["variance_q"]])
    for coefficient in range(1, config.cepstral_order + 1):
        key = f"cepstral_{coefficient}"
        stat_vector.append(int(statistics[key]["mean_q"]))
    for coefficient in range(1, config.cepstral_order + 1):
        key = f"cepstral_{coefficient}"
        stat_vector.append(int(statistics[key]["variance_q"]))
    fingerprint_core = {
        "sample": features.sample,
        "bits": bits,
        "valid_frame_indices": list(valid_indices),
        "statistics": statistics,
        "stat_vector_q": stat_vector,
        "interpolation": "linear_type7",
    }
    return AggregateResult(
        sample=features.sample,
        bits=bits,
        valid_frame_indices=valid_indices,
        valid_count=len(valid_indices),
        statistics=statistics,
        stat_vector_q=tuple(stat_vector),
        fingerprint=sha256_obj(fingerprint_core),
        max_absolute_accumulator=max_abs_accumulator,
        overflow_risk=max_abs_accumulator > ((1 << 63) - 1),
    )
