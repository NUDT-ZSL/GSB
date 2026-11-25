"""Fixed-point conversion and conservative error-ledger primitives."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable


def decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _round_decimal(value: Decimal, mode: str) -> int:
    if mode == "half_up":
        rounding = ROUND_HALF_UP
    elif mode == "floor":
        rounding = ROUND_FLOOR
    elif mode == "ceil":
        rounding = ROUND_CEILING
    else:
        raise ValueError(f"unsupported rounding mode: {mode}")
    return int(value.to_integral_value(rounding=rounding))


@dataclass(frozen=True)
class Quantity:
    entry_id: str
    raw_value: float
    raw_text: str
    fixed_value: int
    scale: str
    error_upper_bound: Decimal
    source_chain: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "raw_float": self.raw_value,
            "raw_text": self.raw_text,
            "fixed_value": self.fixed_value,
            "scale": self.scale,
            "error_upper_bound": decimal_string(self.error_upper_bound),
            "source_chain": list(self.source_chain),
        }


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    operation: str
    label: str
    raw_value: float
    raw_text: str
    scale: str
    fixed_value: int
    rounding: str
    quantization_error: str
    error_upper_bound: str
    inputs: tuple[str, ...]
    source_chain: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "operation": self.operation,
            "label": self.label,
            "raw_float": self.raw_value,
            "raw_text": self.raw_text,
            "scale": self.scale,
            "fixed_value": self.fixed_value,
            "rounding": self.rounding,
            "quantization_error": self.quantization_error,
            "error_upper_bound": self.error_upper_bound,
            "inputs": list(self.inputs),
            "source_chain": list(self.source_chain),
        }


class FixedLedger:
    """Records every quantization, addition and multiplication in one batch."""

    def __init__(self, rounding_mode: str) -> None:
        self.rounding_mode = rounding_mode
        self.entries: list[LedgerEntry] = []
        self._by_id: dict[str, LedgerEntry] = {}

    def _register(
        self,
        *,
        entry_id: str,
        operation: str,
        label: str,
        raw_value: float,
        scale: Decimal,
        fixed_value: int,
        quantization_error: Decimal,
        error_upper_bound: Decimal,
        inputs: Iterable[str],
        source_chain: Iterable[str],
        rounding: str = "none",
    ) -> Quantity:
        if not math.isfinite(raw_value):
            raise ValueError(f"non-finite ledger value for {entry_id}")
        if entry_id in self._by_id:
            raise ValueError(f"duplicate ledger entry id: {entry_id}")
        inputs_tuple = tuple(inputs)
        source_tuple = tuple(source_chain)
        entry = LedgerEntry(
            entry_id=entry_id,
            operation=operation,
            label=label,
            raw_value=float(raw_value),
            raw_text=repr(float(raw_value)),
            scale=decimal_string(scale),
            fixed_value=fixed_value,
            rounding=rounding,
            quantization_error=decimal_string(quantization_error),
            error_upper_bound=decimal_string(error_upper_bound),
            inputs=inputs_tuple,
            source_chain=source_tuple,
        )
        self.entries.append(entry)
        self._by_id[entry_id] = entry
        return Quantity(
            entry_id=entry_id,
            raw_value=entry.raw_value,
            raw_text=entry.raw_text,
            fixed_value=fixed_value,
            scale=entry.scale,
            error_upper_bound=error_upper_bound,
            source_chain=source_tuple,
        )

    def quantize(
        self,
        entry_id: str,
        label: str,
        raw_value: float,
        scale: float | int,
        *,
        inputs: Iterable[str] = (),
    ) -> Quantity:
        if not math.isfinite(float(raw_value)):
            raise ValueError(f"cannot quantize non-finite value for {entry_id}")
        raw_decimal = _decimal(float(raw_value))
        scale_decimal = _decimal(float(scale))
        if scale_decimal <= 0:
            raise ValueError("fixed-point scale must be positive")
        fixed = _round_decimal(raw_decimal * scale_decimal, self.rounding_mode)
        actual = Decimal(fixed) / scale_decimal
        error = abs(raw_decimal - actual)
        if actual > raw_decimal:
            direction = "up"
        elif actual < raw_decimal:
            direction = "down"
        else:
            direction = "none"
        input_ids = tuple(inputs)
        chain: list[str] = []
        for input_id in input_ids:
            if input_id not in self._by_id:
                raise ValueError(f"unknown ledger input: {input_id}")
            for ancestor in self._by_id[input_id].source_chain:
                if ancestor not in chain:
                    chain.append(ancestor)
        chain.append(entry_id)
        return self._register(
            entry_id=entry_id,
            operation="quantize",
            label=label,
            raw_value=float(raw_value),
            scale=scale_decimal,
            fixed_value=fixed,
            quantization_error=error,
            error_upper_bound=error,
            inputs=input_ids,
            source_chain=chain,
            rounding=direction,
        )

    def add(
        self,
        entry_id: str,
        label: str,
        quantities: Iterable[Quantity],
        scale: float | int,
    ) -> Quantity:
        items = tuple(quantities)
        if not items:
            raise ValueError("addition requires at least one operand")
        scale_decimal = _decimal(float(scale))
        for item in items:
            if item.entry_id not in self._by_id:
                raise ValueError(f"unknown ledger input: {item.entry_id}")
            if _decimal(float(item.scale)) != scale_decimal:
                raise ValueError("addition operands must share a scale")
        raw_float = sum(item.raw_value for item in items)
        raw_decimal = sum((Decimal(item.raw_text) for item in items), Decimal(0))
        fixed = sum(item.fixed_value for item in items)
        actual = Decimal(fixed) / scale_decimal
        quantization_error = abs(raw_decimal - actual)
        error_bound = sum((item.error_upper_bound for item in items), Decimal(0))
        chain: list[str] = []
        for item in items:
            for ancestor in item.source_chain:
                if ancestor not in chain:
                    chain.append(ancestor)
        chain.append(entry_id)
        return self._register(
            entry_id=entry_id,
            operation="add",
            label=label,
            raw_value=raw_float,
            scale=scale_decimal,
            fixed_value=fixed,
            quantization_error=quantization_error,
            error_upper_bound=error_bound,
            inputs=tuple(item.entry_id for item in items),
            source_chain=chain,
            rounding="none",
        )

    def multiply_constant(
        self,
        entry_id: str,
        label: str,
        quantity: Quantity,
        factor: float,
        output_scale: float | int,
    ) -> Quantity:
        if quantity.entry_id not in self._by_id:
            raise ValueError(f"unknown ledger input: {quantity.entry_id}")
        if not math.isfinite(float(factor)):
            raise ValueError("multiplication factor must be finite")
        factor_decimal = _decimal(float(factor))
        scale_decimal = _decimal(float(output_scale))
        if scale_decimal <= 0:
            raise ValueError("fixed-point scale must be positive")
        exact_raw = Decimal(quantity.raw_text) * factor_decimal
        raw_float = float(exact_raw)
        fixed = _round_decimal(exact_raw * scale_decimal, self.rounding_mode)
        actual = Decimal(fixed) / scale_decimal
        quantization_error = abs(exact_raw - actual)
        if actual > exact_raw:
            direction = "up"
        elif actual < exact_raw:
            direction = "down"
        else:
            direction = "none"
        error_bound = (
            abs(factor_decimal) * quantity.error_upper_bound + quantization_error
        )
        chain = list(quantity.source_chain)
        chain.append(entry_id)
        return self._register(
            entry_id=entry_id,
            operation="mul",
            label=label,
            raw_value=raw_float,
            scale=scale_decimal,
            fixed_value=fixed,
            quantization_error=quantization_error,
            error_upper_bound=error_bound,
            inputs=(quantity.entry_id,),
            source_chain=chain,
            rounding=direction,
        )

    def entry(self, entry_id: str) -> LedgerEntry:
        return self._by_id[entry_id]

    def source_chain(self, entry_id: str) -> list[str]:
        return list(self._by_id[entry_id].source_chain)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.entries]


def fixed_to_ceiling_integer(quantity: Quantity) -> int:
    value = Decimal(quantity.fixed_value) / _decimal(float(quantity.scale))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def fixed_normalized(quantity: Quantity) -> Decimal:
    return Decimal(quantity.fixed_value) / _decimal(float(quantity.scale))
