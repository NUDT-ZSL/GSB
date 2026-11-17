"""同步脉冲检测与偏移/漂移最小二乘估计。

可观测性边界：本模块只使用样本中实际检测到的脉冲峰值采样点，
缺失标记或脉冲落在段外时仅记录降级依据，绝不凭空补点。

模型（阶数 1，线性漂移）：
    n_obs = drift_coeff * n_theory + offset_samples + 残差
其中 drift_coeff = 1 + drift_rate，drift_rate 为每采样点相对漂移。
阶数 0（仅偏移）：
    n_obs = n_theory + offset_samples + 残差
偏移以参考采样点为单位报告，并按四舍五入给出整数采样点估计。
"""

import numpy as np

from .errors import SyncError


def detect_peaks(samples, sample_rate, pulses, peak_threshold=0.5):
    """在每个脉冲的理论位置 ± 搜索窗内找能量峰值采样点。

    返回观测列表，元素为 dict：
      theoretical_ms / theory_sample / observed_sample / observed_ms /
      delta_samples / found / search_range
    窗内无有效峰值（或搜索窗完全落在段外）时 found=False。
    多声道取逐采样绝对幅值的跨声道最大值，峰值确定且可重复。
    峰值必须高于 peak_threshold（归一化幅值）才算可观测脉冲，
    避免把低幅背景内容误判为同步标记；门限由会话给定，缺省 0.5。
    """
    frame_count = samples.shape[0]
    energy = np.max(np.abs(samples), axis=1)
    observations = []
    for pulse in pulses:
        center = pulse.theoretical_ms * sample_rate / 1000.0
        half = pulse.search_ms * sample_rate / 1000.0
        lo = int(np.ceil(center - half))
        hi = int(np.floor(center + half))
        lo_clamped = max(lo, 0)
        hi_clamped = min(hi, frame_count - 1)
        record = {
            "theoretical_ms": pulse.theoretical_ms,
            "theory_sample": round(center, 6),
            "search_lo_sample": lo,
            "search_hi_sample": hi,
            "found": False,
            "observed_sample": None,
            "observed_ms": None,
            "delta_samples": None,
            "out_of_segment": hi < 0 or lo >= frame_count,
        }
        if lo_clamped <= hi_clamped:
            window = energy[lo_clamped:hi_clamped + 1]
            if np.any(window >= peak_threshold):
                peak = int(lo_clamped + int(np.argmax(window)))
                record["found"] = True
                record["observed_sample"] = peak
                record["observed_ms"] = round(peak * 1000.0 / sample_rate, 6)
                record["delta_samples"] = round(peak - center, 6)
        observations.append(record)
    return observations


def estimate_clock(observations, sample_rate, ref_rate, model_order, refs):
    """依据检测到的观测点做最小二乘估计。

    返回估计结果 dict；可观测性不足时按规则降级并在结果中标记，
    完全无观测时抛 E_SYNC。
    """
    found = [o for o in observations if o["found"]]
    missing = [o for o in observations if not o["found"]]
    scale = ref_rate / sample_rate
    theory = np.array([o["theory_sample"] for o in found], dtype=np.float64)
    observed = np.array([o["observed_sample"] for o in found],
                        dtype=np.float64)

    result = {
        "refs": refs,
        "sample_rate": sample_rate,
        "reference_rate": ref_rate,
        "requested_model_order": model_order,
        "used_model_order": model_order,
        "degraded": False,
        "degradation_reasons": [],
        "observation_count": len(found),
        "missing_markers": [
            {
                "theoretical_ms": o["theoretical_ms"],
                "search_lo_sample": o["search_lo_sample"],
                "search_hi_sample": o["search_hi_sample"],
                "out_of_segment": o["out_of_segment"],
            }
            for o in missing
        ],
        "observations": [
            {
                "theoretical_ms": o["theoretical_ms"],
                "theory_sample": o["theory_sample"],
                "observed_sample": o["observed_sample"],
                "observed_ms": o["observed_ms"],
                "delta_samples": o["delta_samples"],
            }
            for o in found
        ],
    }

    if not found:
        raise SyncError(
            "段内没有任何可观测同步脉冲，无法估计偏移",
            refs=[refs.get("device_id"), refs.get("segment_id")],
            detail={"missing_markers": result["missing_markers"]},
        )

    if model_order == 1 and len(found) < 2:
        result["degraded"] = True
        result["used_model_order"] = 0
        result["degradation_reasons"].append(
            "线性漂移模型至少需要 2 个观测点，实际 %d 个，降级为仅偏移估计"
            % len(found))

    used_order = result["used_model_order"]
    if used_order == 1:
        x_mean = float(theory.mean())
        y_mean = float(observed.mean())
        denom = float(np.sum((theory - x_mean) ** 2))
        drift_coeff = float(
            np.sum((theory - x_mean) * (observed - y_mean)) / denom)
        offset_dev = y_mean - drift_coeff * x_mean
        predicted = offset_dev + drift_coeff * theory
    else:
        drift_coeff = 1.0
        offset_dev = float(np.mean(observed - theory))
        predicted = theory + offset_dev

    residuals_dev = observed - predicted
    drift_rate = drift_coeff - 1.0
    result.update({
        "drift_coeff": drift_coeff,
        "drift_rate": drift_rate,
        "drift_ppm": drift_rate * 1e6,
        "offset_samples_device": offset_dev,
        "offset_samples_reference": offset_dev * scale,
        "offset_integer_reference": int(np.rint(offset_dev * scale)),
        "residuals_device_samples": [round(float(r), 6) for r in residuals_dev],
        "rms_residual_device_samples": round(
            float(np.sqrt(np.mean(residuals_dev ** 2))), 6),
        "max_abs_residual_device_samples": round(
            float(np.max(np.abs(residuals_dev))), 6),
    })
    return result


def reference_identity(sample_rate, refs):
    """参考设备与自身时间轴对齐：偏移 0、漂移 0，仍保留观测以便对照。"""
    return {
        "refs": refs,
        "sample_rate": sample_rate,
        "reference_rate": sample_rate,
        "requested_model_order": 1,
        "used_model_order": 1,
        "degraded": False,
        "degradation_reasons": ["参考设备，时钟恒等映射"],
        "drift_coeff": 1.0,
        "drift_rate": 0.0,
        "drift_ppm": 0.0,
        "offset_samples_device": 0.0,
        "offset_samples_reference": 0.0,
        "offset_integer_reference": 0,
        "residuals_device_samples": [],
        "rms_residual_device_samples": 0.0,
        "max_abs_residual_device_samples": 0.0,
        "observation_count": 0,
        "missing_markers": [],
        "observations": [],
    }

