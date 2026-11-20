"""Stage 5: error propagation along the reading chain.

Observation convention: the ground-truth `value` fields of the manifest
are the observation convention. For each stage we emit absolute
deviation, relative deviation, an error interval [min, max] of absolute
deviations, and an amplification ratio. The dominant error source among
tick_localization / calibration / point_localization is the level with
the largest mean contribution expressed in value units.

Traceability rules (checked here and re-checked by the self-check):
  * interval lower <= upper, otherwise E_ERROR_UNTRACEABLE;
  * every amplification ratio must be recomputable from the emitted
    deviation details, otherwise E_ERROR_UNTRACEABLE.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from . import codes
from .errors import BusinessError

STAGE = "error_propagation"
REL_EPS = sys.float_info.epsilon  # declared relative-deviation denominator floor


def _round_sig(x: Optional[float], precision: int) -> Optional[float]:
    """Deterministic significant-digit rounding for REPORTING of error
    statistics only (declared convention; never applied to inverted values)."""
    if x is None:
        return None
    if x == 0.0:
        return 0.0
    return float(f"{x:.{precision}g}")


def _rel(dev: float, truth: float) -> float:
    return abs(dev) / max(abs(truth), REL_EPS)


def _interval(devs: List[float], chart_id: str, level: str) -> List[float]:
    lo = min(devs)
    hi = max(devs)
    if lo > hi:  # defensive; cannot happen with min/max, kept for the contract
        raise BusinessError(
            codes.E_ERROR_UNTRACEABLE,
            f"error interval inverted at level {level}: [{lo}, {hi}]",
            chart_id=chart_id, stage=STAGE,
        )
    return [lo, hi]


def compute_error_spectrum(
    chart_id: str,
    truth: Dict[str, Any],
    tick_result: Dict[str, Any],
    calib: Dict[str, Any],
    matched_points: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """matched_points carry truth pixel/value plus detected pixel/value."""
    slope = calib["slope"]
    gain = abs(slope)  # value units per pixel, the pixel->value gain

    truth_ticks = sorted(truth["axis"]["tick_pixels"])
    tick_devs = [abs(d - t) for d, t in
                 zip(tick_result["tick_pixels"], truth_ticks)]
    tick_rel = [_rel(d - t, t) for d, t in
                zip(tick_result["tick_pixels"], truth_ticks)]

    calib_devs = [abs(r) for r in calib["residuals"]]
    calib_rel = [_rel(r, tv) for r, tv in
                 zip(calib["residuals"],
                     sorted(truth["axis"]["tick_values"], reverse=True))]

    pt_devs = [((m["pixel_x"] - m["truth_pixel_x"]) ** 2
                + (m["pixel_y"] - m["truth_pixel_y"]) ** 2) ** 0.5
               for m in matched_points]
    pt_rel = [_rel(m["pixel_y"] - m["truth_pixel_y"], m["truth_pixel_y"])
              for m in matched_points]

    inv_devs = [abs(m["value"] - m["truth_value"]) for m in matched_points]
    inv_rel = [_rel(m["value"] - m["truth_value"], m["truth_value"])
               for m in matched_points]

    mean_tick = sum(tick_devs) / len(tick_devs) if tick_devs else 0.0
    mean_calib = sum(calib_devs) / len(calib_devs) if calib_devs else 0.0
    mean_pt = sum(pt_devs) / len(pt_devs) if pt_devs else 0.0
    mean_inv = sum(inv_devs) / len(inv_devs) if inv_devs else 0.0

    denom = mean_pt * gain
    if denom > 0:
        inv_ratio: Optional[float] = mean_inv / denom
    else:
        inv_ratio = 0.0 if mean_inv == 0 else None

    contributions = {
        "tick_localization": mean_tick * gain,
        "calibration": mean_calib,
        "point_localization": mean_pt * gain,
    }
    dominant = max(contributions, key=lambda k: (contributions[k], k))

    p = cfg["error_stats_precision"]
    return {
        "chart_id": chart_id,
        "observation_convention": "ground_truth_value",
        "rel_eps": REL_EPS,
        "levels": {
            "tick_localization": {
                "domain": "pixel",
                "abs_deviations": [_round_sig(d, p) for d in tick_devs],
                "rel_deviations": [_round_sig(d, p) for d in tick_rel],
                "error_interval": [_round_sig(v, p) for v in
                                   _interval(tick_devs, chart_id, "tick")],
                "amplification_ratio": _round_sig(gain, p),
            },
            "calibration": {
                "domain": "value",
                "abs_deviations": [_round_sig(d, p) for d in calib_devs],
                "rel_deviations": [_round_sig(d, p) for d in calib_rel],
                "error_interval": [_round_sig(v, p) for v in
                                   _interval(calib_devs, chart_id, "calibration")],
                "amplification_ratio": 1.0,
            },
            "point_localization": {
                "domain": "pixel",
                "abs_deviations": [_round_sig(d, p) for d in pt_devs],
                "rel_deviations": [_round_sig(d, p) for d in pt_rel],
                "error_interval": [_round_sig(v, p) for v in
                                   _interval(pt_devs, chart_id, "point")],
                "amplification_ratio": _round_sig(gain, p),
            },
            "inversion": {
                "domain": "value",
                "abs_deviations": [_round_sig(d, p) for d in inv_devs],
                "rel_deviations": [_round_sig(d, p) for d in inv_rel],
                "error_interval": [_round_sig(v, p) for v in
                                   _interval(inv_devs, chart_id, "inversion")],
                "amplification_ratio": _round_sig(inv_ratio, p),
            },
        },
        "value_domain_contributions":
            {k: _round_sig(v, p) for k, v in contributions.items()},
        "dominant_source": dominant,
    }


def traceability_check(spectrum: Dict[str, Any], calib_slope: float,
                       chart_id: str) -> None:
    """Re-derive intervals and ratios from the emitted details."""
    levels = spectrum["levels"]
    for name, lvl in levels.items():
        devs = lvl["abs_deviations"]
        lo, hi = lvl["error_interval"]
        if lo > hi:
            raise BusinessError(
                codes.E_ERROR_UNTRACEABLE,
                f"error interval inverted at level {name}",
                chart_id=chart_id, stage=STAGE,
            )
        if devs and (min(devs) != lo or max(devs) != hi):
            raise BusinessError(
                codes.E_ERROR_UNTRACEABLE,
                f"error interval at level {name} not recomputable from details",
                chart_id=chart_id, stage=STAGE,
            )
    gain = abs(calib_slope)
    pt = levels["point_localization"]["abs_deviations"]
    inv = levels["inversion"]["abs_deviations"]
    mean_pt = sum(pt) / len(pt) if pt else 0.0
    mean_inv = sum(inv) / len(inv) if inv else 0.0
    denom = mean_pt * gain
    expected = (mean_inv / denom) if denom > 0 else (0.0 if mean_inv == 0 else None)
    got = levels["inversion"]["amplification_ratio"]
    if expected is None or got is None:
        if expected is not got:
            raise BusinessError(
                codes.E_ERROR_UNTRACEABLE,
                "inversion amplification ratio undefined but reported",
                chart_id=chart_id, stage=STAGE,
            )
    elif abs(expected - got) > 1e-6 * max(1.0, abs(expected)):
        raise BusinessError(
            codes.E_ERROR_UNTRACEABLE,
            f"inversion amplification ratio {got} not recomputable "
            f"(expected {expected})",
            chart_id=chart_id, stage=STAGE,
        )
