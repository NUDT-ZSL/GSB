"""逐设备段动态范围预算判定与建议动作。

全部判定只依赖统一时间轴上的测量值（峰值、窗口 RMS），
与并发完成顺序无关；条目严格按设备段声明顺序写入。
同步降级或存在被标记缝隙时附加降级依据，但不改变测量结论。
"""

import math

import numpy as np


def _db(value):
    if value <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(value)


def evaluate_segments(contributions, stitched, loudness_result, session,
                      sync_results):
    timeline = stitched["timeline"]
    owner = stitched["owner"]
    gap_mask = stitched["gap_mask"]
    verdicts = []
    for index, contrib in enumerate(contributions):
        seg = contrib["seg"] if contrib is not None else None
        if seg is None:
            continue
        start = contrib["ref_start"]
        end = min(contrib["ref_end"], timeline.shape[0] - 1)
        block = timeline[start:end + 1]
        owned = owner[start:end + 1] == index
        if not np.any(owned):
            rms_db = float("-inf")
            peak_db = float("-inf")
            loud_range_db = 0.0
        else:
            owned_samples = block[owned]
            rms = float(np.sqrt(np.mean(owned_samples ** 2)))
            peak = float(np.max(np.abs(owned_samples))) if owned_samples.size else 0.0
            rms_db = _db(rms)
            peak_db = _db(peak)
            loud_range_db = peak_db - rms_db if math.isfinite(rms_db) else 0.0

        in_budget = (math.isfinite(loud_range_db) and
                     session.dr_lower_db <= loud_range_db <= session.dr_upper_db)
        sync = sync_results[index]
        segment_gap_frames = int(np.count_nonzero(gap_mask[start:end + 1]))
        degraded_basis = []
        if sync.get("degraded"):
            degraded_basis.extend(sync.get("degradation_reasons", []))
        if segment_gap_frames:
            degraded_basis.append(
                "段覆盖区间内存在 %d 个缝隙填充帧" % segment_gap_frames)

        action, gain_delta_db, limiter_threshold_db = _recommend(
            peak_db, loud_range_db, session)
        verdicts.append({
            "segment_index": index,
            "device_id": seg.device_id,
            "segment_id": seg.segment_id,
            "rms_dbfs": rms_db,
            "peak_dbfs": peak_db,
            "loudness_range_db": loud_range_db,
            "dr_lower_db": session.dr_lower_db,
            "dr_upper_db": session.dr_upper_db,
            "within_budget": bool(in_budget),
            "recommended_action": action,
            "gain_delta_db": gain_delta_db,
            "limiter_threshold_dbfs": limiter_threshold_db,
            "degraded_basis": degraded_basis,
        })
    return verdicts


def _recommend(peak_db, loud_range_db, session):
    """结论只由测量值与预算上下界推导，保证确定性。"""
    if not math.isfinite(peak_db):
        return "hold", 0.0, None
    if loud_range_db > session.dr_upper_db:
        threshold = round(session.dr_upper_db - 3.0, 3)
        return ("limit", 0.0, min(threshold, peak_db - 1.0))
    if loud_range_db < session.dr_lower_db:
        delta = round(session.dr_lower_db - loud_range_db, 3)
        if peak_db + delta > -1.0:
            return ("limit", 0.0, -1.0)
        return ("gain", delta, None)
    return "hold", 0.0, None

