"""Explicit, auditable v1 <-> v2 migration rules."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .config_io import read_config, serialize_v1, serialize_v2
from .errors import BusinessError, ErrorCode
from .models import MigrationResult, SamplingConfig

TEMPERATURE_QUANTUM = Decimal("0.000001")


def upgrade_v1_to_v2(
    config: SamplingConfig,
    *,
    default_vocab_version: str,
    request_id: str | None = None,
) -> MigrationResult:
    """Fill fields not expressible by v1 and record every supplementation."""

    if config.version != "v1":
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            "v1 -> v2 upgrade requires a v1 source configuration",
            request_id=request_id,
            field="version",
            version=config.version,
        )
    if not default_vocab_version or not isinstance(default_vocab_version, str):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "a non-empty vocabulary binding is required for v1 -> v2 upgrade",
            request_id=request_id,
            field="vocab_version",
        )

    target = SamplingConfig(
        version="v2",
        temperature=config.temperature,
        top_k=config.top_k,
        temperature_literal=config.temperature_literal,
        top_p=1.0,
        repetition_penalty=1.0,
        vocab_version=default_vocab_version,
        units={
            "temperature": "ratio",
            "top_k": "tokens",
            "top_p": "probability",
            "repetition_penalty": "multiplier",
            "vocab_version": "vocab-version",
        },
        literal_values={
            "temperature": config.temperature_literal,
            "top_k": config.literal_values.get("top_k", str(config.top_k)),
            "top_p": "1.0",
            "repetition_penalty": "1.0",
            "vocab_version": default_vocab_version,
        },
        source_format="json-unit-objects",
    )
    output = serialize_v2(target)
    readback = read_config(output, request_id=request_id)
    added_fields = [
        {
            "field": "top_p",
            "value": 1.0,
            "unit": "probability",
            "rule": "v1_to_v2.default_top_p",
            "reason": "v1 has no top_p field; effective no-truncation default",
        },
        {
            "field": "repetition_penalty",
            "value": 1.0,
            "unit": "multiplier",
            "rule": "v1_to_v2.default_repetition_penalty",
            "reason": "v1 has no repetition penalty field; neutral default",
        },
        {
            "field": "vocab_version",
            "value": default_vocab_version,
            "unit": "vocab-version",
            "rule": "v1_to_v2.bind_request_tokenizer_version",
            "reason": "v1 stores no vocabulary binding; bind request tokenizer version",
        },
    ]
    return MigrationResult(
        source_version="v1",
        target_version="v2",
        config=readback,
        output_text=output,
        added_fields=added_fields,
    )


def downgrade_v2_to_v1(
    config: SamplingConfig,
    *,
    request_id: str | None = None,
) -> MigrationResult:
    """Quantize temperature and explicitly drop fields unavailable in v1."""

    if config.version != "v2":
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            "v2 -> v1 downgrade requires a v2 source configuration",
            request_id=request_id,
            field="version",
            version=config.version,
        )
    if config.top_p is None or config.repetition_penalty is None:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "v2 source is missing top_p or repetition_penalty",
            request_id=request_id,
            field="top_p",
        )
    if not math.isfinite(config.top_k) or not float(config.top_k).is_integer():
        raise BusinessError(
            ErrorCode.PARAM_RANGE,
            "v2 top_k cannot be represented as a v1 integer",
            request_id=request_id,
            field="top_k",
            original_value=config.literal_values.get("top_k", str(config.top_k)),
        )

    try:
        decimal_temperature = Decimal(config.temperature_literal.strip().replace("_", ""))
    except (InvalidOperation, ValueError):
        decimal_temperature = Decimal(str(config.temperature))
    quantized = decimal_temperature.quantize(
        TEMPERATURE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    quantized_literal = format(quantized, "f")

    target = SamplingConfig(
        version="v1",
        temperature=float(quantized),
        top_k=int(config.top_k),
        temperature_literal=quantized_literal,
        units={
            "temperature": "implicit:ratio",
            "top_k": "implicit:tokens",
        },
        literal_values={
            "temperature": quantized_literal,
            "top_k": str(int(config.top_k)),
        },
        source_format="json-flat",
    )
    output = serialize_v1(target)
    readback = read_config(output, request_id=request_id)

    dropped_fields = [
        {
            "field": "top_p",
            "source_value": config.top_p,
            "unit": "probability",
            "rule": "v2_to_v1.drop_unexpressible",
            "semantic_loss": not math.isclose(config.top_p, 1.0, rel_tol=0.0, abs_tol=1e-12),
        },
        {
            "field": "repetition_penalty",
            "source_value": config.repetition_penalty,
            "unit": "multiplier",
            "rule": "v2_to_v1.drop_unexpressible",
            "semantic_loss": not math.isclose(
                config.repetition_penalty,
                1.0,
                rel_tol=0.0,
                abs_tol=1e-12,
            ),
        },
        {
            "field": "vocab_version",
            "source_value": config.vocab_version,
            "unit": "vocab-version",
            "rule": "v2_to_v1.drop_binding_requires_request_tokenizer_version",
            "semantic_loss": False,
            "rehydration": "request.tokenizer_version",
        },
        {
            "field": "field_units",
            "source_value": dict(config.units),
            "rule": "v2_to_v1.drop_unit_markers",
            "semantic_loss": False,
        },
    ]
    quantized_fields = [
        {
            "field": "temperature",
            "before": config.temperature_literal,
            "before_value": config.temperature,
            "after": quantized_literal,
            "after_value": float(quantized),
            "rule": "v2_to_v1.quantize_temperature_6dp_round_half_up",
            "changed": float(quantized) != config.temperature,
        }
    ]
    losses = [
        {
            "field": item["field"],
            "source_value": item["source_value"],
            "reason": "v1 has no field able to preserve this sampling semantic",
        }
        for item in dropped_fields
        if item.get("semantic_loss")
    ]
    if quantized_fields[0]["changed"]:
        losses.append(
            {
                "field": "temperature",
                "source_value": config.temperature_literal,
                "reason": "temperature was quantized to six decimal places",
                "after": quantized_literal,
            }
        )
    return MigrationResult(
        source_version="v2",
        target_version="v1",
        config=readback,
        output_text=output,
        dropped_fields=dropped_fields,
        quantized_fields=quantized_fields,
        losses=losses,
    )
