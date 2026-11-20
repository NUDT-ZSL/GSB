"""噪声底低分位滑窗估计、突变检测与分段。"""

import numpy as np

from .config import NoiseConfig, SegmentConfig
from .errors import DomainError, E_WINDOW_INVALID, E_SEGMENT_SPILL
from .models import WindowCoverage, NoiseSegment


def estimate_noise_floor(energies, config, call_id=""):
    """对逐帧能量做确定性低分位滑窗估计。

    窗口锚点按 hop 推进；每个锚点取锚点前（含锚点）最多 window_frames 帧，
    样本不足时按 E_WINDOW_INVALID 报错并指明 call_id 与帧索引；
    hop<=0 同样返回 E_WINDOW_INVALID。逐帧结果由覆盖窗口唯一确定。
    """
    energies = np.asarray(energies, dtype=np.float64)
    n_frames = energies.size
    if config.hop_frames <= 0:
        raise DomainError(
            E_WINDOW_INVALID,
            "滑窗步长必须为正整数",
            call_id=call_id,
            frame_index=0,
            hop_frames=config.hop_frames,
        )
    if config.window_frames <= 0:
        raise DomainError(
            E_WINDOW_INVALID,
            "估计窗口必须为正整数",
            call_id=call_id,
            frame_index=0,
            window_frames=config.window_frames,
        )
    if not 0.0 < config.quantile < 1.0:
        raise DomainError(
            E_WINDOW_INVALID,
            "低分位必须位于 (0,1)",
            call_id=call_id,
            frame_index=0,
            quantile=config.quantile,
        )

    anchors = list(range(0, n_frames, config.hop_frames))
    window_values = []
    coverages = []
    for window_id, anchor in enumerate(anchors):
        start = max(0, anchor - config.window_frames + 1)
        end = anchor + 1
        sample_count = end - start
        if sample_count < config.window_frames:
            raise DomainError(
                E_WINDOW_INVALID,
                "窗口样本数不足",
                call_id=call_id,
                frame_index=anchor,
                window_id=window_id,
                sample_count=sample_count,
                required=config.window_frames,
            )
        value = float(
            np.quantile(energies[start:end], config.quantile, method="linear")
        )
        window_values.append(value)
        coverages.append(
            WindowCoverage(
                window_id=window_id,
                start_frame=start,
                end_frame=end,
                anchor_frame=anchor,
            )
        )

    floor = np.empty(n_frames, dtype=np.float64)
    for window_id, anchor in enumerate(anchors):
        next_anchor = (
            anchors[window_id + 1]
            if window_id + 1 < len(anchors)
            else n_frames
        )
        floor[anchor:next_anchor] = window_values[window_id]
    return floor, coverages


def _persistent_filter(raw_flags, min_duration):
    """最小持续帧数过滤，确定性消除翻转；输出布尔数组。"""
    n = raw_flags.size
    kept = np.zeros(n, dtype=bool)
    index = 0
    while index < n:
        if not raw_flags[index]:
            index += 1
            continue
        end = index + 1
        while end < n and raw_flags[end]:
            end += 1
        if end - index >= min_duration:
            kept[index:end] = True
        index = end
    return kept


def detect_step_positions(floor, observations, config):
    """识别稳定台阶位置（帧索引），语音活动帧不参与突变判定。"""
    n_frames = floor.size
    eligible = np.array(
        [not obs.is_speech for obs in observations], dtype=bool
    )
    raw = np.zeros(n_frames, dtype=bool)
    for index in range(1, n_frames):
        if eligible[index] and eligible[index - 1]:
            raw[index] = abs(floor[index] - floor[index - 1]) >= config.step_threshold_db
    persistent = _persistent_filter(raw, max(1, config.min_duration_frames))
    return [index for index in range(n_frames) if persistent[index]]


def segment_noise_floor(floor, observations, config, call_id=""):
    """分段：语音边界与稳定台阶位置共同切段，绝不跨越语音活动帧。"""
    n_frames = floor.size
    step_positions = detect_step_positions(floor, observations, config)
    boundaries = {0, n_frames}
    for index, obs in enumerate(observations):
        if obs.is_speech and (index == 0 or not observations[index - 1].is_speech):
            boundaries.add(index)
        if not obs.is_speech and index > 0 and observations[index - 1].is_speech:
            boundaries.add(index)
    boundaries.update(step_positions)
    boundaries = sorted(boundaries)

    segments = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        if end <= start:
            continue
        block = floor[start:end]
        is_speech = bool(observations[start].is_speech)
        spill_frames = [
            frame
            for frame in range(start, end)
            if observations[frame].is_speech != is_speech
        ]
        if spill_frames:
            raise DomainError(
                E_SEGMENT_SPILL,
                "噪声底分段跨越语音活动帧",
                call_id=call_id,
                spill_frames=spill_frames[:10],
            )
        segments.append(
            NoiseSegment(
                start_frame=start,
                end_frame=end,
                level_db=float(np.mean(block)),
                dispersion_db=float(np.std(block)),
                is_speech=is_speech,
            )
        )
    return segments, step_positions

