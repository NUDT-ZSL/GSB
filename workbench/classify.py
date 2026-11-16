"""Deterministic feature-based category scoring and confidence computation."""

from __future__ import annotations

from .features import RegionFeatures


def classify_region(feat: RegionFeatures, config) -> tuple[str, int, dict]:
    """Weighted scoring per category; highest score wins.

    Ties resolve by category name in lexicographic order (categories are
    visited sorted and only a strictly greater score replaces the incumbent).
    Confidence is an integer permille clipped to [0, 1000].
    Returns (category, conf_permille, score_breakdown).
    """
    img_area = config["image"]["width"] * config["image"]["height"]
    breakdown: dict[str, dict] = {}
    best_cat, best_score = None, None
    for cat in sorted(config["categories"]):
        spec = config["categories"][cat]
        weights = spec["weights"]
        wsum = sum(weights.values())
        parts, total = {}, 0.0
        for fname in sorted(weights):
            fval = feat.feature_value(fname, img_area)
            target = spec["targets"][fname]
            tol = config["tolerance"][fname]
            contrib = weights[fname] * max(0.0, 1.0 - abs(fval - target) / tol)
            parts[fname] = contrib
            total += contrib
        score = total / wsum
        breakdown[cat] = {**parts, "total": score}
        if best_score is None or score > best_score + 1e-12:
            best_cat, best_score = cat, score
    conf = int(round(best_score * 1000.0))
    conf = max(0, min(1000, conf))
    return best_cat, conf, breakdown
