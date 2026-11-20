"""静音段切分与帧能量计算。"""

import numpy as np

from .models import SilenceSegment, frame_rms_db


def frame_energy_db(pcm, frame_len):
    """逐帧 RMS 能量（dBFS），帧边界以样本索引精确确定。"""
    n_frames = len(pcm) // frame_len
    energies = np.empty(n_frames, dtype=np.float64)
    for frame in range(n_frames):
        start = frame * frame_len
        energies[frame] = frame_rms_db(pcm, start, start + frame_len)
    return energies


def silence_segments(observations):
    """以帧级观测 is_speech 为真值，切出最大连续静音段。"""
    result = []
    silence_id = 0
    start = None
    for obs in observations:
        if not obs.is_speech and start is None:
            start = obs.frame_index
        elif obs.is_speech and start is not None:
            result.append(SilenceSegment(silence_id, start, obs.frame_index))
            silence_id += 1
            start = None
    if start is not None:
        result.append(SilenceSegment(silence_id, start, len(observations)))
    return result

