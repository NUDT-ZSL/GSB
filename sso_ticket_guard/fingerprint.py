"""Platform-aware fixed-point fingerprint canonicalization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

from .constants import CANONICAL_DECIMALS
from .models import CanonicalFingerprint, PlatformProfile


@dataclass(frozen=True)
class FingerprintComparison:
    result: str
    code: str | None
    request: CanonicalFingerprint
    bound: CanonicalFingerprint
    component_margins: tuple[int, ...]
    ambiguous_components: tuple[int, ...]
    mismatched_components: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "result": self.result,
            "code": self.code,
            "request": self.request.as_dict(),
            "bound": self.bound.as_dict(),
            "comparison_threshold": 0,
            "component_margins": list(self.component_margins),
            "ambiguous_components": list(self.ambiguous_components),
            "mismatched_components": list(self.mismatched_components),
        }
        if self.result == "ambiguous":
            result["original_components"] = {
                "request": {
                    "platform": self.request.platform,
                    "decimals": self.request.decimals,
                    "components": list(self.request.raw_components),
                },
                "bound": {
                    "platform": self.bound.platform,
                    "decimals": self.bound.decimals,
                    "components": list(self.bound.raw_components),
                },
            }
        return result


def canonicalize(
    *,
    device_id: str,
    platform: str,
    profile: PlatformProfile,
    components: Sequence[int],
) -> CanonicalFingerprint:
    """Scale platform fixed-point components into a six-decimal space.

    A value with platform precision p is represented exactly as
    ``raw * 10**(6-p)``. Treating the declaration itself as a rounded
    observation contributes a half-quantum interval of
    ``10**(6-p) / 2`` canonical units.
    """

    exponent = CANONICAL_DECIMALS - profile.decimals
    step = 10**exponent
    aligned = tuple(int(component) * step for component in components)
    errors = tuple(step // 2 for _ in components)
    canonical_key = hashlib.sha256(
        (
            f"{CANONICAL_DECIMALS}:"
            + ",".join(str(component) for component in aligned)
        ).encode("ascii")
    ).hexdigest()
    return CanonicalFingerprint(
        device_id=device_id,
        platform=platform,
        decimals=profile.decimals,
        raw_components=tuple(int(component) for component in components),
        canonical_components=aligned,
        component_error_bounds=errors,
        alignment_step=step,
        canonical_key=canonical_key,
    )


def compare_fingerprints(
    request: CanonicalFingerprint,
    bound: CanonicalFingerprint,
) -> FingerprintComparison:
    """Compare canonical vectors without hiding quantization uncertainty."""

    if len(request.canonical_components) != len(bound.canonical_components):
        return FingerprintComparison(
            result="mismatch",
            code="E_DEVICE_MISMATCH",
            request=request,
            bound=bound,
            component_margins=(),
            ambiguous_components=(),
            mismatched_components=tuple(range(len(request.canonical_components))),
        )
    margins: list[int] = []
    ambiguous: list[int] = []
    mismatched: list[int] = []
    for index, (left, right) in enumerate(
        zip(request.canonical_components, bound.canonical_components, strict=True)
    ):
        if left == right:
            margins.append(0)
            continue
        combined_error = (
            request.component_error_bounds[index]
            + bound.component_error_bounds[index]
        )
        difference = abs(left - right)
        margin = difference - combined_error
        margins.append(margin)
        if margin > 0:
            mismatched.append(index)
        else:
            ambiguous.append(index)

    if request.canonical_key == bound.canonical_key:
        return FingerprintComparison(
            result="match",
            code=None,
            request=request,
            bound=bound,
            component_margins=tuple(margins),
            ambiguous_components=(),
            mismatched_components=(),
        )
    if mismatched:
        return FingerprintComparison(
            result="mismatch",
            code="E_DEVICE_MISMATCH",
            request=request,
            bound=bound,
            component_margins=tuple(margins),
            ambiguous_components=tuple(ambiguous),
            mismatched_components=tuple(mismatched),
        )
    return FingerprintComparison(
        result="ambiguous",
        code="E_FP_AMBIGUOUS",
        request=request,
        bound=bound,
        component_margins=tuple(margins),
        ambiguous_components=tuple(ambiguous),
        mismatched_components=(),
    )

