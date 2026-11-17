"""Base-frequency / scale-factor boundary sensitivity diagnostics."""

import numpy as np

from .pipeline import compute_encode, compute_score
from .schemes import precision_dtype

BASE_MULTIPLIERS = (0.8, 0.9, 1.0, 1.1, 1.25)
SCALE_MULTIPLIERS = (0.5, 0.75, 1.0, 1.5, 2.0)


def boundary_check(scheme, base, scale, dim, precision):
    """Detect dimension-level truncation / scaling overflow of frequencies."""
    theta_ref = scheme.frequencies(base, scale, dim)
    theta_prec = theta_ref.astype(precision_dtype(precision))
    kinds = []
    if not (np.all(np.isfinite(theta_ref)) and np.all(np.isfinite(theta_prec))):
        kinds.append("scale_overflow")
    if np.any((theta_prec == 0.0) & (theta_ref != 0.0)):
        kinds.append("dim_truncation")
    return kinds, theta_ref


def _mean_extrap_deviation(scheme, config, samples, base, scale):
    deviations = []
    for sample in samples:
        encode = compute_encode(scheme, sample, config, base, scale)
        score = compute_score(scheme, sample, config, encode)
        for idx, in_range in enumerate(score["in_range"]):
            if not in_range:
                deviations.append(abs(score["scores"][idx]
                                      - score["scores_ref"][idx]))
    if not deviations:
        return 0.0
    return float(np.mean(np.asarray(deviations, dtype=np.float64)))


def scan_scheme(scheme, config, samples):
    """Scan base/scale neighbourhoods; rank parameters by sensitivity."""
    dim = config["dim"]
    precision = config["precision"]
    params = {}
    boundary_samples = []
    for param, multipliers in (("base_freq", BASE_MULTIPLIERS),
                               ("scale_factor", SCALE_MULTIPLIERS)):
        entries = []
        for multiplier in multipliers:
            base = config["base_freq"]
            scale = config["scale_factor"]
            if param == "base_freq":
                base = base * multiplier
            else:
                scale = scale * multiplier
            kinds, theta_ref = boundary_check(scheme, base, scale, dim, precision)
            deviation = _mean_extrap_deviation(
                scheme, config, samples, base, scale)
            entries.append({
                "multiplier": multiplier,
                "value": base if param == "base_freq" else scale,
                "mean_abs_deviation": deviation,
                "boundary_kinds": kinds,
            })
            if kinds and samples:
                probe = samples[0]
                encode = compute_encode(scheme, probe, config, base, scale)
                score = compute_score(scheme, probe, config, encode)
                boundary_samples.append({
                    "scheme": scheme.name,
                    "parameter": param,
                    "value": base if param == "base_freq" else scale,
                    "boundary_kinds": kinds,
                    "sample_id": probe["sample_id"],
                    "position": int(probe["query_pos"]),
                    "float64_reference": {
                        "frequencies": theta_ref.tolist(),
                        "scores": score["scores_ref"],
                    },
                })
        deviations = [e["mean_abs_deviation"] for e in entries]
        params[param] = {
            "entries": entries,
            "sensitivity": float(max(deviations) - min(deviations)),
        }
    ranking = sorted(params, key=lambda p: (-params[p]["sensitivity"], p))
    return {
        "scheme": scheme.name,
        "parameters": params,
        "ranking": ranking,
        "boundary_samples": boundary_samples,
    }
