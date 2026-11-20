"""Stage 6: stable numeric ordering and threshold judgement.

Stability protections:
  * inverted values are never rounded before comparison;
  * two strategies: `strict` (exact >=) and `tolerance_band`
    (>= threshold - effective_tolerance);
  * subnormal protection: the effective tolerance is floored at
    `ulps_tolerance` ulps of the operands (math.ulp handles subnormals),
    so points sitting exactly on the threshold can never flip verdict
    across repeated runs due to rounding;
  * sorting is by (value, pixel_x) with a total order, so equal values
    keep a deterministic order.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

STAGE = "sorting"


def effective_tolerance(value: float, threshold: float, cfg: Dict[str, Any]) -> float:
    ulp_floor = cfg["ulps_tolerance"] * max(
        math.ulp(value),
        math.ulp(threshold),
        math.ulp(0.0),
    )
    return max(cfg["threshold_tolerance"], ulp_floor)


def judge_threshold(value: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    threshold = cfg["min_credible_threshold"]
    strategy = cfg["threshold_strategy"]
    tol = effective_tolerance(value, threshold, cfg)
    if strategy == "strict":
        credible = value >= threshold
    else:
        credible = value >= threshold - tol
    return {
        "credible": credible,
        "strategy": strategy,
        "effective_tolerance": tol,
    }


def sort_series_points(points: List[Dict[str, Any]], cfg: Dict[str, Any]
                       ) -> List[Dict[str, Any]]:
    """Sort one series' inverted points by (value, pixel_x); attach
    threshold judgement and rank. Total order => deterministic."""
    ordered = sorted(points, key=lambda p: (p["value"], p["pixel_x"]))
    out = []
    for rank, p in enumerate(ordered):
        verdict = judge_threshold(p["value"], cfg)
        out.append({
            "rank": rank,
            "series_id": p["series_id"],
            "pixel_x": p["pixel_x"],
            "pixel_y": p["pixel_y"],
            "value": p["value"],
            "credible": verdict["credible"],
            "effective_tolerance": verdict["effective_tolerance"],
        })
    return out
