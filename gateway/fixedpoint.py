"""Exact fixed-point token bucket arithmetic.

One token is worth ``scale`` ledger units.  Refill rate is expressed in whole
tokens per 1000 logical milliseconds.  For a refill delta ``dt``:

    numerator = rate * scale * dt + previous_remainder
    added     = numerator // 1000
    remainder = numerator % 1000

The remainder is strictly less than one ledger unit and is carried into the
next refill.  No wall clock and no floating point value participates in this
calculation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

REFILL_DENOMINATOR = 1000


def new_token_ledger(
    capacity_tokens: int,
    rate_per_second: int,
    scale: int,
    clock_ms: int = 0,
) -> dict[str, Any]:
    return {
        "capacity_fixed": capacity_tokens * scale,
        "balance_fixed": capacity_tokens * scale,
        "rate_per_second": rate_per_second,
        "scale": scale,
        "refill_denominator": REFILL_DENOMINATOR,
        "refill_remainder": 0,
        "last_refill_ms": clock_ms,
        "rounding_direction": "floor",
        "last_refill_fraction": 0,
        "overflow_discarded_fixed": 0,
        "total_refilled_fixed": 0,
        "total_spent_fixed": 0,
    }


def refill_token_ledger(ledger: dict[str, Any], clock_ms: int) -> dict[str, Any]:
    """Refill a ledger in place and return the refill audit record."""

    last = int(ledger["last_refill_ms"])
    if clock_ms < last:
        raise ValueError("token refill clock moved backwards")
    delta = clock_ms - last
    if delta == 0:
        return {
            "clock_ms": clock_ms,
            "delta_ms": 0,
            "added_fixed": 0,
            "remainder": int(ledger["refill_remainder"]),
            "overflow_discarded_fixed": 0,
            "rounding_direction": "floor",
        }

    scale = int(ledger["scale"])
    rate = int(ledger["rate_per_second"])
    denominator = int(ledger["refill_denominator"])
    numerator = rate * scale * delta + int(ledger["refill_remainder"])
    added = numerator // denominator
    remainder = numerator % denominator
    raw_balance = int(ledger["balance_fixed"]) + added
    capacity = int(ledger["capacity_fixed"])
    new_balance = min(capacity, raw_balance)
    overflow = raw_balance - new_balance

    ledger["balance_fixed"] = new_balance
    ledger["last_refill_ms"] = clock_ms
    ledger["refill_remainder"] = remainder
    ledger["last_refill_fraction"] = remainder
    ledger["overflow_discarded_fixed"] = int(ledger["overflow_discarded_fixed"]) + overflow
    ledger["total_refilled_fixed"] = int(ledger["total_refilled_fixed"]) + added
    ledger["rounding_direction"] = "floor"
    return {
        "clock_ms": clock_ms,
        "delta_ms": delta,
        "added_fixed": added,
        "remainder": remainder,
        "overflow_discarded_fixed": overflow,
        "rounding_direction": "floor",
    }


def charge_token_ledger(ledger: dict[str, Any], cost_fixed: int) -> int:
    balance = int(ledger["balance_fixed"])
    if cost_fixed <= 0:
        raise ValueError("token cost must be positive")
    if balance < cost_fixed:
        raise ValueError("insufficient token balance")
    ledger["balance_fixed"] = balance - cost_fixed
    ledger["total_spent_fixed"] = int(ledger["total_spent_fixed"]) + cost_fixed
    return int(ledger["balance_fixed"])


def format_fixed(value_fixed: int, scale: int) -> str:
    quotient = Decimal(value_fixed) / Decimal(scale)
    text = format(quotient, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def token_view(ledger: dict[str, Any]) -> dict[str, Any]:
    scale = int(ledger["scale"])
    return {
        "capacity_fixed": int(ledger["capacity_fixed"]),
        "balance_fixed": int(ledger["balance_fixed"]),
        "capacity": format_fixed(int(ledger["capacity_fixed"]), scale),
        "balance": format_fixed(int(ledger["balance_fixed"]), scale),
        "scale": scale,
        "refill_rate_per_second": int(ledger["rate_per_second"]),
        "last_refill_ms": int(ledger["last_refill_ms"]),
        "refill_remainder": int(ledger["refill_remainder"]),
        "refill_denominator": int(ledger["refill_denominator"]),
        "rounding_direction": ledger["rounding_direction"],
        "last_refill_fraction": int(ledger["last_refill_fraction"]),
        "overflow_discarded_fixed": int(ledger["overflow_discarded_fixed"]),
        "total_refilled_fixed": int(ledger["total_refilled_fixed"]),
        "total_spent_fixed": int(ledger["total_spent_fixed"]),
    }
