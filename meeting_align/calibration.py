"""Cross-batch timeline calibration.

Solves, per batch, a global origin offset and scale so that seam candidates
of both batches land on one global axis with minimal residual:
global_ms = scale_k * local_ms + offset_k.  local_ms uses the batch's
declared sample rate; global_ms uses the configured (applied) sample rate.
"""

from __future__ import annotations

from .errors import SampleRateMismatch
from .util import hash_obj, round_ms


def check_declared_rate(declared_rate, applied_rate, tolerance, batch_id):
    rel = abs(declared_rate - applied_rate) / float(applied_rate)
    if rel > tolerance:
        raise SampleRateMismatch(
            f"batch {batch_id} declares {declared_rate} Hz, applied {applied_rate} Hz",
            batch_id=batch_id,
            declared_sample_rate=declared_rate,
            applied_sample_rate=applied_rate,
            tolerance=tolerance,
        )


def _match_seams(local_candidates, global_candidates, batch_start_sample,
                 declared_rate, applied_rate, match_tolerance_ms):
    """Greedy order-preserving nearest matching in the sample domain."""
    approx_scale = declared_rate / float(applied_rate)
    tol_samples = match_tolerance_ms * applied_rate / 1000.0
    pairs = []
    used = set()
    for cand in local_candidates:
        approx_global = batch_start_sample + cand["sample_index"] / approx_scale
        best, best_dist = None, None
        for gi, gcand in enumerate(global_candidates):
            if gi in used:
                continue
            dist = abs(gcand["sample_index"] - approx_global)
            if dist <= tol_samples and (best_dist is None or dist < best_dist):
                best, best_dist = gi, dist
        if best is not None:
            used.add(best)
            local_ms = cand["sample_index"] / declared_rate * 1000.0
            global_ms = global_candidates[best]["sample_index"] / applied_rate * 1000.0
            pairs.append((local_ms, global_ms))
    pairs.sort()
    return pairs


def _least_squares(pairs):
    n = len(pairs)
    sx = sum(p[0] for p in pairs)
    sy = sum(p[1] for p in pairs)
    sxx = sum(p[0] * p[0] for p in pairs)
    sxy = sum(p[0] * p[1] for p in pairs)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return a, b


def calibrate_batch(batch_id, local_seams, global_seams, batch_start_sample,
                    declared_rate, config):
    """Solve offset/scale for one batch; fully deterministic."""
    check_declared_rate(
        declared_rate, config.sample_rate, config.rate_tolerance, batch_id
    )
    pairs = _match_seams(
        local_seams["candidates"],
        global_seams["candidates"],
        batch_start_sample,
        declared_rate,
        config.sample_rate,
        config.match_tolerance_ms,
    )
    fallback_offset = batch_start_sample / config.sample_rate * 1000.0
    fallback_scale = declared_rate / float(config.sample_rate)
    if len(pairs) >= 2 and _least_squares(pairs) is not None:
        method = "least_squares"
        scale, offset = _least_squares(pairs)
    elif len(pairs) == 1:
        method = "single_anchor"
        scale = fallback_scale
        offset = pairs[0][1] - scale * pairs[0][0]
    else:
        method = "fallback"
        scale, offset = fallback_scale, fallback_offset
    residuals = [round_ms(g - (scale * l + offset)) for l, g in pairs]
    rms = round_ms(
        (sum(r * r for r in residuals) / len(residuals)) ** 0.5 if residuals else 0.0
    )
    return {
        "batch_id": batch_id,
        "scale": round_ms(scale),
        "offset_ms": round_ms(offset),
        "method": method,
        "declared_sample_rate": declared_rate,
        "applied_sample_rate": config.sample_rate,
        "anchor_pairs": [
            {"local_ms": round_ms(l), "global_ms": round_ms(g)} for l, g in pairs
        ],
        "residuals_ms": residuals,
        "residual_rms_ms": rms,
    }


def calibrate_all(batch_order, batch_seams, global_seams, batch_starts,
                  declared_rates, config):
    """Calibrate every batch; returns calibrations plus a content version."""
    result = {}
    for batch_id in batch_order:
        result[batch_id] = calibrate_batch(
            batch_id,
            batch_seams[batch_id],
            global_seams,
            batch_starts[batch_id],
            declared_rates[batch_id],
            config,
        )
    return {"calibrations": result, "version": hash_obj(result)}
