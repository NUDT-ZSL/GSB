"""有理数比重采样（线性插值，无外部音频库）。

输出帧数 N_out = round(N_in * target_rate / ref_rate)，
与理论值误差不超过 1 个目标采样点（实际为 0，因 round 取整）。
"""

import numpy as np


def rational_resample(samples, ref_rate, target_rate):
    if ref_rate == target_rate:
        return samples.copy()
    frames = samples.shape[0]
    out_frames = int(np.rint(frames * target_rate / ref_rate))
    if out_frames == 0:
        return np.zeros((0, samples.shape[1]), dtype=samples.dtype)
    positions = np.arange(out_frames, dtype=np.float64) * ref_rate / target_rate
    lo = np.floor(positions).astype(np.int64)
    frac = positions - lo
    lo_clip = np.clip(lo, 0, frames - 1)
    hi_clip = np.clip(lo + 1, 0, frames - 1)
    left = samples[lo_clip]
    right = samples[hi_clip]
    return left * (1.0 - frac)[:, np.newaxis] + right * frac[:, np.newaxis]


def theoretical_output_frames(input_frames, ref_rate, target_rate):
    return int(np.rint(input_frames * target_rate / ref_rate))

