"""Order-independent fixed-point device fingerprint comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import Codes
from .models import DeviceFingerprint
from .util import sha256_canonical


@dataclass(frozen=True)
class FingerprintComparison:
    conclusion: str
    code: str | None
    canonical_components: tuple[int, ...]
    canonical_fp_key: str
    canonical_scale: int
    threshold_micro: int
    error_bounds: tuple[int, ...]
    component_details: tuple[dict[str, Any], ...]
    margin: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "code": self.code,
            "canonical_components": list(self.canonical_components),
            "canonical_fp_key": self.canonical_fp_key,
            "canonical_scale": self.canonical_scale,
            "threshold_micro": self.threshold_micro,
            "error_bounds": list(self.error_bounds),
            "component_details": list(self.component_details),
            "margin": self.margin,
        }


def align_components(
    components: tuple[int, ...],
    scale: int,
    canonical_scale: int = 6,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if scale < 0 or scale > 6 or canonical_scale < 0 or canonical_scale > 6:
        raise ValueError("scales must be in [0, 6]")
    if scale > canonical_scale:
        raise ValueError("source scale cannot exceed the canonical scale")
    factor = 10 ** (canonical_scale - scale)
    half_unit = factor // 2
    return (
        tuple(component * factor for component in components),
        tuple(half_unit for _ in components),
    )


def canonical_fingerprint_key(
    components: tuple[int, ...],
    canonical_scale: int = 6,
) -> str:
    return sha256_canonical(
        {
            "space": "fixedpoint",
            "scale": canonical_scale,
            "components": list(components),
        }
    )


def compare_device(
    bound: DeviceFingerprint,
    attempt_components: tuple[int, ...],
    attempt_scale: int,
    threshold_micro: int,
    canonical_scale: int = 6,
) -> FingerprintComparison:
    if threshold_micro < 0:
        raise ValueError("threshold_micro must be non-negative")
    if len(bound.components) != len(attempt_components):
        return FingerprintComparison(
            conclusion="MISMATCH",
            code=Codes.DEVICE_MISMATCH,
            canonical_components=tuple(),
            canonical_fp_key=canonical_fingerprint_key(tuple(), canonical_scale),
            canonical_scale=canonical_scale,
            threshold_micro=threshold_micro,
            error_bounds=tuple(),
            component_details=tuple(),
            margin=0,
        )

    if attempt_scale > canonical_scale:
        return FingerprintComparison(
            conclusion="MISMATCH",
            code=Codes.DEVICE_MISMATCH,
            canonical_components=tuple(),
            canonical_fp_key=canonical_fingerprint_key(tuple(), canonical_scale),
            canonical_scale=canonical_scale,
            threshold_micro=threshold_micro,
            error_bounds=tuple(),
            component_details=tuple(),
            margin=0,
        )

    bound_canonical, bound_errors = align_components(
        bound.components, bound.scale, canonical_scale
    )
    attempt_canonical, attempt_errors = align_components(
        attempt_components, attempt_scale, canonical_scale
    )

    details: list[dict[str, Any]] = []
    error_bounds: list[int] = []
    any_mismatch = False
    any_ambiguous = False
    margins: list[int] = []

    for position, (reference, supplied, ref_error, attempt_error) in enumerate(
        zip(bound_canonical, attempt_canonical, bound_errors, attempt_errors)
    ):
        total_error = ref_error + attempt_error
        difference = abs(supplied - reference)
        lower = max(0, difference - total_error)
        upper = difference + total_error
        if upper <= threshold_micro:
            conclusion = "MATCH"
            margin = threshold_micro - upper
        elif lower > threshold_micro:
            conclusion = "MISMATCH"
            margin = lower - threshold_micro
            any_mismatch = True
        else:
            conclusion = "AMBIGUOUS"
            margin = min(threshold_micro - lower, upper - threshold_micro)
            any_ambiguous = True
        margins.append(margin)
        error_bounds.append(total_error)
        details.append(
            {
                "position": position,
                "reference": reference,
                "supplied": supplied,
                "difference": difference,
                "reference_error": ref_error,
                "attempt_error": attempt_error,
                "error_bound": total_error,
                "interval": [lower, upper],
                "threshold": threshold_micro,
                "conclusion": conclusion,
                "margin": margin,
            }
        )

    if any_mismatch:
        conclusion = "MISMATCH"
        code = Codes.DEVICE_MISMATCH
    elif any_ambiguous:
        conclusion = "AMBIGUOUS"
        code = Codes.FP_AMBIGUOUS
    else:
        conclusion = "MATCH"
        code = None

    return FingerprintComparison(
        conclusion=conclusion,
        code=code,
        canonical_components=attempt_canonical,
        canonical_fp_key=canonical_fingerprint_key(attempt_canonical, canonical_scale),
        canonical_scale=canonical_scale,
        threshold_micro=threshold_micro,
        error_bounds=tuple(error_bounds),
        component_details=tuple(details),
        margin=min(margins) if margins else 0,
    )

