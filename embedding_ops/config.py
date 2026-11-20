"""Numerical configuration shared by quantization and accumulation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .errors import (
    E_CONFIG_INVALID,
    E_ROUNDING_MODE_UNKNOWN,
    E_SUBNORMAL_POLICY_UNKNOWN,
    business_error,
)


ROUNDING_MODES = (
    "nearest_even",
    "nearest_away",
    "truncate",
    "floor",
    "ceil",
)

SUBNORMAL_POLICIES = ("preserve", "flush_to_zero", "clamp_to_threshold")

PAD_POLICIES = ("exclude", "zero_weight")


@dataclass(frozen=True)
class NumericsConfig:
    """Explicit numerical policy.

    ``dequant_rounding_step`` and ``accum_rounding_step`` are absolute
    float64 grids. A step of zero disables the corresponding extra rounding.
    """

    rounding_mode: str = "nearest_even"
    dequant_rounding_step: float = 0.0
    accum_rounding_enabled: bool = False
    accum_rounding_step: float = 0.0
    subnormal_policy: str = "preserve"
    subnormal_threshold: float = float(np.finfo(np.float64).tiny)
    pad_policy: str = "exclude"
    tolerance_abs: float = 1e-12
    tolerance_rel: float = 1e-9
    tolerance_policy: str = "fixed_no_auto_widening"

    def __post_init__(self) -> None:
        if self.rounding_mode not in ROUNDING_MODES:
            raise business_error(
                E_ROUNDING_MODE_UNKNOWN,
                "numerics_config",
                "config",
                f"unknown rounding mode: {self.rounding_mode!r}",
                known_modes=list(ROUNDING_MODES),
            )
        if self.subnormal_policy not in SUBNORMAL_POLICIES:
            raise business_error(
                E_SUBNORMAL_POLICY_UNKNOWN,
                "numerics_config",
                "config",
                f"unknown subnormal policy: {self.subnormal_policy!r}",
                known_policies=list(SUBNORMAL_POLICIES),
            )
        if self.pad_policy not in PAD_POLICIES:
            raise business_error(
                E_CONFIG_INVALID,
                "numerics_config",
                "config",
                f"unknown pad policy: {self.pad_policy!r}",
                known_policies=list(PAD_POLICIES),
            )
        for name in (
            "dequant_rounding_step",
            "accum_rounding_step",
            "subnormal_threshold",
            "tolerance_abs",
            "tolerance_rel",
        ):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value < 0.0:
                raise business_error(
                    E_CONFIG_INVALID,
                    "numerics_config",
                    "config",
                    f"{name} must be finite and non-negative",
                    field=name,
                    value=value,
                )
        if not isinstance(self.accum_rounding_enabled, bool):
            raise business_error(
                E_CONFIG_INVALID,
                "numerics_config",
                "config",
                "accum_rounding_enabled must be boolean",
            )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tolerance"] = {
            "abs": self.tolerance_abs,
            "rel": self.tolerance_rel,
            "policy": self.tolerance_policy,
            "comparison": "abs_diff <= abs + rel * max(1, abs(reference))",
        }
        return result

    def tolerance_for(self, reference: float) -> float:
        return float(
            self.tolerance_abs
            + self.tolerance_rel * max(1.0, abs(float(reference)))
        )


def config_from_mapping(value: dict[str, Any]) -> NumericsConfig:
    tolerance = value.get("tolerance", {})
    merged = dict(value)
    merged.pop("tolerance", None)
    if isinstance(tolerance, dict):
        if "abs" in tolerance:
            merged.setdefault("tolerance_abs", tolerance["abs"])
        if "rel" in tolerance:
            merged.setdefault("tolerance_rel", tolerance["rel"])
        if "policy" in tolerance:
            merged.setdefault("tolerance_policy", tolerance["policy"])
    allowed = set(NumericsConfig.__dataclass_fields__)
    unknown = sorted(set(merged) - allowed)
    if unknown:
        raise business_error(
            E_CONFIG_INVALID,
            "numerics_config",
            "config",
            "unknown numerics configuration fields",
            fields=unknown,
        )
    return NumericsConfig(**merged)
