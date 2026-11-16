"""Integer fixed-point arithmetic for the Score stage.

All plugin raw scores are integers in [0, 100]. Weights are positive
integer ratios. The weighted, normalized total is computed on an
integer grid of GRID units per score point, using floor division only.
No floating point value ever influences the final ranking.

Error bound derivation
----------------------
The exact normalized score on the grid would be

    T = sum(raw_i * w_i * GRID / W)        (real value, W = sum of w_i)

The computed value is

    C = sum(floor(raw_i * w_i * GRID / W))

Each floor loses a value in [0, 1) grid unit, so with n available
plugins:

    0 <= T - C < n        (grid units)

Hence the per-node error bound is `n` grid units, i.e. `n / GRID`
score points. Two nodes a and b are INDISTINGUISHABLE when

    |C_a - C_b| < bound_a + bound_b

because the true ordering may differ from the computed one by up to
the sum of both truncation bounds.
"""

import numpy as np

GRID = 100  # fixed-point grid: units per score point


def weighted_sum(contributions):
    """Compute the fixed-point weighted normalized sum.

    contributions: list of dicts with keys ``plugin``, ``raw`` (int
    0..100) and ``weight`` (positive int). Returns
    ``(total_units, error_bound_units, details)`` where details carry
    each plugin's integer contribution in grid units.
    """
    total_weight = 0
    for c in contributions:
        if not (0 <= c["raw"] <= 100):
            raise ValueError(f"raw score out of range: {c}")
        if c["weight"] <= 0:
            raise ValueError(f"weight must be positive: {c}")
        total_weight += c["weight"]
    if total_weight == 0:
        return 0, 0, []

    total = np.int64(0)
    details = []
    for c in contributions:
        numer = np.int64(c["raw"]) * np.int64(c["weight"]) * np.int64(GRID)
        contrib = int(numer // np.int64(total_weight))
        total += contrib
        details.append({
            "plugin": c["plugin"],
            "raw": c["raw"],
            "weight": c["weight"],
            "contribution_units": contrib,
        })
    error_bound_units = len(contributions)  # each floor loses < 1 unit
    return int(total), error_bound_units, details


def units_to_points(units):
    """Human-readable score points, e.g. 7250 -> '72.50'."""
    return f"{units // GRID}.{units % GRID:02d}"


def indistinguishable(units_a, bound_a, units_b, bound_b):
    """True when the score gap is strictly inside the combined bound."""
    return abs(units_a - units_b) < (bound_a + bound_b)
