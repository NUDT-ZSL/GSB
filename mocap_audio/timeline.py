"""参考时间轴映射与时间片拼接。

设备本地采样点 n 映射到参考时间轴（参考采样点单位）：
    r(n) = (n - offset_device) / drift_coeff * (ref_rate / dev_rate)
其中 drift_coeff = 1 + drift_rate，offset_device 为 LS 估计的设备域偏移。
对每个目标参考采样点做逆映射后逐采样线性插值，漂移补偿与采样率
换算在同一步完成，保证任意参考时刻的映射误差小于 1 个参考采样点。
缝隙（没有任何段覆盖）按会话规则填零并逐声道标记。
"""

import numpy as np


def _interpolate_channel(samples, device_pos):
    """按浮点设备采样位置做边界保持的线性插值。"""
    frame_count = samples.shape[0]
    lo = np.floor(device_pos).astype(np.int64)
    frac = device_pos - lo
    lo_clip = np.clip(lo, 0, frame_count - 1)
    hi_clip = np.clip(lo + 1, 0, frame_count - 1)
    return samples[lo_clip] * (1.0 - frac)[:, np.newaxis] + \
        samples[hi_clip] * frac[:, np.newaxis]


def map_segment(samples, dev_rate, ref_rate, clock, seg, output_channels):
    """把单个设备段映射到参考时间轴，返回覆盖区间与逐声道浮点贡献。"""
    frame_count = samples.shape[0]
    drift = clock["drift_coeff"]
    offset = clock["offset_samples_device"]
    start_ref = seg.start_ms * ref_rate / 1000.0
    end_ref = start_ref + (frame_count - offset) / drift * ref_rate / dev_rate

    r0 = int(np.ceil(start_ref))
    r1 = int(np.floor(end_ref))
    if r1 < r0:
        return None
    ref_positions = np.arange(r0, r1 + 1, dtype=np.float64)
    device_positions = offset + drift * (
        (ref_positions - start_ref) * dev_rate / ref_rate)
    valid = (device_positions >= 0.0) & (device_positions <= frame_count - 1)
    if not np.any(valid):
        return None
    ref_positions = ref_positions[valid]
    device_positions = device_positions[valid]

    interpolated = _interpolate_channel(samples, device_positions)
    gain = np.array(seg.gain, dtype=np.float64)
    interpolated = interpolated * gain[np.newaxis, :]

    contribution = np.zeros((ref_positions.shape[0], len(output_channels)),
                            dtype=np.float64)
    channel_index = {name: idx for idx, name in enumerate(seg.channels)}
    for out_idx, name in enumerate(output_channels):
        if name in channel_index:
            contribution[:, out_idx] = interpolated[:, channel_index[name]]
    return {
        "ref_start": int(ref_positions[0]),
        "ref_end": int(ref_positions[-1]),
        "samples": contribution,
        "seg": seg,
        "device_rate": dev_rate,
    }


def stitch(contributions, ref_rate, total_frames, output_channels, timeslice_ms):
    """按声明顺序归并段贡献，构造统一时间轴、缝隙标记与时间片清单。"""
    channels = len(output_channels)
    timeline = np.zeros((total_frames, channels), dtype=np.float64)
    owner = np.full(total_frames, -1, dtype=np.int64)
    gap_mask = np.ones(total_frames, dtype=bool)

    for index, contrib in enumerate(contributions):
        if contrib is None:
            continue
        start = contrib["ref_start"]
        end = contrib["ref_end"]
        block = contrib["samples"]
        end = min(end, total_frames - 1)
        block = block[: end - start + 1]
        unowned = gap_mask[start:end + 1]
        timeline[start:end + 1][unowned] = block[unowned]
        owner[start:end + 1][unowned] = index
        gap_mask[start:end + 1] = False

    slice_frames = timeslice_ms * ref_rate // 1000
    slice_count = (total_frames + slice_frames - 1) // slice_frames
    slices = []
    for slice_idx in range(slice_count):
        lo = slice_idx * slice_frames
        hi = min(lo + slice_frames, total_frames)
        gap_frames = int(np.count_nonzero(gap_mask[lo:hi]))
        owner_codes, owner_counts = np.unique(owner[lo:hi], return_counts=True)
        ownership = []
        for code, count in zip(owner_codes.tolist(), owner_counts.tolist()):
            if code < 0:
                ownership.append({"segment_index": None, "frames": int(count)})
            else:
                seg = contributions[code]["seg"]
                ownership.append({
                    "segment_index": int(code),
                    "device_id": seg.device_id,
                    "segment_id": seg.segment_id,
                    "frames": int(count),
                })
        slices.append({
            "slice_index": slice_idx,
            "start_frame": lo,
            "end_frame": hi,
            "start_ms": round(lo * 1000.0 / ref_rate, 3),
            "end_ms": round(hi * 1000.0 / ref_rate, 3),
            "empty": bool(np.all(timeline[lo:hi] == 0.0)),
            "gap_frames": gap_frames,
            "total_frames": hi - lo,
            "ownership": ownership,
        })

    gap_ranges = _runs(gap_mask)
    return {
        "timeline": timeline,
        "owner": owner,
        "gap_mask": gap_mask,
        "slices": slices,
        "gap_ranges": [
            {"start_frame": int(a), "end_frame": int(b),
             "frames": int(b - a + 1),
             "start_ms": round(a * 1000.0 / ref_rate, 3),
             "end_ms": round((b + 1) * 1000.0 / ref_rate, 3)}
            for a, b in gap_ranges
        ],
    }


def _runs(mask):
    if not np.any(mask):
        return []
    idx = np.flatnonzero(mask)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    return list(zip(starts.tolist(), ends.tolist()))


def total_reference_frames(session, ref_rate):
    """时间轴覆盖到最后一个段的理论结束时刻，向上取整到参考帧。"""
    last_end = max(seg.start_ms + seg.duration_ms
                   for _, seg in session.iter_segments())
    return int(np.ceil(last_end * ref_rate / 1000.0))

