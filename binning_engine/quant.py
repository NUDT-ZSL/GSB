"""Fixed int8 quantization and error interval helpers."""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence

from .codes import EngineError, ErrorCode


INT8_MIN = -128
INT8_MAX = 127


def validate_quant_params(quant_scale: float, zero_point: int) -> None:
    if isinstance(quant_scale, bool) or not isinstance(quant_scale, (int, float)):
        raise EngineError(
            ErrorCode.E_QUANT_PARAM,
            "quant_scale must be a positive finite number",
            details={"quant_scale": quant_scale},
        )
    try:
        scale = float(quant_scale)
    except (TypeError, ValueError, OverflowError):
        raise EngineError(
            ErrorCode.E_QUANT_PARAM,
            "quant_scale must be a positive finite number",
            details={"quant_scale": quant_scale},
        )
    if not math.isfinite(scale) or scale <= 0.0:
        raise EngineError(
            ErrorCode.E_QUANT_PARAM,
            "quant_scale must be a positive finite number",
            details={"quant_scale": quant_scale},
        )
    if isinstance(zero_point, bool) or not isinstance(zero_point, int):
        raise EngineError(
            ErrorCode.E_QUANT_PARAM,
            "zero_point must be an integer in [-128, 127]",
            details={"zero_point": zero_point},
        )
    if not INT8_MIN <= zero_point <= INT8_MAX:
        raise EngineError(
            ErrorCode.E_QUANT_PARAM,
            "zero_point must be an integer in [-128, 127]",
            details={"zero_point": zero_point},
        )


def round_half_away_from_zero(value: float) -> int:
    """Fixed rounding rule: halves move away from zero."""

    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def quantize_scalar(value: float, quant_scale: float, zero_point: int) -> int:
    validate_quant_params(quant_scale, zero_point)
    scaled = float(value) / float(quant_scale) + zero_point
    code = round_half_away_from_zero(scaled)
    return max(INT8_MIN, min(INT8_MAX, code))


def dequantize_scalar(code: int, quant_scale: float, zero_point: int) -> float:
    return (int(code) - int(zero_point)) * float(quant_scale)


def quantize_values(
    values: Sequence[float], quant_scale: float, zero_point: int
) -> Dict[str, object]:
    codes: List[int] = []
    dequantized: List[float] = []
    errors: List[float] = []
    clipped = 0
    for value in values:
        code = quantize_scalar(value, quant_scale, zero_point)
        unclipped = round_half_away_from_zero(
            float(value) / float(quant_scale) + zero_point
        )
        if unclipped != code:
            clipped += 1
        restored = dequantize_scalar(code, quant_scale, zero_point)
        codes.append(code)
        dequantized.append(restored)
        errors.append(restored - float(value))

    if errors:
        error_min = min(errors)
        error_max = max(errors)
        error_abs_max = max(abs(item) for item in errors)
    else:
        error_min = error_max = error_abs_max = 0.0
    return {
        "quantized": codes,
        "dequantized": dequantized,
        "error_interval": {
            "observed_min": error_min,
            "observed_max": error_max,
            "observed_abs_max": error_abs_max,
            "theoretical_half_step": float(quant_scale) / 2.0,
            "clipped_count": clipped,
        },
    }


def merge_error_intervals(intervals: Iterable[Dict[str, float]]) -> Dict[str, float]:
    intervals = list(intervals)
    if not intervals:
        return {
            "observed_min": 0.0,
            "observed_max": 0.0,
            "observed_abs_max": 0.0,
            "theoretical_half_step": 0.0,
            "clipped_count": 0,
        }
    return {
        "observed_min": min(item["observed_min"] for item in intervals),
        "observed_max": max(item["observed_max"] for item in intervals),
        "observed_abs_max": max(item["observed_abs_max"] for item in intervals),
        "theoretical_half_step": max(
            float(item.get("theoretical_half_step", 0.0)) for item in intervals
        ),
        "clipped_count": sum(int(item.get("clipped_count", 0)) for item in intervals),
    }
