"""Stage 3: length-ratio and noise filtering with strict/tolerant thresholds.

Tolerant strategy uses a tolerance band with subnormal protection: values
flushed below the smallest normal float are treated as zero, the effective
tolerance is floored by several ulps of the boundary, and any value landing
inside the band is arbitrated by an exact Fraction comparison so decisions
at the threshold cannot be flipped by rounding across precision modes.
"""

import math
import sys
from dataclasses import dataclass, field
from fractions import Fraction

from .errors import Codes, PipelineError

_MIN_NORMAL = sys.float_info.min


def flush_subnormal(x):
    """Subnormal protection: flush subnormal magnitudes to zero."""
    return 0.0 if 0 < abs(x) < _MIN_NORMAL else x


def _effective_tol(boundary, tol):
    return max(tol, 8 * math.ulp(boundary))


@dataclass(frozen=True)
class FilterConfig:
    ratio_min: float = 0.5
    ratio_max: float = 2.0
    noise_max: float = 0.25
    strategy: str = "tolerant"
    tolerance: float = 1e-9
    mix_targets: dict = field(default_factory=dict)

    def validate(self):
        bad = None
        if self.strategy not in ("strict", "tolerant"):
            bad = f"unknown threshold strategy: {self.strategy!r}"
        elif not (math.isfinite(self.tolerance) and self.tolerance >= 0):
            bad = "tolerance must be a finite non-negative number"
        elif not all(map(math.isfinite, (self.ratio_min, self.ratio_max, self.noise_max))):
            bad = "thresholds must be finite"
        elif self.ratio_min > self.ratio_max:
            bad = "ratio_min must be <= ratio_max"
        elif self.noise_max < 0:
            bad = "noise_max must be >= 0"
        if bad:
            raise PipelineError(Codes.THRESHOLD_INVALID, bad)


def ratio_decision(src_pieces_len, tgt_pieces_len, cfg: FilterConfig):
    """Return (ok, ratio). Borderline values are arbitrated with exact
    Fraction arithmetic so the decision is rounding-flip proof."""
    ratio = flush_subnormal(tgt_pieces_len / src_pieces_len) if src_pieces_len else float("inf")
    if cfg.strategy == "strict":
        return cfg.ratio_min <= ratio <= cfg.ratio_max, ratio
    tol = max(_effective_tol(cfg.ratio_min, cfg.tolerance),
              _effective_tol(cfg.ratio_max, cfg.tolerance))
    near_lo = abs(ratio - cfg.ratio_min) <= tol
    near_hi = abs(ratio - cfg.ratio_max) <= tol
    if not (near_lo or near_hi):
        return cfg.ratio_min <= ratio <= cfg.ratio_max, ratio
    if src_pieces_len == 0:
        return False, ratio
    exact = Fraction(tgt_pieces_len, src_pieces_len)
    ok = Fraction(cfg.ratio_min) <= exact <= Fraction(cfg.ratio_max)
    return ok, ratio


def noise_decision(noise_level, cfg: FilterConfig):
    noise = flush_subnormal(float(noise_level))
    if cfg.strategy == "strict":
        return noise <= cfg.noise_max
    tol = _effective_tol(cfg.noise_max, cfg.tolerance)
    if abs(noise - cfg.noise_max) <= tol:
        return Fraction(noise) <= Fraction(cfg.noise_max)
    return noise <= cfg.noise_max


def run_filter(records, seg_pairs, dropped_by_dedup, cfg: FilterConfig):
    """Produce per-sample decision details, ordered by pair_id ascending."""
    cfg.validate()
    seg_by_id = {p["pair_id"]: p for p in seg_pairs}
    details = []
    for rec in records:
        pair_id = rec["pair_id"]
        seg = seg_by_id[pair_id]
        ratio_ok, ratio = ratio_decision(len(seg["src_pieces"]), len(seg["tgt_pieces"]), cfg)
        noise_ok = noise_decision(rec["noise_level"], cfg)
        reasons = []
        if pair_id in dropped_by_dedup:
            reasons.append("DUPLICATE")
        if not ratio_ok:
            reasons.append("RATIO")
        if not noise_ok:
            reasons.append("NOISE")
        details.append({
            "pair_id": pair_id,
            "domain_tag": rec["domain_tag"],
            "noise_level": rec["noise_level"],
            "ratio": ratio,
            "src_sub_len": len(seg["src_pieces"]),
            "tgt_sub_len": len(seg["tgt_pieces"]),
            "reasons": reasons,
            "decision": "drop" if reasons else "keep",
        })
    details.sort(key=lambda d: d["pair_id"])
    return details


def apply_mix(details, mix_targets):
    """Deterministic per-domain quota sampling on kept samples (pair_id asc)."""
    if not mix_targets:
        return details
    kept = [d for d in details if d["decision"] == "keep"]
    total = len(kept)
    quotas = {dom: int(prop * total) for dom, prop in mix_targets.items()}
    counts = {dom: 0 for dom in mix_targets}
    out = []
    for d in details:
        d = dict(d)
        if d["decision"] == "keep":
            dom = d["domain_tag"]
            if dom in quotas:
                if counts[dom] < quotas[dom]:
                    counts[dom] += 1
                else:
                    d["decision"] = "drop"
                    d["reasons"] = d["reasons"] + ["MIX_QUOTA"]
        out.append(d)
    return out


def metrics_from_details(details):
    """Metrics independently recomputed from per-sample details."""
    total = len(details)
    kept = [d for d in details if d["decision"] == "keep"]
    ratios = [d["ratio"] for d in details if math.isfinite(d["ratio"])]
    return {
        "total": total,
        "kept": len(kept),
        "dropped": total - len(kept),
        "keep_rate": (len(kept) / total) if total else 0.0,
        "ratio_stats": {
            "min": min(ratios) if ratios else 0.0,
            "max": max(ratios) if ratios else 0.0,
            "mean": (sum(ratios) / len(ratios)) if ratios else 0.0,
        },
    }
