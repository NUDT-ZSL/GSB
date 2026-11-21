from __future__ import annotations

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from .errors import BusinessError, ErrorCode
from .models import COLD, HOT, StorageObject, TierConfig

SCORE_PRECISION = 28
SCORE_QUANTUM = Decimal("0.000000000001")
SCORE_CONTEXT = Context(prec=SCORE_PRECISION, rounding=ROUND_HALF_EVEN)


def heat_score(obj: StorageObject, logical_clock: int) -> Decimal:
    """Compute the fixed-order heat score.

    Formula:
        age = max(0, logical_clock - last_access_clock)
        recency = 1 / (1 + age)
        score = round_12(heat_weight * recency * (1 + access_count))

    Multiplication order is fixed as (weight * recency) * count_factor.
    """
    with localcontext(SCORE_CONTEXT):
        age = max(0, logical_clock - obj.last_access_clock)
        recency = Decimal(1) / Decimal(1 + age)
        count_factor = Decimal(1 + obj.access_count)
        raw = (obj.heat_weight * recency) * count_factor
        quantum_exponent = SCORE_QUANTUM.as_tuple().exponent
        integer_digits = max(1, raw.adjusted() + 1)
        precision = max(
            SCORE_PRECISION,
            integer_digits + abs(int(quantum_exponent)) + 2,
        )
        quantize_context = Context(
            prec=precision,
            rounding=ROUND_HALF_EVEN,
            Emax=999_999_999,
            Emin=-999_999_999,
        )
        return raw.quantize(
            SCORE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
            context=quantize_context,
        )


def target_layer(score: Decimal, config: TierConfig) -> str:
    return HOT if score >= config.hot_threshold else COLD


def check_clock(
    current_watermark: int,
    injected_clock: int,
    tolerance: int,
) -> int:
    if injected_clock < current_watermark - tolerance:
        raise BusinessError(
            ErrorCode.CLOCK_ROLLBACK,
            "injected logical clock is behind the persisted watermark beyond tolerance",
            {
                "current_watermark": current_watermark,
                "injected_clock": injected_clock,
                "tolerance": tolerance,
                "minimum_accepted": current_watermark - tolerance,
            },
        )
    return max(current_watermark, injected_clock)
