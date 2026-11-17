"""Fixed-point projection, quantization, and unitization of speaker embeddings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .aggregation import AggregateResult
from .canonical import (
    derived_key,
    quantize_float_q,
    round_half_away_ratio,
    round_significant,
    sha256_obj,
    canonical_json_bytes,
)
from .config import Config
from .fixedpoint import (
    INT64_MAX,
    fixed_sqrt,
    fixed_square_norm,
    fixed_to_float,
    int64_overflow_risk,
    normalize_fixed_value,
    q_round_div,
    quantized_code,
    unitize_codes,
)

MATRIX_FRACTION_BITS = 8
_SPLITMIX_MASK = (1 << 64) - 1
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15


def _splitmix64(seed: int) -> Any:
    state = int(seed) & _SPLITMIX_MASK

    def next_value() -> int:
        nonlocal state
        state = (state + _SPLITMIX_GAMMA) & _SPLITMIX_MASK
        value = state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _SPLITMIX_MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _SPLITMIX_MASK
        return (value ^ (value >> 31)) & _SPLITMIX_MASK

    return next_value


def stat_normalisation_scales(config: Config) -> list[int]:
    # f0 mean, f0 variance, log-energy mean, log-energy variance,
    # cepstral means, cepstral variances.
    return [400, 40_000, 30, 100] + [20] * config.cepstral_order + [400] * config.cepstral_order


def projection_matrix(config: Config) -> list[list[int]]:
    stat_dim = 4 + 2 * config.cepstral_order
    seed_bytes = canonical_json_bytes(
        {
            "projection_hash": config.projection_hash(),
            "stat_dim": stat_dim,
            "embedding_dim": config.embedding_dim,
        }
    )
    import hashlib

    seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "little")
    random_value = _splitmix64(seed)
    span = 2 * config.projection_matrix_scale + 1
    matrix: list[list[int]] = []
    for _ in range(config.embedding_dim):
        row = [int(random_value() % span) - config.projection_matrix_scale for _ in range(stat_dim)]
        matrix.append(row)
    return matrix


def projection_matrix_fingerprint(matrix: list[list[int]]) -> str:
    return sha256_obj(matrix)


@dataclass(frozen=True)
class EmbeddingResult:
    sample: str
    bits: int
    vector_q: tuple[int, ...]
    level_codes: tuple[int, ...]
    projection_values_q: tuple[int, ...]
    residuals: tuple[float, ...]
    residual_abs_sum: float
    matrix_fingerprint: str
    fingerprint: str
    max_absolute_dot: int
    max_possible_absolute_dot: int
    overflow_risk: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "bits": self.bits,
            "vector_q": list(self.vector_q),
            "level_codes": list(self.level_codes),
            "projection_values_q": list(self.projection_values_q),
            "residuals": list(self.residuals),
            "residual_abs_sum": self.residual_abs_sum,
            "matrix_fingerprint": self.matrix_fingerprint,
            "embedding_fingerprint": self.fingerprint,
            "max_absolute_dot": self.max_absolute_dot,
            "max_possible_absolute_dot": self.max_possible_absolute_dot,
            "overflow_risk": self.overflow_risk,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EmbeddingResult":
        return cls(
            sample=payload["sample"],
            bits=int(payload["bits"]),
            vector_q=tuple(int(item) for item in payload["vector_q"]),
            level_codes=tuple(int(item) for item in payload["level_codes"]),
            projection_values_q=tuple(int(item) for item in payload["projection_values_q"]),
            residuals=tuple(float(item) for item in payload["residuals"]),
            residual_abs_sum=float(payload["residual_abs_sum"]),
            matrix_fingerprint=payload["matrix_fingerprint"],
            fingerprint=payload["embedding_fingerprint"],
            max_absolute_dot=int(payload["max_absolute_dot"]),
            max_possible_absolute_dot=int(payload["max_possible_absolute_dot"]),
            overflow_risk=bool(payload["overflow_risk"]),
        )

    def float_vector(self) -> list[float]:
        return [fixed_to_float(item, self.bits) for item in self.vector_q]

    def unit_norm_q16(self) -> int:
        return fixed_sqrt(fixed_square_norm(self.vector_q))


def embedding_cache_key(aggregate_key: str, config: Config, bits: int) -> str:
    return derived_key(
        "embedding/v3",
        aggregate_key,
        config.embedding_hash(),
        {
            "fractional_bits": bits,
            "quantization_levels": config.quantization_levels,
        },
    )


def build_embedding(aggregate: AggregateResult, bits: int, config: Config) -> EmbeddingResult:
    matrix = projection_matrix(config)
    scales = stat_normalisation_scales(config)
    if len(aggregate.stat_vector_q) != len(scales):
        raise ValueError("aggregate statistic vector dimension does not match configuration")
    normalized_input = [
        normalize_fixed_value(value, aggregate.bits, scales[index], bits)
        for index, value in enumerate(aggregate.stat_vector_q)
    ]
    max_input = max((abs(item) for item in normalized_input), default=0)
    half_levels = config.quantization_levels // 2
    clip_q = quantize_float_q(config.projection_clip, bits)
    level_codes: list[int] = []
    raw_values: list[int] = []
    residuals: list[float] = []
    max_dot = 0
    max_possible_dot = 0
    for row in matrix:
        dot = 0
        for matrix_value, input_value in zip(row, normalized_input):
            dot += matrix_value * input_value
        max_dot = max(max_dot, abs(dot))
        possible = sum(abs(value) for value in row) * max_input
        max_possible_dot = max(max_possible_dot, possible)
        raw_q = q_round_div(dot, 1 << MATRIX_FRACTION_BITS)
        code = quantized_code(raw_q, half_levels, clip_q)
        raw_values.append(raw_q)
        level_codes.append(code)
        raw_real = raw_q / float(1 << bits)
        code_real = 0.0 if half_levels == 0 else code * config.projection_clip / half_levels
        residual = raw_real - code_real
        residuals.append(round_significant(residual, config.feature_significant_digits))
    unit = unitize_codes(level_codes, bits)
    residual_abs_sum = round_significant(sum(abs(item) for item in residuals), config.feature_significant_digits)
    fingerprint_core = {
        "sample": aggregate.sample,
        "bits": bits,
        "aggregate_fingerprint": aggregate.fingerprint,
        "quantization_levels": config.quantization_levels,
        "projection_matrix_fingerprint": projection_matrix_fingerprint(matrix),
        "level_codes": level_codes,
        "vector_q": unit,
        "residuals": residuals,
    }
    return EmbeddingResult(
        sample=aggregate.sample,
        bits=bits,
        vector_q=tuple(unit),
        level_codes=tuple(level_codes),
        projection_values_q=tuple(raw_values),
        residuals=tuple(residuals),
        residual_abs_sum=residual_abs_sum,
        matrix_fingerprint=projection_matrix_fingerprint(matrix),
        fingerprint=sha256_obj(fingerprint_core),
        max_absolute_dot=max_dot,
        max_possible_absolute_dot=max_possible_dot,
        overflow_risk=int64_overflow_risk(max_possible_dot),
    )


def embedding_report(embedding: EmbeddingResult, config: Config) -> dict[str, Any]:
    digits = config.feature_significant_digits
    vector_float = embedding.float_vector()
    return {
        "dimension": len(vector_float),
        "fractional_bits": embedding.bits,
        "quantization_levels": config.quantization_levels,
        "vector": [round_significant(item, digits) for item in vector_float],
        "vector_q": list(embedding.vector_q),
        "level_codes": list(embedding.level_codes),
        "level_code_min": min(embedding.level_codes) if embedding.level_codes else 0,
        "level_code_max": max(embedding.level_codes) if embedding.level_codes else 0,
        "projection_integer_min": min(embedding.projection_values_q) if embedding.projection_values_q else 0,
        "projection_integer_max": max(embedding.projection_values_q) if embedding.projection_values_q else 0,
        "residuals": list(embedding.residuals),
        "residual_abs_sum": embedding.residual_abs_sum,
        "unit_norm_q16": embedding.unit_norm_q16(),
        "unit_norm": round_significant(embedding.unit_norm_q16() / float(1 << 16), digits),
        "matrix_fingerprint": embedding.matrix_fingerprint,
        "embedding_fingerprint": embedding.fingerprint,
        "max_absolute_dot": embedding.max_absolute_dot,
        "max_possible_absolute_dot": embedding.max_possible_absolute_dot,
        "int64_max": INT64_MAX,
        "overflow_risk": embedding.overflow_risk,
    }
