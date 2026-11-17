"""统一时间轴上的门限响度测量与同步观测对照。

统计窗口起止严格按整数毫秒换算为参考采样点：
    窗口 i 起点 = i * window_ms * ref_rate / 1000
两级门限：先做绝对门限（dBFS），再以通过绝对门限窗口的均值能量
减去相对门限得到动态门限；积分响度只由两级都通过的窗口决定。
末尾不足一窗按会话 tail_rule 处理（include 保留短窗，drop 丢弃）。
全静音/无窗口通过时返回 -inf dBFS 等确定结果，不抛异常。
"""

import math

import numpy as np

EPS = 1e-20


def _to_db(energy):
    if energy <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(energy)


def window_boundaries(ref_rate, window_ms, total_frames, tail_rule):
    step = window_ms * ref_rate // 1000
    bounds = []
    start = 0
    while start < total_frames:
        end = min(start + step, total_frames)
        if end - start == step or tail_rule == "include":
            bounds.append((start, end))
        start += step
    return bounds


def measure(timeline, owner, gap_mask, contributions, ref_rate, window_ms,
            absolute_gate_db, relative_gate_db, tail_rule):
    """逐窗口计算能量、两级门限筛选与段归属，返回可回溯的测量结果。"""
    bounds = window_boundaries(ref_rate, window_ms, timeline.shape[0], tail_rule)
    windows = []
    energies = []
    for index, (start, end) in enumerate(bounds):
        block = timeline[start:end]
        energy = float(np.mean(block.astype(np.float64) ** 2))
        energies.append(energy)
        owner_codes, owner_counts = np.unique(owner[start:end], return_counts=True)
        attribution = []
        for code, count in zip(owner_codes.tolist(), owner_counts.tolist()):
            if code < 0:
                attribution.append({"segment_index": None,
                                    "kind": "gap", "frames": int(count)})
            else:
                seg = contributions[code]["seg"]
                attribution.append({
                    "segment_index": int(code),
                    "kind": "segment",
                    "device_id": seg.device_id,
                    "segment_id": seg.segment_id,
                    "frames": int(count),
                })
        windows.append({
            "window_index": index,
            "start_frame": int(start),
            "end_frame": int(end),
            "start_ms": round(start * 1000.0 / ref_rate, 3),
            "end_ms": round(end * 1000.0 / ref_rate, 3),
            "frames": int(end - start),
            "energy": energy,
            "level_dbfs": _to_db(energy),
            "gap_frames": int(np.count_nonzero(gap_mask[start:end])),
            "attribution": attribution,
            "passes_absolute": False,
            "passes_relative": False,
        })

    abs_gate_energy = 10.0 ** (absolute_gate_db / 10.0)
    absolute_pass = [e > abs_gate_energy for e in energies]
    if any(absolute_pass):
        anchor = float(np.mean([e for e, ok in zip(energies, absolute_pass) if ok]))
        relative_gate_energy = anchor * (10.0 ** (relative_gate_db / 10.0))
    else:
        anchor = 0.0
        relative_gate_energy = float("inf")

    integrated = []
    for window, energy, passed in zip(windows, energies, absolute_pass):
        window["passes_absolute"] = bool(passed)
        relative_pass = bool(passed and energy > relative_gate_energy)
        window["passes_relative"] = relative_pass
        if relative_pass:
            integrated.append(energy)

    integrated_energy = float(np.mean(integrated)) if integrated else 0.0
    all_silent = bool(np.all(timeline == 0.0))
    return {
        "window_ms": window_ms,
        "absolute_gate_db": absolute_gate_db,
        "relative_gate_db": relative_gate_db,
        "tail_rule": tail_rule,
        "anchor_energy": anchor,
        "anchor_level_dbfs": _to_db(anchor),
        "relative_gate_level_dbfs": _to_db(relative_gate_energy)
        if math.isfinite(relative_gate_energy) else None,
        "integrated_energy": integrated_energy,
        "integrated_loudness_dbfs": _to_db(integrated_energy),
        "windows_passed_absolute": sum(absolute_pass),
        "windows_passed_relative": len(integrated),
        "window_count": len(windows),
        "all_silent": all_silent,
        "windows": windows,
    }

