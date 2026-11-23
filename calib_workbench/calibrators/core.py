"""Deterministic calibrator fit/apply cores (requirement 2).

Every fitter declares its initial values, comparison order and stopping
conditions explicitly, records the objective value and comparison count
of every iteration, and breaks score ties by ascending sample_id.  No
randomness is used anywhere in fitting or applying.

Conventions:
  * ``fit(scores, y, sample_ids, params) -> (model_dict, trace)``
  * ``apply(model, scores) -> list[float]``
  * trace = {"iterations": [{"iter", "objective", "comparisons"}], "meta": {...}}
"""

from __future__ import annotations

import numpy as np

CLIP_LO, CLIP_HI = 1e-6, 1.0 - 1e-6


# ---------------------------------------------------------------- Platt
def fit_platt(scores, y, sample_ids, params):
    """Platt scaling via Newton's method on L2-regularized NLL.

    Declared determinism contract:
      * init: a = 0.0, b = 0.0
      * samples are processed in (score, sample_id) ascending order
      * stop: gradient L2 norm < tol, or max_iter iterations reached
      * objective: sum(logaddexp(0, z) - y*z) + l2*(a^2 + b^2), z = a*s+b
      * comparisons: n_ref sample evaluations per iteration
    """
    max_iter = int(params.get("max_iter", 50))
    tol = float(params.get("tol", 1e-10))
    l2 = float(params.get("l2", 1e-6))

    order = sorted(range(len(scores)), key=lambda i: (scores[i], sample_ids[i]))
    s = np.array([scores[i] for i in order], dtype=float)
    t = np.array([y[i] for i in order], dtype=float)
    n = len(s)

    a, b = 0.0, 0.0
    iterations = []
    comparisons = 0
    for it in range(max_iter + 1):
        z = a * s + b
        p = 1.0 / (1.0 + np.exp(-z))
        obj = float(np.sum(np.logaddexp(0.0, z) - t * z) + l2 * (a * a + b * b))
        comparisons += n
        g_a = float(np.sum((p - t) * s) + 2.0 * l2 * a)
        g_b = float(np.sum(p - t) + 2.0 * l2 * b)
        grad_norm = float(np.hypot(g_a, g_b))
        iterations.append({
            "iter": it, "objective": obj, "comparisons": comparisons,
            "grad_norm": grad_norm, "a": a, "b": b,
        })
        if grad_norm < tol or it == max_iter:
            break
        w = p * (1.0 - p)
        h_aa = float(np.sum(w * s * s) + 2.0 * l2)
        h_ab = float(np.sum(w * s))
        h_bb = float(np.sum(w) + 2.0 * l2)
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 0.0:  # singular Hessian: stop deterministically
            break
        step_a = (h_bb * g_a - h_ab * g_b) / det
        step_b = (-h_ab * g_a + h_aa * g_b) / det
        a -= step_a
        b -= step_b

    model = {"type": "platt", "a": a, "b": b, "l2": l2}
    trace = {"iterations": iterations,
             "meta": {"init": {"a": 0.0, "b": 0.0}, "tol": tol, "max_iter": max_iter,
                      "order": "(score, sample_id) ascending"}}
    return model, trace


def apply_platt(model, scores):
    a, b = model["a"], model["b"]
    z = a * np.asarray(scores, dtype=float) + b
    p = 1.0 / (1.0 + np.exp(-z))
    return np.clip(p, CLIP_LO, CLIP_HI).tolist()


# ------------------------------------------------------------- Isotonic
def fit_isotonic(scores, y, sample_ids, params):
    """Isotonic regression via pool-adjacent-violators (PAVA).

    Declared determinism contract:
      * samples sorted by (score, sample_id) ascending
      * blocks merged while adjacent block means violate monotonicity;
        merges are evaluated strictly left-to-right
      * objective: weighted SSE of block fit, recorded per merge sweep
      * comparisons: one per adjacent block-pair check
    """
    order = sorted(range(len(scores)), key=lambda i: (scores[i], sample_ids[i]))
    s = [scores[i] for i in order]
    t = [float(y[i]) for i in order]

    # Each block: [weight, sum_y, lo_score, hi_score]
    blocks = [[1.0, v, s[i], s[i]] for i, v in enumerate(t)]
    comparisons = 0
    iterations = []
    merged = True
    sweep = 0
    while merged:
        merged = False
        i = 0
        while i < len(blocks) - 1:
            comparisons += 1
            mean_l = blocks[i][1] / blocks[i][0]
            mean_r = blocks[i + 1][1] / blocks[i + 1][0]
            if mean_l > mean_r:  # violation: pool
                blocks[i][0] += blocks[i + 1][0]
                blocks[i][1] += blocks[i + 1][1]
                blocks[i][3] = blocks[i + 1][3]
                del blocks[i + 1]
                merged = True
                if i > 0:
                    i -= 1
            else:
                i += 1
        sse = 0.0
        for w_, sum_y, lo, hi in blocks:
            m = sum_y / w_
            for j, v in enumerate(t):
                if lo <= s[j] <= hi:
                    sse += (v - m) ** 2
        iterations.append({"iter": sweep, "objective": float(sse),
                           "comparisons": comparisons, "n_blocks": len(blocks)})
        sweep += 1

    model = {
        "type": "isotonic",
        "blocks": [
            {"lo": lo, "hi": hi, "value": sum_y / w_, "weight": w_}
            for w_, sum_y, lo, hi in blocks
        ],
    }
    trace = {"iterations": iterations,
             "meta": {"order": "(score, sample_id) ascending", "algorithm": "PAVA"}}
    return model, trace


def apply_isotonic(model, scores):
    blocks = model["blocks"]
    his = [blk["hi"] for blk in blocks]
    vals = [blk["value"] for blk in blocks]
    out = []
    for s in scores:
        idx = int(np.searchsorted(his, s, side="right"))
        if idx >= len(vals):
            idx = len(vals) - 1
        out.append(float(np.clip(vals[idx], CLIP_LO, CLIP_HI)))
    return out


# ------------------------------------------------------------ Histogram
def fit_histogram(scores, y, sample_ids, params):
    """Histogram binning on equal-width bins over [0, 1].

    Declared determinism contract:
      * scores are clipped to [0, 1] before binning (recorded rule)
      * bin index = min(int(score * n_bins), n_bins - 1)
      * empty bins fall back to the overall reference prevalence
      * single pass; comparisons = n_ref assignments + n_bins aggregates
    """
    n_bins = int(params["n_bins"])
    order = sorted(range(len(scores)), key=lambda i: (scores[i], sample_ids[i]))
    s = [min(max(scores[i], 0.0), 1.0) for i in order]
    t = [float(y[i]) for i in order]

    counts = [0] * n_bins
    sums = [0.0] * n_bins
    comparisons = 0
    for v, lbl in zip(s, t):
        b = min(int(v * n_bins), n_bins - 1)
        counts[b] += 1
        sums[b] += lbl
        comparisons += 1
    prevalence = sum(t) / len(t) if t else 0.5
    values = []
    for b in range(n_bins):
        comparisons += 1
        values.append(sums[b] / counts[b] if counts[b] > 0 else prevalence)
    sse = 0.0
    for v, lbl in zip(s, t):
        b = min(int(v * n_bins), n_bins - 1)
        sse += (lbl - values[b]) ** 2

    model = {
        "type": "histogram",
        "n_bins": n_bins,
        "edges": [i / n_bins for i in range(n_bins + 1)],
        "values": values,
        "counts": counts,
        "fallback": prevalence,
    }
    trace = {"iterations": [{"iter": 0, "objective": float(sse),
                             "comparisons": comparisons, "n_bins": n_bins}],
             "meta": {"clip": "[0, 1]", "bin_rule": "min(int(s*n_bins), n_bins-1)",
                      "empty_bin": "overall reference prevalence"}}
    return model, trace


def apply_histogram(model, scores):
    n_bins = model["n_bins"]
    values = model["values"]
    out = []
    for s in scores:
        v = min(max(s, 0.0), 1.0)
        b = min(int(v * n_bins), n_bins - 1)
        out.append(float(np.clip(values[b], CLIP_LO, CLIP_HI)))
    return out


FITTERS = {"platt": fit_platt, "isotonic": fit_isotonic, "histogram": fit_histogram}
APPLIERS = {"platt": apply_platt, "isotonic": apply_isotonic, "histogram": apply_histogram}
