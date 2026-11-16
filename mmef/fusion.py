"""Fixed-point fusion chain: quantization, weighted fusion, softmax,
per-round convergence and dual-precision error tracking (requirements 2-5).

Every sample is computed twice over the identical chain: once at the
configured fractional bit width (``frac_bits``) and once at one bit higher
(``frac_bits + 1``). Differences between the two runs are reported at three
stages: feature quantization, softmax exp+normalization, weighted sum.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import E_FUSION, E_QUANTIZE
from .errors import StageError
from .fixedpoint import fp_softmax, quantize_scalar, round_half_away

# Fixed deterministic projection from (region mean, region gradient) to
# per-class visual logits. Defined in source so the chain is reproducible.
VIS_COEFS = {
    "supported": (0.4, 0.2),
    "refuted": (-0.3, 0.5),
    "uncertain": (0.3, -0.2),
}
DEFAULT_COEF = (0.1, 0.1)

STAGES = ("quantization", "softmax", "weighted_sum")

RELATION_TYPES = ("associate", "conflict", "supplement")


def extract_visual_features(image: np.ndarray, feature_dim: int):
    """Deterministic horizontal-strip blocking of the image.

    Returns a list of ``(mean, gradient)`` float pairs in [0, 1], one per
    region. mean = average gray level; gradient = mean absolute horizontal
    difference (gradient strength).
    """
    h = image.shape[0]
    feats = []
    for i in range(feature_dim):
        r0 = i * h // feature_dim
        r1 = (i + 1) * h // feature_dim
        region = image[r0:r1].astype(np.float64) / 255.0
        mean = float(region.mean())
        grad = float(np.abs(np.diff(region, axis=1)).mean())
        feats.append((mean, grad))
    return feats


def first_exceed_stage(diffs: dict, tolerance: int):
    """First stage (in listed order) whose diff exceeds the tolerance."""
    for stage in STAGES:
        if diffs.get(stage, 0) > tolerance:
            return stage
    return None


def is_boundary_sensitive(conf_lo, conf_hi, thr_lo, thr_hi, margin_lo, margin_hi) -> bool:
    """Confidence inside the threshold neighborhood and the two precision
    levels disagree on the threshold comparison.

    Thresholds and margins are the actual per-precision quantized values
    (they are NOT exact shifts of each other, which is precisely why
    boundary sensitivity occurs).
    """
    near = abs(conf_lo - thr_lo) <= margin_lo or abs(conf_hi - thr_hi) <= margin_hi
    disagree = (conf_lo >= thr_lo) != (conf_hi >= thr_hi)
    return near and disagree


@dataclass
class PreparedSample:
    name: str
    classes: list
    bits_lo: int
    bits_hi: int
    base: dict  # "lo"/"hi" -> per-class base logits (fixed-point ints)
    cur: dict  # "lo"/"hi" -> current per-class logits (round state)
    temp_fp: dict  # "lo"/"hi" -> fusion temperature (fixed-point)
    thr_fp: dict  # "lo"/"hi" -> confidence threshold (fixed-point)
    margin_fp: dict  # "lo"/"hi" -> boundary margin (fixed-point)
    quant_diff: int  # max |q_hi - (q_lo << 1)| over all quantized features
    weighted_sum_diff: int  # max |L_hi - (L_lo << 1)| over classes
    contributions: dict  # class -> list of {"source", "value"} (lo precision)
    error_sources: list = field(default_factory=list)


def _relation_for(rel_map: dict, assertion_id, feature_dim: int):
    rel = rel_map.get(assertion_id)
    if rel is None:
        return 0, "associate", 1.0
    region = int(rel.get("region", 0))
    region = max(0, min(feature_dim - 1, region))
    rtype = rel.get("type", "associate")
    if rtype not in RELATION_TYPES:
        rtype = "associate"
    return region, rtype, float(rel.get("weight", 1.0))


def prepare_sample(name, image, assertions, rel_map, cfg) -> PreparedSample:
    """Quantize features and build base logits at both precision levels."""
    classes = list(cfg["classes"])
    bits_lo = cfg["frac_bits"]
    bits_hi = bits_lo + 1
    levels = cfg["quant_levels"]
    feature_dim = cfg["feature_dim"]
    try:
        feats = extract_visual_features(image, feature_dim)
    except Exception as exc:  # pragma: no cover - defensive
        raise StageError("quantize", E_QUANTIZE, f"feature extraction: {exc}")

    try:
        built = {
            key: _build_chain(bits, feats, assertions, rel_map, cfg, classes)
            for key, bits in (("lo", bits_lo), ("hi", bits_hi))
        }
    except StageError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise StageError("quantize", E_QUANTIZE, str(exc))

    quant_diff = 0
    for q_lo, q_hi in zip(built["lo"]["qscalars"], built["hi"]["qscalars"]):
        quant_diff = max(quant_diff, abs(q_hi - (q_lo << 1)))
    weighted_sum_diff = 0
    for c in range(len(classes)):
        weighted_sum_diff = max(
            weighted_sum_diff,
            abs(built["hi"]["base"][c] - (built["lo"]["base"][c] << 1)),
        )

    half_unit = 0.5 / (1 << bits_lo)
    error_sources = []
    for label, stage, residual in built["lo"]["residuals"]:
        if abs(residual) > half_unit:
            error_sources.append(
                {
                    "feature": label,
                    "stage": stage,
                    "residual_units": round_half_away(residual * (1 << bits_lo)),
                }
            )

    return PreparedSample(
        name=name,
        classes=classes,
        bits_lo=bits_lo,
        bits_hi=bits_hi,
        base={"lo": built["lo"]["base"], "hi": built["hi"]["base"]},
        cur={
            "lo": [0] * len(classes),
            "hi": [0] * len(classes),
        },
        temp_fp={"lo": built["lo"]["temp_fp"], "hi": built["hi"]["temp_fp"]},
        thr_fp={"lo": built["lo"]["thr_fp"], "hi": built["hi"]["thr_fp"]},
        margin_fp={"lo": built["lo"]["margin_fp"], "hi": built["hi"]["margin_fp"]},
        quant_diff=quant_diff,
        weighted_sum_diff=weighted_sum_diff,
        contributions=built["lo"]["contributions"],
        error_sources=error_sources,
    )


def _build_chain(bits, feats, assertions, rel_map, cfg, classes):
    """Quantize everything at ``bits`` and compute fused base logits."""
    levels = cfg["quant_levels"]
    feature_dim = cfg["feature_dim"]
    qscalars: list[int] = []
    residuals: list[tuple[str, str, float]] = []

    def q(value, label, use_levels=True):
        qi, r_lvl, r_fp = quantize_scalar(
            value, bits, levels if use_levels else None
        )
        qscalars.append(qi)
        residuals.append((label, "level", r_lvl))
        residuals.append((label, "fixedpoint", r_fp))
        return qi

    # Visual features -> per-region, per-class visual logits.
    coefs = {
        c: (
            q(VIS_COEFS.get(c, DEFAULT_COEF)[0], f"coef:{c}:a", use_levels=False),
            q(VIS_COEFS.get(c, DEFAULT_COEF)[1], f"coef:{c}:b", use_levels=False),
        )
        for c in classes
    }
    vis = []
    for idx, (mean, grad) in enumerate(feats):
        qm = q(mean, f"visual:region{idx}:mean")
        qg = q(grad, f"visual:region{idx}:grad")
        vis.append(
            [(coefs[c][0] * qm + coefs[c][1] * qg) >> bits for c in classes]
        )

    # Text assertions -> mapped to visual regions via the relation table.
    tw_fp = q(cfg["text_weight"], "const:text_weight", use_levels=False)
    conflict_fp = q(cfg["conflict_reduction"], "const:conflict_reduction",
                    use_levels=False)
    bonus_fp = q(cfg["supplement_bonus"], "const:supplement_bonus",
                 use_levels=False)
    one = 1 << bits
    region_text = [[0] * len(classes) for _ in range(feature_dim)]
    text_contrib = []  # (assertion_id, region, per-class fused contribution)
    for a in assertions:
        region, rtype, weight = _relation_for(rel_map, a["id"], feature_dim)
        w_fp = q(weight, f"text:{a['id']}:weight", use_levels=False)
        if rtype == "conflict":
            factor_fp = -(one - conflict_fp)  # fixed折减: flip and attenuate
        elif rtype == "supplement":
            factor_fp = one
            w_fp += bonus_fp  # 补充关系按权重叠加
        else:
            factor_fp = one
        eff_w = (w_fp * factor_fp) >> bits
        per_class_inner = []
        for ci, c in enumerate(classes):
            ql = q(float(a["class_logits"][c]), f"text:{a['id']}:{c}")
            inner = (eff_w * ql) >> bits
            region_text[region][ci] += inner
            per_class_inner.append(inner)
        text_contrib.append(
            (
                a["id"],
                region,
                [((tw_fp * inner) >> bits) // feature_dim
                 for inner in per_class_inner],
            )
        )

    # Weighted-sum fusion: region logit = visual + text_weight * text.
    base = [0] * len(classes)
    for r in range(feature_dim):
        for ci in range(len(classes)):
            base[ci] += vis[r][ci] + ((tw_fp * region_text[r][ci]) >> bits)
    base = [v // feature_dim for v in base]

    contributions = {c: [] for c in classes}
    for r in range(feature_dim):
        for ci, c in enumerate(classes):
            contributions[c].append(
                {"source": f"visual:region{r}", "value": vis[r][ci] // feature_dim}
            )
    for aid, region, per_class in text_contrib:
        for ci, c in enumerate(classes):
            contributions[c].append(
                {"source": f"text:{aid}@region{region}", "value": per_class[ci]}
            )

    return {
        "base": base,
        "qscalars": qscalars,
        "residuals": residuals,
        "contributions": contributions,
        "temp_fp": q(cfg["fusion_temperature"], "const:temperature",
                     use_levels=False),
        "thr_fp": q(cfg["confidence_threshold"], "const:threshold",
                    use_levels=False),
        "margin_fp": q(cfg["boundary_margin"], "const:margin", use_levels=False),
    }


def advance_round(prep: PreparedSample, round_idx: int, cfg: dict) -> dict:
    """Advance one convergence round at both precision levels.

    Round update: L_r = L_{r-1} + (L_base - L_{r-1}) * 1/(r+1), all in
    fixed-point. Round 0 therefore starts exactly at the fused base logits.
    """
    try:
        out = {}
        for key, bits in (("lo", prep.bits_lo), ("hi", prep.bits_hi)):
            damp = quantize_scalar(1.0 / (round_idx + 1), bits)[0]
            base = prep.base[key]
            cur = prep.cur[key]
            new = [
                cur[c] + (((base[c] - cur[c]) * damp) >> bits)
                for c in range(len(base))
            ]
            prep.cur[key] = new
            probs, truncs = fp_softmax(new, bits, prep.temp_fp[key])
            out[key] = {"logits": new, "probs": probs, "truncs": truncs}
    except StageError:
        raise
    except Exception as exc:
        raise StageError("fusion", E_FUSION, str(exc))

    classes = prep.classes
    lo, hi = out["lo"], out["hi"]
    win = max(range(len(classes)), key=lambda i: (lo["probs"][i], -i))
    win_hi = max(range(len(classes)), key=lambda i: (hi["probs"][i], -i))
    conf_lo = lo["probs"][win]
    conf_hi = hi["probs"][win_hi]
    softmax_diff = max(
        abs(hi["probs"][i] - (lo["probs"][i] << 1)) for i in range(len(classes))
    )
    scale_lo = 1 << prep.bits_lo
    contributions = sorted(
        (dict(c) for c in prep.contributions[classes[win]]),
        key=lambda c: (-abs(c["value"]), c["source"]),
    )
    return {
        "round": round_idx,
        "logits": {c: lo["logits"][i] for i, c in enumerate(classes)},
        "decision": classes[win],
        "decision_hi": classes[win_hi],
        "confidence_fp": conf_lo,
        "confidence": round(conf_lo / scale_lo, 6),
        "confidence_hi_fp": conf_hi,
        "confidence_hi": round(conf_hi / (scale_lo << 1), 6),
        "confidence_diff": conf_hi - (conf_lo << 1),
        "accepted": conf_lo >= prep.thr_fp["lo"],
        "accepted_hi": conf_hi >= prep.thr_fp["hi"],
        "threshold": round(prep.thr_fp["lo"] / scale_lo, 6),
        "softmax_diff": softmax_diff,
        "exp_truncations": lo["truncs"],
        "contributions": contributions,
    }
