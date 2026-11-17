"""Configuration model and phase-specific canonical fingerprints."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .canonical import read_json, sha256_obj


class ConfigError(ValueError):
    pass


_ALIASES = {
    "frame_length_ms": "frame_ms",
    "frame_shift_ms": "hop_ms",
    "hop_length_ms": "hop_ms",
    "n_mels": "mel_bands",
    "mel_max_hz": "mel_max_freq",
    "n_cepstra": "cepstral_order",
    "num_cepstral": "cepstral_order",
    "embedding_dims": "embedding_dim",
    "quant_levels": "quantization_levels",
    "quantization_steps": "quantization_levels",
    "fixed_fraction_bits": "fractional_bits",
    "q_fraction_bits": "fractional_bits",
    "fixed_bits": "fractional_bits",
    "threshold": "similarity_threshold",
    "similarity_decision_threshold": "similarity_threshold",
    "min_frames": "min_valid_frames",
    "max_workers": "concurrency",
    "snapshot_commit_interval": "snapshot_interval",
    "silence_threshold": "silence_energy_threshold",
    "feature_digits": "feature_significant_digits",
    "tolerance": "precision_tolerance",
}


def _coerce_float(name: str, value: Any) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a finite number") from exc
    if not math.isfinite(converted):
        raise ConfigError(f"{name} must be finite")
    return converted


def _coerce_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if converted != value and not (isinstance(value, str) and value.strip().isdigit()):
        raise ConfigError(f"{name} must be an integer")
    return converted


@dataclass(frozen=True)
class Config:
    frame_ms: float = 25.0
    hop_ms: float = 10.0
    mel_bands: int = 26
    mel_max_freq: float = 7600.0
    cepstral_order: int = 12
    embedding_dim: int = 16
    quantization_levels: int = 256
    fractional_bits: int = 12
    precision_trace_bit_step: int = 4
    similarity_threshold: float = 0.72
    min_valid_frames: int = 12
    concurrency: int = 4
    snapshot_interval: int = 8
    silence_energy_threshold: float = -11.0
    feature_significant_digits: int = 12
    precision_tolerance: float = 0.0005
    expected_sample_rate: int = 16000
    expected_channels: int = 1
    expected_sample_width: int = 2
    f0_min_hz: float = 65.0
    f0_max_hz: float = 420.0
    f0_voicing_threshold: float = 0.30
    projection_seed: int = 20261117
    projection_matrix_scale: int = 128
    projection_clip: float = 4.0
    similarity_scale: int = 1_000_000_000
    global_version: int = 1
    quantile_probabilities: tuple[float, ...] = (0.1, 0.5, 0.9)

    def __post_init__(self) -> None:
        if self.frame_ms <= 0 or self.hop_ms <= 0:
            raise ConfigError("frame_ms and hop_ms must be positive")
        if self.expected_sample_rate <= 0:
            raise ConfigError("expected_sample_rate must be positive")
        if self.expected_channels != 1 or self.expected_sample_width != 2:
            raise ConfigError("this build requires mono 16-bit PCM")
        if not 2 <= self.mel_bands <= 512:
            raise ConfigError("mel_bands must be in [2, 512]")
        if not 0 < self.mel_max_freq <= self.expected_sample_rate / 2:
            raise ConfigError("mel_max_freq must be within Nyquist")
        if not 1 <= self.cepstral_order <= self.mel_bands:
            raise ConfigError("cepstral_order must be in [1, mel_bands]")
        if not 1 <= self.embedding_dim <= 4096:
            raise ConfigError("embedding_dim must be in [1, 4096]")
        if self.quantization_levels < 4:
            raise ConfigError("quantization_levels must be at least 4")
        if not 2 <= self.fractional_bits <= 24:
            raise ConfigError("fractional_bits must be in [2, 24]")
        if not 1 <= self.precision_trace_bit_step <= 12:
            raise ConfigError("precision_trace_bit_step must be in [1, 12]")
        if not -1.0 <= self.similarity_threshold <= 1.0:
            raise ConfigError("similarity_threshold must be in [-1, 1]")
        if self.min_valid_frames < 1:
            raise ConfigError("min_valid_frames must be positive")
        if not 1 <= self.concurrency <= 256:
            raise ConfigError("concurrency must be in [1, 256]")
        if self.snapshot_interval < 1:
            raise ConfigError("snapshot_interval must be positive")
        if not 4 <= self.feature_significant_digits <= 17:
            raise ConfigError("feature_significant_digits must be in [4, 17]")
        if self.precision_tolerance < 0:
            raise ConfigError("precision_tolerance must be non-negative")
        if not 0 < self.f0_min_hz < self.f0_max_hz < self.expected_sample_rate / 2:
            raise ConfigError("f0 range must satisfy 0 < min < max < Nyquist")
        if not 0 <= self.f0_voicing_threshold <= 1:
            raise ConfigError("f0_voicing_threshold must be in [0, 1]")
        if not 1 <= self.projection_matrix_scale <= 1_000_000:
            raise ConfigError("projection_matrix_scale out of range")
        if self.projection_clip <= 0:
            raise ConfigError("projection_clip must be positive")
        if self.similarity_scale < 1_000_000:
            raise ConfigError("similarity_scale must be at least 1e6")
        if self.global_version < 1:
            raise ConfigError("global_version must be positive")
        if not self.quantile_probabilities:
            raise ConfigError("quantile_probabilities must not be empty")
        if any(item < 0 or item > 1 for item in self.quantile_probabilities):
            raise ConfigError("quantile probabilities must be in [0, 1]")
        if tuple(sorted(self.quantile_probabilities)) != self.quantile_probabilities:
            raise ConfigError("quantile probabilities must be ascending")

    @property
    def frame_length(self) -> int:
        return max(1, int(round(self.expected_sample_rate * self.frame_ms / 1000.0)))

    @property
    def hop_length(self) -> int:
        return max(1, int(round(self.expected_sample_rate * self.hop_ms / 1000.0)))

    @property
    def high_fractional_bits(self) -> int:
        return self.fractional_bits + self.precision_trace_bit_step

    @property
    def quantile_fractions(self) -> tuple[Fraction, ...]:
        return tuple(Fraction(str(item)) for item in self.quantile_probabilities)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["quantile_probabilities"] = list(self.quantile_probabilities)
        value["frame_length_samples"] = self.frame_length
        value["hop_length_samples"] = self.hop_length
        value["high_fractional_bits"] = self.high_fractional_bits
        return value

    def hash(self) -> str:
        return sha256_obj(self.as_dict())

    def compatibility_hash(self) -> str:
        """Hash of computational configuration; runtime scheduling may change."""
        value = self.as_dict()
        value.pop("concurrency", None)
        value.pop("snapshot_interval", None)
        return sha256_obj(value)

    def feature_hash(self) -> str:
        return sha256_obj(
            {
                "schema": "feature/v1",
                "frame_ms": self.frame_ms,
                "hop_ms": self.hop_ms,
                "mel_bands": self.mel_bands,
                "mel_max_freq": self.mel_max_freq,
                "cepstral_order": self.cepstral_order,
                "feature_significant_digits": self.feature_significant_digits,
                "expected_sample_rate": self.expected_sample_rate,
                "expected_channels": self.expected_channels,
                "expected_sample_width": self.expected_sample_width,
                "f0_min_hz": self.f0_min_hz,
                "f0_max_hz": self.f0_max_hz,
                "f0_voicing_threshold": self.f0_voicing_threshold,
            }
        )

    def aggregation_hash(self) -> str:
        return sha256_obj(
            {
                "schema": "aggregation/v1",
                "feature_config": self.feature_hash(),
                "min_valid_frames": self.min_valid_frames,
                "silence_energy_threshold": self.silence_energy_threshold,
                "quantile_probabilities": list(self.quantile_probabilities),
                "interpolation": "linear_type7",
                "accumulation_order": "frame_index_ascending",
            }
        )

    def projection_hash(self) -> str:
        # Precision and quantization are intentionally excluded: the same matrix
        # is used for low/high-width tracing, isolating the precision effect.
        return sha256_obj(
            {
                "schema": "projection/v1",
                "embedding_dim": self.embedding_dim,
                "projection_seed": self.projection_seed,
                "projection_matrix_scale": self.projection_matrix_scale,
                "projection_clip": self.projection_clip,
                "stat_dim": 4 + 2 * self.cepstral_order,
            }
        )

    def embedding_hash(self) -> str:
        return sha256_obj(
            {
                "schema": "embedding/v1",
                "aggregation_config": self.aggregation_hash(),
                "projection_config": self.projection_hash(),
                "embedding_dim": self.embedding_dim,
                "quantization_levels": self.quantization_levels,
                "similarity_scale": self.similarity_scale,
            }
        )

    def similarity_hash(self) -> str:
        return sha256_obj(
            {
                "schema": "similarity/v1",
                "threshold": self.similarity_threshold,
                "similarity_scale": self.similarity_scale,
                "tie_rule": "greater_or_equal_accept",
                "norm_rule": "integer_isqrt_q16",
                "rounding": "half_away_from_zero",
            }
        )


def _normalise_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    normalised: dict[str, Any] = {}
    for key, value in raw.items():
        destination = _ALIASES.get(str(key), str(key))
        if destination in normalised and destination != key:
            raise ConfigError(f"duplicate configuration key {destination}")
        normalised[destination] = value
    return normalised


def _coerce_values(values: dict[str, Any]) -> dict[str, Any]:
    for integer_name in (
        "mel_bands",
        "cepstral_order",
        "embedding_dim",
        "quantization_levels",
        "fractional_bits",
        "precision_trace_bit_step",
        "min_valid_frames",
        "concurrency",
        "snapshot_interval",
        "feature_significant_digits",
        "expected_sample_rate",
        "expected_channels",
        "expected_sample_width",
        "projection_seed",
        "projection_matrix_scale",
        "similarity_scale",
        "global_version",
    ):
        if integer_name in values:
            values[integer_name] = _coerce_int(integer_name, values[integer_name])
    for float_name in (
        "frame_ms",
        "hop_ms",
        "mel_max_freq",
        "similarity_threshold",
        "silence_energy_threshold",
        "precision_tolerance",
        "f0_min_hz",
        "f0_max_hz",
        "f0_voicing_threshold",
        "projection_clip",
    ):
        if float_name in values:
            values[float_name] = _coerce_float(float_name, values[float_name])
    if "quantile_probabilities" in values:
        items = values["quantile_probabilities"]
        if not isinstance(items, (list, tuple)):
            raise ConfigError("quantile_probabilities must be an array")
        values["quantile_probabilities"] = tuple(
            _coerce_float("quantile_probabilities", item) for item in items
        )
    return values


def load_config(path: str | Path) -> Config:
    raw = read_json(Path(path))
    if not isinstance(raw, dict):
        raise ConfigError("configuration JSON must be an object")
    values = _normalise_mapping(raw)
    allowed = set(Config.__dataclass_fields__.keys())
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown)}")
    values = _coerce_values(values)
    return Config(**values)


def config_from_dict(raw: Mapping[str, Any]) -> Config:
    values = _coerce_values(_normalise_mapping(raw))
    return Config(**values)
