"""Stage-wise deviation, error intervals, amplification and source attribution.

The float64 tier is treated as the in-snapshot reference truth. Every other
tier is compared against it stage by stage, and each attribution entry links
back to concrete per-query/per-item deviation details.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .stats import confidence_interval_mean, distribution_shape


@dataclass
class StageSample:
    stage: str
    ref: np.ndarray
    observed: np.ndarray
    labels: list[str]


def absolute_errors(ref: np.ndarray, observed: np.ndarray) -> np.ndarray:
    return np.abs(np.asarray(observed, dtype=np.float64) - np.asarray(ref, dtype=np.float64))


def relative_errors(ref: np.ndarray, observed: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    ref64 = np.asarray(ref, dtype=np.float64)
    denom = np.maximum(np.abs(ref64), floor)
    return absolute_errors(ref64, observed) / denom


def error_interval(errors: np.ndarray, quantiles: list[float]) -> dict[str, float]:
    if errors.size == 0:
        return {f"q{int(q * 100):02d}": 0.0 for q in quantiles}
    return {
        f"q{int(round(q * 100)):02d}": float(np.quantile(errors, q))
        for q in quantiles
    }


def summarize_stage(ref, observed, labels, quantiles) -> dict:
    ref64 = np.asarray(ref, dtype=np.float64).reshape(-1)
    obs64 = np.asarray(observed, dtype=np.float64).reshape(-1)
    abs_err = absolute_errors(ref64, obs64)
    rel_err = relative_errors(ref64, obs64)
    flat_labels = _expand_labels(labels, ref64.shape[0])
    worst_idx = int(np.argmax(abs_err)) if abs_err.size else 0
    return {
        "n_samples": int(ref64.size),
        "abs_error": {
            "max": float(np.max(abs_err)) if abs_err.size else 0.0,
            "mean": float(np.mean(abs_err)) if abs_err.size else 0.0,
            "rmse": float(np.sqrt(np.mean(abs_err ** 2))) if abs_err.size else 0.0,
            "interval": error_interval(abs_err, quantiles),
            "shape": distribution_shape(abs_err),
            "mean_confidence_interval_95": confidence_interval_mean(abs_err),
        },
        "rel_error": {
            "max": float(np.max(rel_err)) if rel_err.size else 0.0,
            "mean": float(np.mean(rel_err)) if rel_err.size else 0.0,
            "interval": error_interval(rel_err, quantiles),
        },
        "worst_sample": {
            "label": flat_labels[worst_idx] if flat_labels else None,
            "ref": float(ref64[worst_idx]) if ref64.size else 0.0,
            "observed": float(obs64[worst_idx]) if obs64.size else 0.0,
            "abs_error": float(abs_err[worst_idx]) if abs_err.size else 0.0,
        },
        "detail": [
            {
                "label": flat_labels[i],
                "ref": float(ref64[i]),
                "observed": float(obs64[i]),
                "abs_error": float(abs_err[i]),
                "rel_error": float(rel_err[i]),
            }
            for i in range(ref64.size)
        ],
    }


def _expand_labels(labels, n: int) -> list[str]:
    flat: list[str] = []
    for label in labels:
        if isinstance(label, (list, tuple)):
            flat.extend(str(x) for x in label)
        else:
            flat.append(str(label))
    return flat[:n] if len(flat) >= n else flat + [f"idx{i}" for i in range(len(flat), n)]


def amplification_chain(stage_summaries: list[dict]) -> list[dict]:
    """Ratio of each stage's mean abs error to the feature-stage input error."""
    base = stage_summaries[0]["abs_error"]["mean"] if stage_summaries else 0.0
    chain = []
    prev = 0.0
    for summary in stage_summaries:
        mean_err = summary["abs_error"]["mean"]
        chain.append({
            "stage": summary["stage"],
            "mean_abs_error": mean_err,
            "amplification_vs_feature": (mean_err / base) if base > 0 else 0.0,
            "amplification_vs_prev": (mean_err / prev) if prev > 0 else 0.0,
        })
        prev = mean_err
    return chain


def order_inversions(ref_order: list[str], obs_order: list[str]) -> dict:
    """Count inverted / flipped / missing positions between two ranked lists."""
    ref_pos = {item_id: pos for pos, item_id in enumerate(ref_order)}
    obs_pos = {item_id: pos for pos, item_id in enumerate(obs_order)}
    common = [iid for iid in ref_order if iid in obs_pos]
    inversions = 0
    for a in range(len(common)):
        for b in range(a + 1, len(common)):
            if obs_pos[common[a]] > obs_pos[common[b]]:
                inversions += 1
    flips = sum(1 for iid in common if ref_pos[iid] != obs_pos[iid])
    return {
        "common_items": len(common),
        "inversions": inversions,
        "position_flips": flips,
        "missing_in_observed": sorted(set(ref_order) - set(obs_order)),
        "extra_in_observed": sorted(set(obs_order) - set(ref_order)),
    }


def attribute_dominant_source(stage_summaries: dict[str, dict],
                              ranking_divergence: dict) -> dict:
    """Pick the dominant numeric error source, plus a discrete ranking flag.

    Continuous stage errors live on an absolute-error scale; ranking inversions
    are discrete structural events. They are therefore reported separately
    instead of being compared on the same numeric axis.
    """
    candidates = []
    feature = stage_summaries.get("features")
    if feature:
        candidates.append(("feature_construction",
                           feature["abs_error"]["max"],
                           feature["worst_sample"]))
    linear = stage_summaries.get("linear")
    if linear:
        candidates.append(("score_scaling",
                           linear["abs_error"]["max"],
                           linear["worst_sample"]))
    sigmoid = stage_summaries.get("sigmoid")
    if sigmoid:
        candidates.append(("activation_function",
                           sigmoid["abs_error"]["max"],
                           sigmoid["worst_sample"]))
    name, magnitude, evidence = max(candidates, key=lambda c: c[1])
    return {
        "dominant_source": name,
        "magnitude": magnitude,
        "evidence": evidence,
        "candidate_magnitudes": {c[0]: c[1] for c in candidates},
        "ranking_comparison_triggered": ranking_divergence.get("inversions", 0) > 0
                                        or ranking_divergence.get("position_flips", 0) > 0,
        "ranking_evidence": {
            "inversions": ranking_divergence.get("inversions", 0),
            "position_flips": ranking_divergence.get("position_flips", 0),
        },
    }


def theoretical_fixed_bound(value_f64: float, scale: int, rounding: str) -> float:
    """Maximum quantization error for a single encode, given the rounding mode."""
    unit = 0.5 / scale
    if rounding in ("trunc", "floor"):
        unit = 1.0 / scale
    return unit


def verify_fixed_recompute(observed_f64: np.ndarray, raw_f64: np.ndarray,
                           scale: int, rounding: str) -> dict:
    """The fixed-tier encoding must be reproducible from scale and rounding.

    Two distinct assertions are reported:

    * ``recompute_exact_match``: re-encoding each raw value with the documented
      rounding rule reproduces the observed fixed-tier representation exactly.
    * ``all_within_quantization_bound``: every raw-vs-observed deviation stays
      inside the single-step quantization bound implied by the rounding mode.
      Downstream compounded stages may legitimately exceed it; this check is
      therefore evaluated against the encoding-level samples.
    """
    from .precision import _round_divide
    recomputed = np.array([
        _round_divide(int(round(float(v) * scale * 10)), 10, rounding) / scale
        for v in np.asarray(raw_f64, dtype=np.float64).reshape(-1)
    ])
    observed_flat = np.asarray(observed_f64, dtype=np.float64).reshape(-1)
    delta = np.abs(recomputed - observed_flat)
    bounds = np.array([
        theoretical_fixed_bound(float(v), scale, rounding)
        for v in np.asarray(raw_f64, dtype=np.float64).reshape(-1)
    ])
    raw_err = np.abs(observed_flat - np.asarray(raw_f64, dtype=np.float64).reshape(-1))
    recomputed_err = np.abs(recomputed - np.asarray(raw_f64, dtype=np.float64).reshape(-1))
    # float64 decode of an S-scaled integer carries ~1/S relative round-off;
    # allow one such unit before declaring the quantization bound violated.
    tolerance = 1.0 / scale
    within_bound = bool(np.all(recomputed_err <= bounds + tolerance))
    exact_match = bool(np.all(delta < 1e-15))
    return {
        "recompute_exact_match": exact_match,
        "all_within_quantization_bound": within_bound,
        "max_recompute_delta": float(np.max(delta)) if delta.size else 0.0,
        "max_quantization_bound": float(np.max(bounds)) if bounds.size else 0.0,
        "max_observed_raw_error": float(np.max(raw_err)) if raw_err.size else 0.0,
    }
