"""Deviation spectrum vs the float64 dequantized reference path.

Levels: quant_input, fixed_point_mac, accumulator_reduce, dequant_output.
Per level: max/mean absolute deviation, max relative deviation, deviation
quantiles (p50/p90/p99) and the error amplification ratio against the
previous level. The dominant error source is the level with the largest
amplification ratio.
"""
from __future__ import annotations

import numpy as np

from . import conv_core
from .errors import Code, PipelineError
from .sample_gen import gen_activation, gen_kernel

LEVELS = ("quant_input", "fixed_point_mac", "accumulator_reduce",
          "dequant_output")

_EPS = 1e-12


def _metrics(dev: np.ndarray, ref: np.ndarray):
    dev = np.asarray(dev, dtype=np.float64).ravel()
    ref = np.asarray(ref, dtype=np.float64).ravel()
    abs_dev = np.abs(dev)
    rel = abs_dev / np.maximum(np.abs(ref), _EPS)
    return {
        "max_abs": float(abs_dev.max()) if abs_dev.size else 0.0,
        "mean_abs": float(abs_dev.mean()) if abs_dev.size else 0.0,
        "max_rel": float(rel.max()) if rel.size else 0.0,
        "p50": float(np.quantile(abs_dev, 0.50)) if abs_dev.size else 0.0,
        "p90": float(np.quantile(abs_dev, 0.90)) if abs_dev.size else 0.0,
        "p99": float(np.quantile(abs_dev, 0.99)) if abs_dev.size else 0.0,
    }


def _first_exceeding(dev: np.ndarray, tol: float):
    abs_dev = np.abs(dev)
    idx = int(np.argmax(abs_dev > tol))
    if abs_dev.flat[idx] <= tol:
        return None
    pos = np.unravel_index(idx, dev.shape)
    return {"position": [int(p) for p in pos],
            "deviation": float(dev[pos]),
            "abs_deviation": float(abs_dev[pos]),
            "tolerance": tol}


def compute_spectrum(job, weight_params, calib_stats, artifacts,
                     tolerances, enforce=True):
    """Build the deviation spectrum for one job.

    ``artifacts`` maps node name -> artifact dict (from the cache).
    Raises E_TOLERANCE_EXCEEDED on the first level out of tolerance.
    """
    x = gen_activation(job)
    w = gen_kernel(job)
    stride, pad = int(job["stride"]), int(job["padding"])
    ref = conv_core.float_reference_conv(x, w, stride, pad)
    n, k, oh, ow = ref.shape

    deq_in = artifacts["quant_input_prep"]["deq_input"]
    acc = artifacts["blocked_mac"]["acc"]
    f_out = artifacts["reduce_dequant"]["f_out"]

    combined = float(job["quant_scale"]) * float(weight_params["weight_scale"])
    acc_f = acc.astype(np.float64) * combined
    ref_flat = ref.transpose(0, 2, 3, 1).reshape(n * oh * ow, k)

    # single-block (unblocked) accumulator: exactness baseline of the reduce
    q_in = artifacts["quant_input_prep"]["q_input"]
    q_w = artifacts["weight_quant"]["q_weight"]
    kh, kw = int(job["kernel_shape"][2]), int(job["kernel_shape"][3])
    q_cols = conv_core.im2col(q_in, kh, kw, stride, pad,
                              pad_value=int(job["zero_point"]))
    q_wmat = conv_core.kernel_matrix(q_w)
    red = q_cols.shape[1]
    acc_unblocked = conv_core.blocked_int8_mac(
        q_cols, q_wmat, red, int(job["zero_point"]),
        int(weight_params["weight_zero_point"]))
    reduce_dev = (acc - acc_unblocked).astype(np.float64) * combined

    devs = {
        "quant_input": deq_in - x,
        "fixed_point_mac": acc_f - ref_flat,
        "accumulator_reduce": reduce_dev,
        "dequant_output": f_out - ref_flat,
    }
    refs = {"quant_input": x, "fixed_point_mac": ref_flat,
            "accumulator_reduce": ref_flat, "dequant_output": ref_flat}

    levels = {}
    prev_max = None
    dominant, dominant_jump = None, -1.0
    for level in LEVELS:
        m = _metrics(devs[level], refs[level])
        if prev_max is None:
            m["amplification"] = 1.0
            jump = m["max_abs"]
        elif prev_max < _EPS:
            # previous level is exact: the ratio is undefined
            m["amplification"] = None
            jump = m["max_abs"] - prev_max
        else:
            m["amplification"] = m["max_abs"] / max(prev_max, _EPS)
            jump = m["max_abs"] - prev_max
        if jump > dominant_jump:
            dominant, dominant_jump = level, jump
        prev_max = m["max_abs"]
        levels[level] = m

        tol = getattr(tolerances, level)
        hit = _first_exceeding(devs[level], tol)
        if hit is not None and enforce:
            raise PipelineError(
                Code.E_TOLERANCE_EXCEEDED,
                f"level '{level}' deviation {hit['abs_deviation']:.6g} "
                f"exceeds tolerance {tol:.6g}",
                job_id=job["job_id"], node=level, level=level, **hit)

    return {"job_id": job["job_id"], "levels": levels,
            "dominant_error_source": dominant,
            "dominant_max_abs_jump": dominant_jump,
            "reference": "float64_dequantized"}
