from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import ErrorCode, PipelineError
from .models import (
    PRECISION_MODES,
    ROUNDING_MODES,
    THRESHOLD_POLICIES,
    PipelineConfig,
    PrecisionConfig,
    ScoringWeights,
)


def _config_error(code: str, message: str, **detail: Any) -> PipelineError:
    return PipelineError(code, "config", "*", message, detail)


def parse_config(payload: dict[str, Any]) -> PipelineConfig:
    try:
        vocab = payload["controlled_vocab"]
        if not isinstance(vocab, list) or not vocab:
            raise ValueError("controlled_vocab must be a non-empty list")
        if any(not isinstance(word, str) or not word.strip() for word in vocab):
            raise ValueError("controlled_vocab entries must be non-empty strings")
        normalized_vocab = tuple(word.strip().lower() for word in vocab)
        if len(set(normalized_vocab)) != len(normalized_vocab):
            raise ValueError("controlled_vocab contains duplicates")

        raw_weights = payload["weights"]
        weights = ScoringWeights(
            position=float(raw_weights["position"]),
            length=float(raw_weights["length"]),
            frequency=float(raw_weights["frequency"]),
            coverage=float(raw_weights["coverage"]),
        )
        for name, value in weights.to_dict().items():
            if not math.isfinite(value) or value <= 0:
                raise _config_error(
                    ErrorCode.INVALID_WEIGHT,
                    f"weight {name} must be positive",
                    weight=name,
                    value=value,
                )

        precision_payload = payload["precision"]
        mode = str(precision_payload["mode"])
        if mode not in PRECISION_MODES:
            raise _config_error(
                ErrorCode.UNKNOWN_PRECISION,
                f"unknown precision mode: {mode}",
                precision_mode=mode,
                allowed=list(PRECISION_MODES),
            )
        rounding = str(precision_payload.get("rounding", "nearest_even"))
        if rounding not in ROUNDING_MODES:
            raise _config_error(
                ErrorCode.INVALID_ROUNDING,
                f"unknown rounding mode: {rounding}",
                rounding=rounding,
                allowed=list(ROUNDING_MODES),
            )
        scale_factor = int(precision_payload.get("scale_factor", 1_000_000))
        if mode == "fixed" and scale_factor <= 0:
            raise _config_error(
                ErrorCode.INVALID_SCALE,
                "fixed-point scale_factor must be positive",
                scale_factor=scale_factor,
            )
        precision = PrecisionConfig(mode=mode, scale_factor=scale_factor, rounding=rounding)

        smoothing = float(payload["smoothing"])
        if not 0.0 <= smoothing <= 1.0:
            raise _config_error(
                ErrorCode.INVALID_SMOOTHING,
                "smoothing must be in [0, 1]",
                smoothing=smoothing,
            )

        threshold_policy = str(payload.get("threshold_policy", "tolerance"))
        if threshold_policy not in THRESHOLD_POLICIES:
            raise _config_error(
                ErrorCode.INVALID_CONFIG,
                f"unknown threshold policy: {threshold_policy}",
                threshold_policy=threshold_policy,
            )

        config = PipelineConfig(
            version=int(payload.get("version", 1)),
            controlled_vocab=normalized_vocab,
            weights=weights,
            sentence_threshold=float(payload["sentence_threshold"]),
            rewrite_threshold=float(payload["rewrite_threshold"]),
            aggregate_threshold=float(payload["aggregate_threshold"]),
            length_budget=int(payload["length_budget"]),
            compression_ratio=float(payload["compression_ratio"]),
            temperature=float(payload["temperature"]),
            smoothing=smoothing,
            threshold_policy=threshold_policy,
            tolerance_abs=float(payload.get("tolerance_abs", 1e-7)),
            subnormal_floor=float(payload.get("subnormal_floor", 1e-12)),
            ideal_sentence_chars=int(payload.get("ideal_sentence_chars", 72)),
            output_precision=int(payload.get("output_precision", 12)),
            precision=precision,
        )
    except PipelineError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _config_error(ErrorCode.INVALID_CONFIG, f"invalid configuration: {exc}") from exc

    _validate_ranges(config)
    return config


def _validate_ranges(config: PipelineConfig) -> None:
    if config.version <= 0:
        raise _config_error(ErrorCode.INVALID_CONFIG, "version must be positive")
    if config.length_budget < 0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG, "length_budget must be non-negative"
        )
    if not 0.0 < config.compression_ratio <= 1.0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "compression_ratio must be in (0, 1]",
            compression_ratio=config.compression_ratio,
        )
    if not math.isfinite(config.temperature) or config.temperature <= 0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "temperature must be positive",
            temperature=config.temperature,
        )
    if not math.isfinite(config.tolerance_abs) or config.tolerance_abs < 0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "tolerance_abs must be non-negative",
            tolerance_abs=config.tolerance_abs,
        )
    if not math.isfinite(config.subnormal_floor) or config.subnormal_floor <= 0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "subnormal_floor must be positive",
            subnormal_floor=config.subnormal_floor,
        )
    if config.ideal_sentence_chars <= 0:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "ideal_sentence_chars must be positive",
            ideal_sentence_chars=config.ideal_sentence_chars,
        )
    if not 0 <= config.output_precision <= 15:
        raise _config_error(
            ErrorCode.INVALID_CONFIG,
            "output_precision must be in [0, 15]",
            output_precision=config.output_precision,
        )
    for name, value in (
        ("sentence_threshold", config.sentence_threshold),
        ("rewrite_threshold", config.rewrite_threshold),
        ("aggregate_threshold", config.aggregate_threshold),
    ):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise _config_error(
                ErrorCode.INVALID_CONFIG,
                f"{name} must be in [0, 1]",
                field=name,
                value=value,
            )


def load_config(path: str | Path) -> PipelineConfig:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise _config_error(ErrorCode.INVALID_CONFIG, f"cannot load config: {exc}") from exc
    if not isinstance(payload, dict):
        raise _config_error(ErrorCode.INVALID_CONFIG, "configuration root must be an object")
    return parse_config(payload)

