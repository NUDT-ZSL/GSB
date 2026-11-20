"""Configuration loading and validation.

All numeric口径 (precision conventions) are declared here and echoed into
the report so that no rounding / truncation ever happens off the record.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict

from . import codes
from .errors import BusinessError

THRESHOLD_STRATEGIES = ("strict", "tolerance_band")

DEFAULTS: Dict[str, Any] = {
    "seed": 20261120,
    "canvas": {"width": 640, "height": 480},
    # Pixel convention: integer pixel indices, origin top-left, y grows down.
    "pixel_convention": "integer_pixel_top_left",
    "axis_black_threshold": 64,      # channel max below this counts as axis black
    "tick_length": 5,                # tick mark length in pixels (must match generator)
    "tick_min_dark_run": 4,          # min dark pixels in tick band to accept a tick row
    "min_tick_count": 2,             # calibration needs at least two ticks
    "candidate_merge_distance": 3.0, # px; candidates closer than this are merged
    "max_candidates_per_series": 64,
    "marker_size": 5,                # marker block edge in px (must match generator)
    "min_credible_threshold": 0.0,
    "threshold_tolerance": 1e-9,
    "threshold_strategy": "strict",
    "ulps_tolerance": 4,             # ulp floor for tolerance-band comparisons
    "clip_to_axis_domain": True,     # clip inverted values to [axis_min, axis_max]
    "error_stats_precision": 12,     # significant digits for error stats in report
    "recompute_atol": 1e-9,          # self-check tolerance for inversion recompute
}


def _fail(field: str, message: str) -> BusinessError:
    return BusinessError(
        codes.E_CONFIG_INVALID,
        message,
        stage="config",
        field=field,
    )


def validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate config; raise E_CONFIG_INVALID naming the offending field."""
    merged = dict(DEFAULTS)
    merged.update(cfg)

    threshold = merged["min_credible_threshold"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
            or not math.isfinite(threshold):
        raise _fail("min_credible_threshold",
                    f"min_credible_threshold must be a finite number, got {threshold!r}")

    tolerance = merged["threshold_tolerance"]
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) \
            or not math.isfinite(tolerance) or tolerance < 0:
        raise _fail("threshold_tolerance",
                    f"threshold_tolerance must be a finite number >= 0, got {tolerance!r}")

    strategy = merged["threshold_strategy"]
    if strategy not in THRESHOLD_STRATEGIES:
        raise _fail("threshold_strategy",
                    f"threshold_strategy must be one of {THRESHOLD_STRATEGIES}, got {strategy!r}")

    for field in ("candidate_merge_distance", "recompute_atol"):
        value = merged[field]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise _fail(field, f"{field} must be a finite number >= 0, got {value!r}")

    for field in ("axis_black_threshold", "tick_length", "tick_min_dark_run",
                  "min_tick_count", "max_candidates_per_series", "marker_size",
                  "ulps_tolerance", "error_stats_precision", "seed"):
        value = merged[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _fail(field, f"{field} must be a non-negative integer, got {value!r}")

    if merged["min_tick_count"] < 2:
        raise _fail("min_tick_count", "min_tick_count must be >= 2 for linear calibration")

    canvas = merged["canvas"]
    if not isinstance(canvas, dict) \
            or int(canvas.get("width", 0)) <= 0 or int(canvas.get("height", 0)) <= 0:
        raise _fail("canvas", "canvas.width/height must be positive integers")

    if merged["threshold_strategy"] not in THRESHOLD_STRATEGIES:
        raise _fail("threshold_strategy", "unknown threshold strategy")

    return merged


def load_config(path: str | Path) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    return validate_config(json.loads(text))
