"""Controlled normalization domain for sampling parameters."""

from __future__ import annotations

import math
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import NormalizedParams, SamplingConfig

TEMPERATURE_MIN_EXCLUSIVE = 0.0
TEMPERATURE_MAX_INCLUSIVE = 5.0
TOP_K_MIN = 1
TOP_P_MIN_EXCLUSIVE = 0.0
TOP_P_MAX_INCLUSIVE = 1.0
REPETITION_MIN_EXCLUSIVE = 0.0
REPETITION_MAX_INCLUSIVE = 10.0

RANGE_POLICY = {
    "temperature": "(0, 5]",
    "top_k": "integer in [1, vocabulary_size]; values above vocabulary_size clamp",
    "top_p": "(0, 1]; 1.0 is the explicit no-truncation sentinel",
    "repetition_penalty": "(0, 10]",
}


def normalize_params(
    config: SamplingConfig,
    *,
    vocabulary_size: int,
    request_id: str | None = None,
) -> NormalizedParams:
    if vocabulary_size <= 0:
        raise BusinessError(
            ErrorCode.VOCAB_VERSION_MISMATCH,
            "vocabulary size must be positive",
            request_id=request_id,
            field="tokenizer_version",
        )

    _validate_temperature(config, request_id=request_id)
    top_k, clamped_fields = _normalize_top_k(
        config,
        vocabulary_size=vocabulary_size,
        request_id=request_id,
    )
    top_p, top_p_default = _normalize_top_p(config, request_id=request_id)
    repetition, repetition_default = _normalize_repetition_penalty(
        config,
        request_id=request_id,
    )

    defaulted = [item for item in (top_p_default, repetition_default) if item]
    return NormalizedParams(
        temperature=config.temperature,
        top_k=top_k,
        top_p=top_p,
        repetition_penalty=repetition,
        vocabulary_size=vocabulary_size,
        temperature_literal=config.temperature_literal,
        defaulted_fields=tuple(defaulted),
        clamped_fields=tuple(clamped_fields),
        range_policy=dict(RANGE_POLICY),
    )


def _validate_temperature(
    config: SamplingConfig, *, request_id: str | None
) -> None:
    value = config.temperature
    if (
        not math.isfinite(value)
        or value <= TEMPERATURE_MIN_EXCLUSIVE
        or value > TEMPERATURE_MAX_INCLUSIVE
    ):
        _range_error(
            "temperature",
            config.temperature_literal,
            RANGE_POLICY["temperature"],
            "temperature must be finite and greater than zero",
            request_id=request_id,
        )


def _normalize_top_k(
    config: SamplingConfig,
    *,
    vocabulary_size: int,
    request_id: str | None,
) -> tuple[int, list[dict[str, Any]]]:
    value = config.top_k
    original = config.literal_values.get("top_k", str(value))
    if not math.isfinite(value) or value < TOP_K_MIN or not float(value).is_integer():
        _range_error(
            "top_k",
            original,
            RANGE_POLICY["top_k"],
            "top_k must be a positive integer",
            request_id=request_id,
        )
    top_k = int(value)
    clamped: list[dict[str, Any]] = []
    if top_k > vocabulary_size:
        clamped.append(
            {
                "field": "top_k",
                "before": top_k,
                "after": vocabulary_size,
                "rule": "clamp_top_k_to_vocabulary_size",
                "reason": "requesting more candidates than the vocabulary is a no-op",
            }
        )
        top_k = vocabulary_size
    return top_k, clamped


def _normalize_top_p(
    config: SamplingConfig, *, request_id: str | None
) -> tuple[float, dict[str, Any] | None]:
    if config.top_p is None:
        if config.version != "v1":
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required field 'top_p' is missing for {config.version}",
                request_id=request_id,
                field="top_p",
            )
        return 1.0, {
            "field": "top_p",
            "value": 1.0,
            "rule": "v1_effective_default_no_truncation",
            "source": "v1",
        }
    value = config.top_p
    if (
        not math.isfinite(value)
        or value <= TOP_P_MIN_EXCLUSIVE
        or value > TOP_P_MAX_INCLUSIVE
    ):
        _range_error(
            "top_p",
            config.literal_values.get("top_p", str(value)),
            RANGE_POLICY["top_p"],
            "top_p must be greater than zero and at most one",
            request_id=request_id,
        )
    return value, None


def _normalize_repetition_penalty(
    config: SamplingConfig, *, request_id: str | None
) -> tuple[float, dict[str, Any] | None]:
    if config.repetition_penalty is None:
        if config.version != "v1":
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required field 'repetition_penalty' is missing for {config.version}",
                request_id=request_id,
                field="repetition_penalty",
            )
        return 1.0, {
            "field": "repetition_penalty",
            "value": 1.0,
            "rule": "v1_effective_default_neutral",
            "source": "v1",
        }
    value = config.repetition_penalty
    if (
        not math.isfinite(value)
        or value <= REPETITION_MIN_EXCLUSIVE
        or value > REPETITION_MAX_INCLUSIVE
    ):
        _range_error(
            "repetition_penalty",
            config.literal_values.get("repetition_penalty", str(value)),
            RANGE_POLICY["repetition_penalty"],
            "repetition_penalty must be greater than zero",
            request_id=request_id,
        )
    return value, None


def _range_error(
    field_name: str,
    original_value: Any,
    allowed_range: str,
    message: str,
    *,
    request_id: str | None,
) -> None:
    raise BusinessError(
        ErrorCode.PARAM_RANGE,
        message,
        request_id=request_id,
        field=field_name,
        original_value=original_value,
        details={"allowed_range": allowed_range},
    )
