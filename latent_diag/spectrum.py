"""逐级误差谱与主导来源归因。

以同一输入上 float64 整块路径为参考真值，对条件嵌入、分块去噪、
边界归一化、定点量化、解码五级分别输出：
  - 绝对偏差（max/mean）、相对偏差（max）
  - 偏差分位数（可配置，默认 p50/p90/p99/p100）
  - 误差放大比 = 本级 max_abs / 上一级 max_abs
主导来源在 {chunk_boundary, quantization, normalization} 中归因，
全部中间量随报告输出，可由偏差明细复算。
"""

from __future__ import annotations

import numpy as np

SPECTRUM_STAGES = ("cond_embed", "chunk_denoise", "boundary_norm", "quantize", "decode")

# 主导误差来源类别
SRC_CHUNK_BOUNDARY = "chunk_boundary"
SRC_QUANTIZATION = "quantization"
SRC_NORMALIZATION = "normalization"
SRC_NONE = "none"

_TINY = np.finfo(np.float64).tiny


def stage_metrics(active, reference, quantiles):
    """单级偏差明细：绝对/相对偏差与分位数。"""
    a = np.asarray(active, dtype=np.float64)
    r = np.asarray(reference, dtype=np.float64)
    abs_diff = np.abs(a - r)
    rel_diff = abs_diff / np.maximum(np.abs(r), _TINY)
    flat = abs_diff.ravel()
    qs = np.quantile(flat, list(quantiles)) if flat.size else np.zeros(len(quantiles))
    return {
        "max_abs_dev": float(abs_diff.max()) if flat.size else 0.0,
        "mean_abs_dev": float(abs_diff.mean()) if flat.size else 0.0,
        "max_rel_dev": float(rel_diff.max()) if flat.size else 0.0,
        "abs_dev_quantiles": {f"p{int(q * 100)}": float(v)
                              for q, v in zip(quantiles, qs)},
    }


def compute_error_spectrum(active: dict, reference: dict, boundary_mask_arr,
                           quantiles) -> dict:
    """逐级误差谱 + 放大比 + 主导来源归因。"""
    pairs = {
        "cond_embed": (active["cond"], reference["cond"]),
        "chunk_denoise": (active["denoised"], reference["denoised"]),
        "boundary_norm": (active["normed"], reference["normed"]),
        "quantize": (active["quantized"], reference["quantized"]),
        "decode": (active["decoded"], reference["decoded"]),
    }
    stages = {}
    prev_max = None
    for name in SPECTRUM_STAGES:
        a, r = pairs[name]
        m = stage_metrics(a, r, quantiles)
        if prev_max is None:
            m["amplification_ratio"] = None  # 首级无上一级
        elif prev_max == 0.0:
            m["amplification_ratio"] = None if m["max_abs_dev"] == 0.0 else float("inf")
        else:
            m["amplification_ratio"] = m["max_abs_dev"] / prev_max
        stages[name] = m
        prev_max = m["max_abs_dev"]

    # ---- 主导来源归因（全部中间量可复算）----
    normed_err = np.abs(np.asarray(active["normed"]) - np.asarray(reference["normed"]))
    bnd_max = float(normed_err[boundary_mask_arr].max()) \
        if boundary_mask_arr.any() else 0.0
    interior = ~boundary_mask_arr
    interior_max = float(normed_err[interior].max()) if interior.any() else 0.0
    scores = {
        # 分块边界：边界元素相对内部元素的超额偏差（分块处理特有）
        SRC_CHUNK_BOUNDARY: max(0.0, bnd_max - interior_max),
        # 归一化：边界归一化级相对分块去噪级的增量误差（统计量放大）
        SRC_NORMALIZATION: max(0.0, stages["boundary_norm"]["max_abs_dev"]
                               - stages["chunk_denoise"]["max_abs_dev"]),
        # 量化舍入：定点量化级相对边界归一化级的增量误差
        SRC_QUANTIZATION: max(0.0, stages["quantize"]["max_abs_dev"]
                              - stages["boundary_norm"]["max_abs_dev"]),
    }
    dominant = max(scores, key=lambda k: scores[k])
    if scores[dominant] == 0.0:
        dominant = SRC_NONE
    return {
        "stages": stages,
        "attribution": {
            "dominant_source": dominant,
            "source_scores": scores,
            "rule": "dominant = argmax(source_scores); 全零则 none",
        },
    }
