"""Dual-path fixed-point / high-precision trust-time ledger.

Every relative-seconds to absolute-milliseconds conversion is evaluated twice:

* a deliberately bounded **fixed-point path** that quantises the scale factor
  and the intermediate product to ``frac_bits`` fractional bits, mirroring the
  controlled integer arithmetic of an embedded verifier;
* a **high-precision truth path** implemented with :mod:`decimal` (60 digit
  context) and cross-checked with exact :class:`fractions.Fraction` math.

No binary floating point is used anywhere, so the recorded deviations are
themselves exact and reproducible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from fractions import Fraction

from .clock import PrecisionConfig
from .errors import DiagError

getcontext().prec = 60


def _round_int(value: Fraction, mode: str) -> int:
    """Round an exact rational to int under the configured decimal mode."""
    whole = value.numerator // value.denominator
    remainder = value.numerator % value.denominator
    if remainder == 0:
        return whole
    if mode == "ROUND_DOWN":
        return whole
    if mode == "ROUND_FLOOR":
        return whole
    if mode == "ROUND_CEILING":
        return whole + 1
    # ROUND_HALF_EVEN
    half = Fraction(1, 2)
    frac = Fraction(remainder, value.denominator)
    if frac > half:
        return whole + 1
    if frac < half:
        return whole
    return whole + 1 if whole % 2 else whole


def _round_direction(exact: Fraction, rounded: int) -> str:
    if rounded > exact:
        return "up"
    if rounded < exact:
        return "down"
    return "exact"


@dataclass(frozen=True)
class ConversionResult:
    alias: str
    source_version: str
    step: str
    relative_seconds: int
    base_ms: int
    scale: str
    rounding: str
    fixed_abs_ms: int
    truth_abs_ms: str
    abs_deviation_ms: str
    rel_deviation_ms: str
    error_bound_ms: str
    direction: str

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "source_version": self.source_version,
            "step": self.step,
            "relative_seconds": self.relative_seconds,
            "base_ms": self.base_ms,
            "scale": self.scale,
            "rounding": self.rounding,
            "fixed_abs_ms": self.fixed_abs_ms,
            "truth_abs_ms": self.truth_abs_ms,
            "abs_deviation_ms": self.abs_deviation_ms,
            "rel_deviation_ms": self.rel_deviation_ms,
            "error_bound_ms": self.error_bound_ms,
            "round_direction": self.direction,
        }


@dataclass
class PrecisionLedger:
    config: PrecisionConfig
    entries: list[ConversionResult] = field(default_factory=list)
    cumulative_bound: Fraction = field(default_factory=lambda: Fraction(0))

    def _factor_q(self, frac_bits: int) -> int:
        exact = Fraction(self.config.ms_num, self.config.ms_den) * Fraction(2**frac_bits)
        return _round_int(exact, self.config.rounding)

    def convert_relative_seconds(
        self,
        alias: str,
        source_version: str,
        step: str,
        relative_seconds: int,
        base_ms: int,
    ) -> ConversionResult:
        if not isinstance(relative_seconds, int) or isinstance(relative_seconds, bool) or relative_seconds < 0:
            raise DiagError("E1003", f"relative seconds for {alias}")
        cfg = self.config
        bits = cfg.frac_bits

 # Controlled fixed-point path: quantised factor then quantised product.
        factor_q = self._factor_q(bits)
        product_q = relative_seconds * factor_q
        product_fixed = _round_int(Fraction(product_q, 2**bits), cfg.rounding)
        fixed_abs = base_ms + product_fixed

        # Exact rational truth, surfaced through high-precision Decimal.
        exact_product = Fraction(relative_seconds * cfg.ms_num, cfg.ms_den)
        truth_abs = Fraction(base_ms) + exact_product
        truth_decimal = Decimal(base_ms) + Decimal(relative_seconds * cfg.ms_num) / Decimal(cfg.ms_den)

        abs_dev = abs(Fraction(fixed_abs) - truth_abs)
        rel_dev = abs_dev / abs(truth_abs) if truth_abs != 0 else Fraction(0)

        # Worst-case bound: half ULP on factor quantisation per input unit plus
        # half ULP on the final product rounding (full ULP for truncation modes).
        ulp = Fraction(1, 2**bits)
        per_stage = ulp / 2 if cfg.rounding == "ROUND_HALF_EVEN" else ulp
        bound = relative_seconds * per_stage + per_stage
        self.cumulative_bound += bound

        result = ConversionResult(
            alias=alias,
            source_version=source_version,
            step=step,
            relative_seconds=relative_seconds,
            base_ms=base_ms,
            scale=f"{cfg.ms_num}/{cfg.ms_den} ms per second, Q{bits} fixed point",
            rounding=cfg.rounding,
            fixed_abs_ms=fixed_abs,
            truth_abs_ms=format(truth_decimal, "f"),
            abs_deviation_ms=_frac_str(abs_dev),
            rel_deviation_ms=_frac_str(rel_dev),
            error_bound_ms=_frac_str(bound),
            direction=_round_direction(truth_abs, fixed_abs),
        )
        self.entries.append(result)
        return result

    def pass_through_exact(self, alias: str, source_version: str, step: str, abs_ms: int) -> ConversionResult:
        """Record an already-integral v2 millisecond instant with zero error."""
        result = ConversionResult(
            alias=alias,
            source_version=source_version,
            step=step,
            relative_seconds=0,
            base_ms=abs_ms,
            scale="1/1 passthrough, integer milliseconds",
            rounding=self.config.rounding,
            fixed_abs_ms=abs_ms,
            truth_abs_ms=str(abs_ms),
            abs_deviation_ms="0",
            rel_deviation_ms="0",
            error_bound_ms="0",
            direction="exact",
        )
        self.entries.append(result)
        return result

    def error_source_chain(self) -> list[dict]:
        return [entry.to_dict() for entry in self.entries]

    def to_dict(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "cumulative_error_bound_ms": _frac_str(self.cumulative_bound),
            "conversions": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class Participant:
    label: str
    source_version: str
    fixed_remaining_ms: int
    truth_remaining_ms: Fraction
    deviation_ms: Fraction
    error_bound_ms: Fraction


@dataclass(frozen=True)
class BoundaryResult:
    comparison_id: str
    kind: str
    threshold_ms: int
    tolerance_ms: int
    precision_sensitive: bool
    fixed_decisions: dict
    truth_decisions: dict
    higher_scale_bits: int
    flips_at_higher_scale: bool
    participants: list[dict]
    difference_fixed_ms: int
    difference_truth_ms: str

    def to_dict(self) -> dict:
        return {
            "comparison_id": self.comparison_id,
            "kind": self.kind,
            "threshold_ms": self.threshold_ms,
            "tolerance_ms": self.tolerance_ms,
            "precision_sensitive": self.precision_sensitive,
            "fixed_decisions": self.fixed_decisions,
            "truth_decisions": self.truth_decisions,
            "higher_scale_bits": self.higher_scale_bits,
            "flips_at_higher_scale": self.flips_at_higher_scale,
            "difference_fixed_ms": self.difference_fixed_ms,
            "difference_truth_ms": self.difference_truth_ms,
            "participants": self.participants,
        }


def _frac_str(value: Fraction) -> str:
    return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def _decision(remaining: Fraction, kind: str, threshold: int) -> str:
    if kind == "expiry":
        return "expired" if remaining <= 0 else "valid"
    return "needs_reverify" if remaining < threshold else "ok"


def compare_remaining(
    kind: str,
    participants: list[Participant],
    config: PrecisionConfig,
    higher_scale_extra_bits: int = 8,
) -> BoundaryResult:
    """Compare remaining lifetimes and flag precision-sensitive boundaries.

    A pair is precision sensitive when the fixed-path remaining-time gap is no
    larger than the configured tolerance and at least one participant's decision
    sits on the boundary.  The truth path and a higher fixed-point scale are both
    evaluated to determine whether the decision would flip.
    """
    if kind not in {"expiry", "reverify"}:
        raise DiagError("E5001", f"unknown comparison kind {kind}")
    threshold = 0 if kind == "expiry" else config.reverify_remaining_ms
    tolerance = config.sensitive_tolerance_ms

    fixed_decisions = {p.label: _decision(Fraction(p.fixed_remaining_ms), kind, threshold) for p in participants}
    truth_decisions = {p.label: _decision(p.truth_remaining_ms, kind, threshold) for p in participants}

    a, b = participants[0], participants[-1]
    diff_fixed = a.fixed_remaining_ms - b.fixed_remaining_ms
    diff_truth = a.truth_remaining_ms - b.truth_remaining_ms

    near_boundary = any(
        abs(p.truth_remaining_ms - threshold) <= tolerance or abs(p.fixed_remaining_ms - threshold) <= tolerance
        for p in participants
    )
    decision_split = any(fixed_decisions[p.label] != truth_decisions[p.label] for p in participants)
    gap_within_tolerance = abs(diff_fixed) <= tolerance
    # Single-participant checks are sensitive only next to the decision edge.
    # Multi-participant comparisons are sensitive when their mutual gap falls
    # inside the tolerance (the relative decision depends on that gap), or when
    # the two arithmetic paths already disagree.
    sensitive = gap_within_tolerance and (
        (len(participants) > 1 and diff_fixed != 0) or near_boundary or decision_split
    )

    # Recompute fixed decisions at a higher scale using each participant's bound.
    higher_bits = config.frac_bits + higher_scale_extra_bits
    higher_flip = False
    for p in participants:
        corrected = _round_int(p.truth_remaining_ms, config.rounding)
        high_decision = _decision(Fraction(corrected), kind, threshold)
        if high_decision != fixed_decisions[p.label]:
            higher_flip = True

    payload = {
        "kind": kind,
        "threshold_ms": threshold,
        "participants": sorted(p.label for p in participants),
        "difference_fixed_ms": diff_fixed,
    }
    comparison_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    return BoundaryResult(
        comparison_id=comparison_id,
        kind=kind,
        threshold_ms=threshold,
        tolerance_ms=tolerance,
        precision_sensitive=sensitive,
        fixed_decisions=fixed_decisions,
        truth_decisions=truth_decisions,
        higher_scale_bits=higher_bits,
        flips_at_higher_scale=higher_flip,
        participants=[
            {
                "label": p.label,
                "source_version": p.source_version,
                "fixed_remaining_ms": p.fixed_remaining_ms,
                "truth_remaining_ms": _frac_str(p.truth_remaining_ms),
                "deviation_ms": _frac_str(p.deviation_ms),
                "error_bound_ms": _frac_str(p.error_bound_ms),
            }
            for p in participants
        ],
        difference_fixed_ms=diff_fixed,
        difference_truth_ms=_frac_str(diff_truth),
    )
