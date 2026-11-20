"""Stage 2: linear calibration from tick pixels to values, and inversion.

Precision convention (declared, never silent):
  * Slope/intercept are solved in IEEE-754 binary64 via the closed-form
    normal equations: slope = Sxy / Sxx, intercept = vy - slope * px.
  * Inverted values are NEVER rounded or truncated. The only
    post-processing is optional clipping to [axis_min, axis_max], applied
    only when config `clip_to_axis_domain` is true, and every clip event
    is recorded on the point record.
  * Degenerate calibration (all tick pixels coincide, Sxx == 0) returns
    E_CALIB_DEGENERATE naming the chart_id.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import codes
from .errors import BusinessError

STAGE = "calibration"

PRECISION_POLICY = {
    "solve": "closed_form_normal_equations",
    "float_format": "IEEE754_binary64",
    "rounding": "none",
    "truncation": "none",
    "clipping": "clip_to_[axis_min,axis_max]_iff_config.clip_to_axis_domain",
}


def fit_calibration(tick_pixels: List[float], tick_values: List[float],
                    axis_min: float, axis_max: float, chart_id: str,
                    cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not (axis_max > axis_min):
        raise BusinessError(
            codes.E_AXIS_RANGE_INVALID,
            f"axis range non-positive: [{axis_min}, {axis_max}]",
            chart_id=chart_id, stage=STAGE,
            detail={"axis_min": axis_min, "axis_max": axis_max},
        )
    # Pairing convention: pixel y grows downward while value grows upward,
    # so the mapping is anti-monotonic. Detected pixels are sorted
    # ascending and paired with tick values sorted descending.
    pairs = sorted(zip(sorted(tick_pixels),
                       sorted(tick_values, reverse=True)))
    tick_pixels = [p for p, _ in pairs]
    tick_values = [v for _, v in pairs]
    n = len(tick_pixels)
    if n < cfg["min_tick_count"]:
        raise BusinessError(
            codes.E_CALIB_DEGENERATE,
            f"only {n} ticks, need >= {cfg['min_tick_count']}",
            chart_id=chart_id, stage=STAGE,
        )

    mean_p = sum(tick_pixels) / n
    mean_v = sum(tick_values) / n
    sxx = sum((p - mean_p) ** 2 for p in tick_pixels)
    sxy = sum((p - mean_p) * (v - mean_v)
              for p, v in zip(tick_pixels, tick_values))
    if sxx == 0.0:
        raise BusinessError(
            codes.E_CALIB_DEGENERATE,
            "tick pixels coincide; slope undefined",
            chart_id=chart_id, stage=STAGE,
            detail={"tick_pixels": list(tick_pixels)},
        )
    slope = sxy / sxx
    intercept = mean_v - slope * mean_p

    residuals = [slope * p + intercept - v
                 for p, v in zip(tick_pixels, tick_values)]

    return {
        "chart_id": chart_id,
        "slope": slope,
        "intercept": intercept,
        "axis_min": axis_min,
        "axis_max": axis_max,
        "n_ticks": n,
        "residuals": residuals,
        "precision_policy": PRECISION_POLICY,
    }


def invert(pixel_y: float, calib: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """value = slope * pixel_y + intercept; optional recorded clipping."""
    value = calib["slope"] * pixel_y + calib["intercept"]
    clipped = False
    if cfg["clip_to_axis_domain"]:
        lo, hi = calib["axis_min"], calib["axis_max"]
        if value < lo:
            value, clipped = lo, True
        elif value > hi:
            value, clipped = hi, True
    return {"value": value, "clipped": clipped}
