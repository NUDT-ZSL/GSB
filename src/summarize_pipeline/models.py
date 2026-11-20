from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


PRECISION_MODES = ("float64", "float32", "fixed")
ROUNDING_MODES = ("nearest_even", "half_up", "truncate", "floor", "ceil")
THRESHOLD_POLICIES = ("strict", "tolerance")


@dataclass(frozen=True)
class ScoringWeights:
    position: float
    length: float
    frequency: float
    coverage: float

    def to_dict(self) -> dict[str, float]:
        return {
            "position": self.position,
            "length": self.length,
            "frequency": self.frequency,
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class PrecisionConfig:
    mode: str
    scale_factor: int = 1_000_000
    rounding: str = "nearest_even"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scale_factor": self.scale_factor if self.mode == "fixed" else None,
            "rounding": self.rounding if self.mode == "fixed" else "ieee_even",
        }

    def with_mode(self, mode: str) -> "PrecisionConfig":
        return replace(self, mode=mode)


@dataclass(frozen=True)
class PipelineConfig:
    version: int
    controlled_vocab: tuple[str, ...]
    weights: ScoringWeights
    sentence_threshold: float
    rewrite_threshold: float
    aggregate_threshold: float
    length_budget: int
    compression_ratio: float
    temperature: float
    smoothing: float
    threshold_policy: str
    tolerance_abs: float
    subnormal_floor: float
    ideal_sentence_chars: int
    output_precision: int
    precision: PrecisionConfig

    def to_dict(self, *, include_vocab: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": self.version,
            "weights": self.weights.to_dict(),
            "sentence_threshold": self.sentence_threshold,
            "rewrite_threshold": self.rewrite_threshold,
            "aggregate_threshold": self.aggregate_threshold,
            "length_budget": self.length_budget,
            "compression_ratio": self.compression_ratio,
            "temperature": self.temperature,
            "smoothing": self.smoothing,
            "threshold_policy": self.threshold_policy,
            "tolerance_abs": self.tolerance_abs,
            "subnormal_floor": self.subnormal_floor,
            "ideal_sentence_chars": self.ideal_sentence_chars,
            "output_precision": self.output_precision,
            "precision": self.precision.to_dict(),
        }
        if include_vocab:
            result["controlled_vocab"] = list(self.controlled_vocab)
        return result

    def config_payload(self) -> dict[str, Any]:
        return self.to_dict(include_vocab=True)

    def scoring_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "controlled_vocab": list(self.controlled_vocab),
            "weights": self.weights.to_dict(),
            "ideal_sentence_chars": self.ideal_sentence_chars,
            "compression_ratio": self.compression_ratio,
        }

    def smoothing_threshold_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sentence_threshold": self.sentence_threshold,
            "rewrite_threshold": self.rewrite_threshold,
            "aggregate_threshold": self.aggregate_threshold,
            "temperature": self.temperature,
            "smoothing": self.smoothing,
            "threshold_policy": self.threshold_policy,
            "tolerance_abs": self.tolerance_abs,
            "subnormal_floor": self.subnormal_floor,
            "length_budget": self.length_budget,
            "output_precision": self.output_precision,
        }


@dataclass(frozen=True)
class SentenceInput:
    sent_no: int
    text: str
    char_len: int


@dataclass(frozen=True)
class DocumentInput:
    doc_id: str
    sentences: tuple[SentenceInput, ...]
    ref_summary_units: tuple[str, ...]
    original_chars: int

