"""Luminance-domain tone mapping and per-channel post-mapping handling."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .errors import BusinessError


ALLOWED_TONE_CURVES = ("reinhard_extended", "linear_clip")


@dataclass(frozen=True)
class ToneMapper:
    curve: str
    target_peak_nits: float
    white_nits: float
    exposure: float

    @classmethod
    def create(
        cls,
        curve: object,
        target_peak_nits: float,
        white_nits: object = None,
        exposure: object = 1.0,
    ) -> "ToneMapper":
        if not isinstance(curve, str) or curve not in ALLOWED_TONE_CURVES:
            raise BusinessError(
                "E_TONE_MAPPING",
                "unsupported tone mapping curve",
                {"value": repr(curve), "allowed": list(ALLOWED_TONE_CURVES)},
            )
        if not _number(exposure) or float(exposure) <= 0.0:
            raise BusinessError(
                "E_TONE_MAPPING",
                "tone mapping exposure must be finite and positive",
                {"exposure": repr(exposure)},
            )
        if white_nits is None:
            white = float(target_peak_nits)
        elif _number(white_nits) and float(white_nits) > 0.0:
            white = float(white_nits)
        else:
            raise BusinessError(
                "E_TONE_MAPPING",
                "tone mapping white_nits must be finite and positive",
                {"white_nits": repr(white_nits)},
            )
        if curve == "reinhard_extended" and white < target_peak_nits:
            raise BusinessError(
                "E_TONE_MAPPING",
                "reinhard_extended requires white_nits >= target_peak_nits",
                {
                    "white_nits": white,
                    "target_peak_nits": target_peak_nits,
                },
            )
        mapper = cls(
            curve=curve,
            target_peak_nits=float(target_peak_nits),
            white_nits=white,
            exposure=float(exposure),
        )
        mapper._verify_monotonic()
        return mapper

    def map_luminance(self, luminance_nits: float) -> float:
        if luminance_nits <= 0.0:
            return 0.0
        x = luminance_nits * self.exposure / self.target_peak_nits
        if not math.isfinite(x):
            return float(self.target_peak_nits)
        if self.curve == "linear_clip":
            mapped = min(x, 1.0)
        else:
            white_ratio = self.white_nits / self.target_peak_nits
            mapped = x * (1.0 + x / (white_ratio * white_ratio)) / (1.0 + x)
            mapped = min(mapped, 1.0)
        if not math.isfinite(mapped):
            mapped = 1.0
        return float(mapped * self.target_peak_nits)

    def apply_to_rgb(
        self,
        rgb_nits: Sequence[float],
        luminance_nits: float,
        channel_ceiling_nits: float | None = None,
    ) -> tuple[list[float], bool]:
        """Scale all channels equally, then apply a deterministic channel clamp."""

        ceiling = (
            self.target_peak_nits
            if channel_ceiling_nits is None
            else float(channel_ceiling_nits)
        )
        mapped_y = self.map_luminance(luminance_nits)
        if luminance_nits <= 0.0:
            return [0.0, 0.0, 0.0], False
        ratio = mapped_y / luminance_nits
        scaled = [channel * ratio for channel in rgb_nits]
        needs_channel_clamp = any(
            channel < 0.0 or channel > ceiling for channel in scaled
        )
        clamped = [min(max(channel, 0.0), ceiling) for channel in scaled]
        return clamped, needs_channel_clamp

    def report(self) -> dict[str, object]:
        return {
            "curve": self.curve,
            "target_peak_nits": self.target_peak_nits,
            "white_nits": self.white_nits,
            "exposure": self.exposure,
            "domain": "absolute luminance cd/m2, non-negative",
            "monotonic": True,
            "hue_handling": "single luminance ratio applied to all RGB channels",
            "channel_postprocess": "clamp each channel to [0, target_peak_nits]",
        }

    def _verify_monotonic(self) -> None:
        previous = -math.inf
        for index in range(4097):
            x = self.target_peak_nits * 16.0 * index / 4096.0
            value = self.map_luminance(x)
            if value + 1e-12 < previous:
                raise BusinessError(
                    "E_TONE_MAPPING",
                    "tone mapping curve is not monotonic in its declared domain",
                    {"sample_nits": x, "current": value, "previous": previous},
                )
            previous = value


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False
