"""Calibration metrics with explicitly recorded conventions (requirement 3).

Recorded conventions (embedded in every metrics payload):
  * ECE bins: equal-width edges linspace(0, 1, n_bins + 1)
  * bin assignment: min(int(p * n_bins), n_bins - 1) on raw probabilities
  * ECE weight: bin_count / N (no smoothing, no truncation)
  * Brier: plain mean of (p - y)^2 over all eval samples
All metrics are exactly recomputable from the per-sample probability and
label detail tables.
"""

from __future__ import annotations

CONVENTIONS = {
    "ece_binning": "equal_width",
    "ece_bin_rule": "min(int(p*n_bins), n_bins-1)",
    "ece_weight": "bin_count/N",
    "brier": "mean((p-y)^2)",
    "smoothing": "none",
}


def _bin_index(p, n_bins):
    return min(int(p * n_bins), n_bins - 1)


def reliability_curve(probs, y, n_bins):
    edges = [i / n_bins for i in range(n_bins + 1)]
    counts = [0] * n_bins
    sum_p = [0.0] * n_bins
    sum_y = [0.0] * n_bins
    for p, lbl in zip(probs, y):
        b = _bin_index(p, n_bins)
        counts[b] += 1
        sum_p[b] += p
        sum_y[b] += lbl
    n = len(probs)
    bins = []
    for i in range(n_bins):
        c = counts[i]
        bins.append({
            "bin": i,
            "lo": edges[i],
            "hi": edges[i + 1],
            "count": c,
            "weight": c / n if n else 0.0,
            "mean_pred": sum_p[i] / c if c else None,
            "mean_true": sum_y[i] / c if c else None,
        })
    return {"n_bins": n_bins, "edges": edges, "bins": bins}


def ece(probs, y, n_bins):
    curve = reliability_curve(probs, y, n_bins)
    total = 0.0
    for b in curve["bins"]:
        if b["count"]:
            total += b["weight"] * abs(b["mean_pred"] - b["mean_true"])
    return total, curve


def brier(probs, y):
    n = len(probs)
    if n == 0:
        return 0.0
    return sum((p - lbl) ** 2 for p, lbl in zip(probs, y)) / n


def compute_metrics(probs, y, n_bins):
    """Full metric bundle for one probability/label detail table."""
    ece_val, curve = ece(probs, y, n_bins)
    return {
        "n": len(probs),
        "ece": ece_val,
        "brier": brier(probs, y),
        "reliability": curve,
        "conventions": dict(CONVENTIONS),
    }
