"""Deterministic fixed-point conversion with per-conversion ledger entries.

All engine arithmetic is integer fixed-point so that any execution order,
any parallelism level, and any crash/resume path produces bit-identical
results. Floating point is used only for error-bound estimation.
"""

ROUNDING = "half_even"


def to_fixed(x: float, scale: int) -> int:
    """Round x*scale to the nearest integer, ties to even (deterministic)."""
    return int(round(x * scale))


def div_round_half_even(numer: int, denom: int) -> int:
    """Integer division numer/denom with round-half-even, denom > 0."""
    q, r = divmod(numer, denom)
    twice = 2 * r
    if twice > denom or (twice == denom and q % 2 == 1):
        q += 1
    return q


def quant_error(raw: float, scale: int) -> tuple:
    """Return (fixed_value, quantization_error) for a raw float."""
    fixed = to_fixed(raw, scale)
    return fixed, raw - fixed / scale


def ledger_entry(node_id: str, seq: int, scale: int, raw: float) -> dict:
    fixed, err = quant_error(raw, scale)
    return {
        "node_id": node_id,
        "event_seq": seq,
        "scale": scale,
        "rounding": ROUNDING,
        "raw_value": raw,
        "fixed_value": fixed,
        "quant_error": err,
    }


def accumulation_bound(errors: list, values: list) -> float:
    """Error upper bound for a sum: sum of |quant errors| plus the float64
    accumulation bound (n-1)*eps*sum(|x|)."""
    n = len(values)
    if n == 0:
        return 0.0
    eps = 2.0 ** -53
    quant = sum(abs(e) for e in errors)
    float_bound = (n - 1) * eps * sum(abs(v) for v in values)
    return quant + float_bound
