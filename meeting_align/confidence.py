"""Per-word confidence interval propagation under timeline changes.

The time error of a word decomposes into three recorded terms:
seam_term (half the applied frame hop), calib_term (batch calibration
residual RMS) and shift_term (segment displacement).  The confidence
half-width is delta = (sum of terms) * confidence_decay_per_ms.  The model
and all parameters are emitted with the output; no hidden constant
tolerance is used.
"""

from __future__ import annotations

from .errors import IntervalInvalidError
from .util import round_ms

SOURCES = ("seam_localization", "batch_calibration", "segment_shift")


def propagate_word_intervals(mapped_segment, calibration, config, applied_hop):
    """Recompute per-word confidence intervals for a mapped segment."""
    seg = mapped_segment
    seam_term = applied_hop / 2.0 / config.sample_rate * 1000.0
    calib_term = float(calibration["residual_rms_ms"])
    shift_term = abs(float(seg["shift_ms"]))
    total = seam_term + calib_term + shift_term
    terms = {
        "seam_localization": round_ms(seam_term),
        "batch_calibration": round_ms(calib_term),
        "segment_shift": round_ms(shift_term),
    }
    dominant = max(SOURCES, key=lambda s: (terms[s], -SOURCES.index(s)))
    base = terms["seam_localization"] if terms["seam_localization"] > 0 else 1.0
    amplification = round_ms(total / base)
    delta = total * config.confidence_decay_per_ms

    words = []
    for w in seg["words"]:
        offset = w["offset_ms"]
        if offset < 0 or offset > seg["local_duration_ms"]:
            raise IntervalInvalidError(
                f"word offset {offset} outside segment {seg['id']} range",
                segment_id=seg["id"],
                word=w["text"],
                offset_ms=offset,
                segment_duration_ms=seg["local_duration_ms"],
            )
        lo = round(max(0.0, w["confidence"] - delta), 6)
        hi = round(min(1.0, w["confidence"] + delta), 6)
        if lo > hi:
            raise IntervalInvalidError(
                f"illegal interval [{lo}, {hi}] for word {w['text']}",
                segment_id=seg["id"],
                word=w["text"],
                lo=lo,
                hi=hi,
            )
        words.append(
            {
                "text": w["text"],
                "offset_ms": offset,
                "global_ms": w["global_ms"],
                "confidence": w["confidence"],
                "interval": [lo, hi],
            }
        )

    return {
        "segment_id": seg["id"],
        "interval_model": {
            "formula": "delta=(seam+calibration+shift)*confidence_decay_per_ms",
            "confidence_decay_per_ms": config.confidence_decay_per_ms,
            "terms_ms": terms,
        },
        "time_error_ms": round_ms(total),
        "amplification_ratio": amplification,
        "dominant_source": dominant,
        "words": words,
    }
