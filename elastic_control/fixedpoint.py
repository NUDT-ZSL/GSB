"""Controlled fixed-point conversion and precision ledger.

Each metric owns a residual smaller than one fixed-point unit.  The residual is
carried into that metric's next conversion instead of being silently dropped.
Every conversion and multiplicative error propagation is recorded with a
monotonic step number so two independent implementations can be audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Any, Iterable, Mapping

from .errors import SCHEMA_ERROR, ControlError
from .util import DECIMAL_CONTEXT_PRECISION, dec, decimal_text


@dataclass
class LedgerEntry:
    step_no: int
    step_id: str
    metric: str
    operation: str
    scale: int
    input_value: str
    carried_residual: str
    adjusted_value: str
    fixed_units: int
    rounding: str
    discarded_residual: str
    error_increment: str
    cumulative_error_bound: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "step_no": self.step_no,
            "step_id": self.step_id,
            "metric": self.metric,
            "operation": self.operation,
            "scale": self.scale,
            "input": self.input_value,
            "carried_residual": self.carried_residual,
            "adjusted": self.adjusted_value,
            "fixed_units": self.fixed_units,
            "rounding": self.rounding,
            "discarded_residual": self.discarded_residual,
            "error_increment": self.error_increment,
            "cumulative_error_bound": self.cumulative_error_bound,
        }
        if self.detail:
            result["detail"] = self.detail
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LedgerEntry":
        return cls(
            step_no=int(raw["step_no"]),
            step_id=str(raw["step_id"]),
            metric=str(raw["metric"]),
            operation=str(raw["operation"]),
            scale=int(raw["scale"]),
            input_value=str(raw["input"]),
            carried_residual=str(raw["carried_residual"]),
            adjusted_value=str(raw["adjusted"]),
            fixed_units=int(raw["fixed_units"]),
            rounding=str(raw["rounding"]),
            discarded_residual=str(raw["discarded_residual"]),
            error_increment=str(raw["error_increment"]),
            cumulative_error_bound=str(raw["cumulative_error_bound"]),
            detail=dict(raw.get("detail", {})),
        )


@dataclass
class MetricLedger:
    metric: str
    scale: int | None = None
    residual: Decimal = Decimal(0)
    cumulative_error_bound: Decimal = Decimal(0)
    next_step_no: int = 1
    entries: list[LedgerEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "scale": self.scale,
            "residual": decimal_text(self.residual),
            "cumulative_error_bound": decimal_text(self.cumulative_error_bound),
            "next_step_no": self.next_step_no,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MetricLedger":
        entries = [
            LedgerEntry.from_dict(item)
            for item in raw.get("entries", [])
            if isinstance(item, Mapping)
        ]
        scale_raw = raw.get("scale")
        return cls(
            metric=str(raw["metric"]),
            scale=None if scale_raw is None else int(scale_raw),
            residual=dec(raw.get("residual", 0), "ledger.residual"),
            cumulative_error_bound=dec(
                raw.get("cumulative_error_bound", 0),
                "ledger.cumulative_error_bound",
            ),
            next_step_no=int(raw.get("next_step_no", len(entries) + 1)),
            entries=entries,
        )


@dataclass(frozen=True)
class Divergence:
    metric: str
    step_no: int
    step_id: str
    fixed_units: int
    fixed_value: Decimal
    decimal_value: Decimal
    difference: Decimal
    tolerance: Decimal
    operation: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "step_no": self.step_no,
            "step_id": self.step_id,
            "operation": self.operation,
            "fixed_units": self.fixed_units,
            "fixed_value": decimal_text(self.fixed_value),
            "decimal_value": decimal_text(self.decimal_value),
            "difference": decimal_text(self.difference),
            "tolerance": decimal_text(self.tolerance),
            "message": self.message,
        }


class FixedPointBook:
    """A per-metric residual and error ledger."""

    def __init__(self, ledgers: Mapping[str, MetricLedger] | None = None) -> None:
        self.ledgers: dict[str, MetricLedger] = {
            name: ledger for name, ledger in (ledgers or {}).items()
        }
        self.divergences: list[Divergence] = []

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "FixedPointBook":
        if not raw:
            return cls()
        metrics_raw = raw.get("metrics", raw)
        ledgers: dict[str, MetricLedger] = {}
        if isinstance(metrics_raw, Mapping):
            for name, item in metrics_raw.items():
                if isinstance(item, Mapping):
                    ledgers[str(name)] = MetricLedger.from_dict(item)
        return cls(ledgers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": {
                name: self.ledgers[name].to_dict() for name in sorted(self.ledgers)
            }
        }

    def ledger(self, metric: str, scale: int) -> MetricLedger:
        ledger = self.ledgers.get(metric)
        if ledger is None:
            ledger = MetricLedger(metric=metric, scale=scale)
            self.ledgers[metric] = ledger
        elif ledger.scale is None:
            ledger.scale = scale
        elif ledger.scale != scale:
            raise ControlError(
                SCHEMA_ERROR,
                f"metric {metric!r} uses inconsistent fixed scales",
                {"metric": metric, "existing_scale": ledger.scale, "requested_scale": scale},
            )
        return ledger

    def _next_step(self, ledger: MetricLedger, step_id: str | None, operation: str) -> tuple[int, str]:
        step_no = ledger.next_step_no
        ledger.next_step_no += 1
        return step_no, step_id or f"{operation}:{step_no:04d}"

    def convert(
        self,
        metric: str,
        value: Decimal,
        scale: int,
        *,
        operation: str,
        step_id: str | None = None,
    ) -> int:
        """Quantise ``value`` with the metric's carried residual."""

        ledger = self.ledger(metric, scale)
        step_no, resolved_step = self._next_step(ledger, step_id, operation)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            adjusted = value + ledger.residual
            scaled = adjusted * Decimal(scale)
            units = int(scaled.to_integral_value(rounding=ROUND_FLOOR))
            discarded = adjusted - (Decimal(units) / Decimal(scale))
            increment = abs(discarded)
            ledger.residual = discarded
            ledger.cumulative_error_bound += increment
            entry = LedgerEntry(
                step_no=step_no,
                step_id=resolved_step,
                metric=metric,
                operation=operation,
                scale=scale,
                input_value=decimal_text(value),
                carried_residual=decimal_text(adjusted - value),
                adjusted_value=decimal_text(adjusted),
                fixed_units=units,
                rounding="floor",
                discarded_residual=decimal_text(discarded),
                error_increment=decimal_text(increment),
                cumulative_error_bound=decimal_text(ledger.cumulative_error_bound),
            )
            ledger.entries.append(entry)
        return units

    def record_product(
        self,
        metric: str,
        *,
        left_name: str,
        left: int,
        left_error: Decimal,
        right_name: str,
        right: int,
        right_error: Decimal,
        scale: int,
        operation: str,
        step_id: str | None = None,
    ) -> int:
        """Record error propagation for an integer multiplication."""

        ledger = self.ledger(metric, scale)
        step_no, resolved_step = self._next_step(ledger, step_id, operation)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            product = left * right
            propagation = (
                abs(Decimal(left)) * right_error
                + abs(Decimal(right)) * left_error
                + left_error * right_error
            )
            ledger.cumulative_error_bound += propagation
            entry = LedgerEntry(
                step_no=step_no,
                step_id=resolved_step,
                metric=metric,
                operation=operation,
                scale=scale,
                input_value=f"{left_name}*{right_name}",
                carried_residual="0",
                adjusted_value=str(product),
                fixed_units=product,
                rounding="none",
                discarded_residual="0",
                error_increment=decimal_text(propagation),
                cumulative_error_bound=decimal_text(ledger.cumulative_error_bound),
                detail={
                    "left": left_name,
                    "left_units": left,
                    "left_error_bound": decimal_text(left_error),
                    "right": right_name,
                    "right_units": right,
                    "right_error_bound": decimal_text(right_error),
                },
            )
            ledger.entries.append(entry)
        return product

    def record_operation_error(
        self,
        metric: str,
        *,
        operation: str,
        error_increment: Decimal,
        scale: int,
        step_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record propagated error for a non-quantising operation."""

        ledger = self.ledger(metric, scale)
        step_no, resolved_step = self._next_step(ledger, step_id, operation)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            ledger.cumulative_error_bound += error_increment
            ledger.entries.append(
                LedgerEntry(
                    step_no=step_no,
                    step_id=resolved_step,
                    metric=metric,
                    operation=operation,
                    scale=scale,
                    input_value=operation,
                    carried_residual="0",
                    adjusted_value="0",
                    fixed_units=0,
                    rounding="none",
                    discarded_residual="0",
                    error_increment=decimal_text(error_increment),
                    cumulative_error_bound=decimal_text(
                        ledger.cumulative_error_bound
                    ),
                    detail=dict(detail or {}),
                )
            )

    def record_result(
        self,
        metric: str,
        *,
        operation: str,
        input_value: Decimal,
        fixed_units: int,
        scale: int,
        rounding: str,
        discarded: Decimal,
        step_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a rounded result such as the derived replica count."""

        ledger = self.ledger(metric, scale)
        step_no, resolved_step = self._next_step(ledger, step_id, operation)
        ledger.entries.append(
            LedgerEntry(
                step_no=step_no,
                step_id=resolved_step,
                metric=metric,
                operation=operation,
                scale=scale,
                input_value=decimal_text(input_value),
                carried_residual=decimal_text(ledger.residual),
                adjusted_value=decimal_text(input_value),
                fixed_units=fixed_units,
                rounding=rounding,
                discarded_residual=decimal_text(discarded),
                error_increment="0",
                cumulative_error_bound=decimal_text(ledger.cumulative_error_bound),
                detail=dict(detail or {}),
            )
        )

    def compare(
        self,
        metric: str,
        *,
        scale: int,
        fixed_units: int,
        decimal_value: Decimal,
        tolerance: Decimal,
        operation: str,
        step_id: str,
        record: bool = True,
    ) -> Divergence | None:
        ledger = self.ledger(metric, scale)
        fixed_value = Decimal(fixed_units) / Decimal(scale)
        difference = abs(fixed_value - decimal_value)
        if difference <= tolerance:
            return None
        step_no = ledger.next_step_no
        for entry in reversed(ledger.entries):
            if entry.step_id == step_id:
                step_no = entry.step_no
                break
        divergence = Divergence(
            metric=metric,
            step_no=step_no,
            step_id=step_id,
            fixed_units=fixed_units,
            fixed_value=fixed_value,
            decimal_value=decimal_value,
            difference=difference,
            tolerance=tolerance,
            operation=operation,
            message=(
                f"fixed-point path diverges from Decimal path for metric {metric!r} "
                f"at step {step_id}"
            ),
        )
        if record:
            self.divergences.append(divergence)
        return divergence


def replay_ledger(
    metric: str,
    entries: Iterable[Mapping[str, Any]],
) -> tuple[Decimal, Decimal, Decimal | None, int]:
    """Recompute residual and error bound from serialised ledger entries.

    Returns ``(residual, cumulative_error, scale, next_step_no)``.  Any
    inconsistency raises ``VERIFY_MISMATCH`` at the caller's level.
    """

    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        residual = Decimal(0)
        cumulative = Decimal(0)
        scale: int | None = None
        next_step = 1
        for raw in entries:
            entry = LedgerEntry.from_dict(raw)
            if entry.metric != metric:
                raise ValueError("ledger entry metric mismatch")
            if scale is None:
                scale = entry.scale
            elif scale != entry.scale:
                raise ValueError("ledger scale changed")
            if entry.operation in {
                "aggregate",
                "window_aggregate",
                "target",
                "target_value",
                "convert",
            }:
                input_value = dec(entry.input_value)
                adjusted = input_value + residual
                if adjusted != dec(entry.adjusted_value) or dec(entry.carried_residual) != residual:
                    raise ValueError("ledger residual replay mismatch")
                units = int(
                    (adjusted * Decimal(entry.scale)).to_integral_value(rounding=ROUND_FLOOR)
                )
                if units != entry.fixed_units:
                    raise ValueError("ledger fixed units replay mismatch")
                discarded = adjusted - Decimal(units) / Decimal(entry.scale)
                if discarded != dec(entry.discarded_residual):
                    raise ValueError("ledger discarded residual replay mismatch")
                residual = discarded
            increment = dec(entry.error_increment)
            cumulative += increment
            if cumulative != dec(entry.cumulative_error_bound):
                raise ValueError("ledger cumulative error replay mismatch")
            next_step = max(next_step, entry.step_no + 1)
        return residual, cumulative, scale, next_step
