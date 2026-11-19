"""Canonical transaction identifiers and exact numeric helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .errors import DiagnosticsError

_TX_RE = re.compile(r"^(?:tx[:/-])?([0-9]+)$", re.IGNORECASE)
_MAX_TX = 2**63 - 1


@dataclass(frozen=True, order=True)
class TxId:
    number: int

    @property
    def canonical(self) -> str:
        return f"{self.number:020d}"


def parse_tx_id(value: Any, *, location: str = "tx_id") -> TxId:
    """Parse an integer or decimal transaction id.

    The canonical on-disk form is a 20-digit, zero-padded unsigned decimal.  A
    few unambiguous textual spellings (``12``, ``tx:12``, ``tx-12``,
    ``tx/12``) are accepted at input boundaries so the two old formats can be
    normalized without changing their numeric order.
    """

    if isinstance(value, bool):
        raise DiagnosticsError("E_TX_ID", location=location, details={"value": value})
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        match = _TX_RE.match(value)
        if not match:
            raise DiagnosticsError("E_TX_ID", location=location, details={"value": value})
        number = int(match.group(1), 10)
    else:
        raise DiagnosticsError("E_TX_ID", location=location, details={"value": repr(value)})
    if number <= 0 or number > _MAX_TX:
        raise DiagnosticsError(
            "E_TX_ID",
            location=location,
            details={"value": value, "reason": "must be in 1..2^63-1"},
        )
    return TxId(number)


def require_canonical_tx_id(value: Any, *, location: str = "tx_id") -> TxId:
    tx = parse_tx_id(value, location=location)
    if not isinstance(value, str) or value != tx.canonical:
        raise DiagnosticsError(
            "E_TX_ID",
            location=location,
            details={"value": value, "canonical": tx.canonical},
        )
    return tx


def require_int(
    value: Any,
    *,
    code: str,
    location: str,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiagnosticsError(code, location=location, details={"value": repr(value)})
    if minimum is not None and value < minimum:
        raise DiagnosticsError(
            code,
            location=location,
            details={"value": value, "minimum": minimum},
        )
    return value


def require_bool(value: Any, *, code: str, location: str) -> bool:
    if not isinstance(value, bool):
        raise DiagnosticsError(code, location=location, details={"value": repr(value)})
    return value


def fraction_to_json(value: Fraction) -> float | int:
    """Return a deterministic JSON number from an exact rational.

    Quantile ranks in this component have denominators dividing 20, so all
    emitted values have a short, exact decimal expansion.  Using ``float`` here
    is still byte-stable because the same rational is always converted the same
    way, while exact forms are also emitted alongside the numbers.
    """

    if value.denominator == 1:
        return value.numerator
    return float(Decimal(value.numerator) / Decimal(value.denominator))


def fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def parse_fraction(value: Any, *, location: str) -> Fraction:
    if isinstance(value, bool):
        raise DiagnosticsError("E_CONFIG", location=location, details={"value": value})
    if isinstance(value, int):
        result = Fraction(value, 1)
    elif isinstance(value, float):
        result = Fraction(str(value))
    elif isinstance(value, str):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise DiagnosticsError(
                "E_CONFIG", location=location, details={"value": value, "reason": str(exc)}
            ) from exc
    else:
        raise DiagnosticsError("E_CONFIG", location=location, details={"value": repr(value)})
    if result < 0:
        raise DiagnosticsError("E_CONFIG", location=location, details={"value": str(result)})
    return result
