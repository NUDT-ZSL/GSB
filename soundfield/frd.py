"""逐频段线性相位分数延迟滤波器（fractional delay FIR）。

8 抽头 Hann 加窗 sinc，滤波器为线性相位 FIR，滤波器群延迟恒为
``(FRAC_TAPS - 1) /2`` 采样点。镜像声源的理论延迟被拆成

    理论延迟 = 整数偏移 D + 分数延迟 f   （D = ceil(理论延迟 - 群延迟)）

从而 ``D + 群延迟 + f`` 不短于理论延迟，且分数部分 ``f in [0, 1)``。
滤波器系数同时生成 float64 与 Q24 两套，贡献叠加时各走各的路径。
"""

from __future__ import annotations

import math

import numpy as np

from .config import FRAC_TAPS
from .fixedpoint import quantize_array

GROUP_DELAY = (FRAC_TAPS - 1) / 2.0


def design_filter(frac: float) -> tuple[np.ndarray, np.ndarray]:
    """返回 (float64 系数, Q24 int64 系数)，frac 必须位于 [0, 1)。"""

    if not (0.0 <= frac < 1.0):
        raise ValueError(f"fractional delay out of [0,1): {frac}")
    n = np.arange(FRAC_TAPS, dtype=np.float64)
    center = frac + GROUP_DELAY
    x = n - center
    with np.errstate(divide="ignore", invalid="ignore"):
        coeffs = np.where(
            np.abs(x) < 1e-12,
            1.0,
            np.sin(np.pi * x) / (np.pi * x),
        )
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * n / (FRAC_TAPS - 1)))
    coeffs = coeffs * window
    return coeffs.astype(np.float64), quantize_array(coeffs)


def split_delay(delay_samples: float) -> tuple[int, float]:
    """把理论采样延迟拆成 (整数偏移 D, 分数延迟 f)。

    保证 ``D >= 理论延迟 - 群延迟``（整数偏移不短于理论要求）且 f in [0,1)。
    """

    integer_offset = int(math.ceil(delay_samples - GROUP_DELAY - 1e-12))
    if integer_offset < 0:
        integer_offset = 0
    frac = delay_samples - integer_offset - GROUP_DELAY
    # 数值保护：把 frac 拉回 [0,1)。
    if frac < 0.0:
        frac = 0.0
    if frac >= 1.0:
        integer_offset += 1
        frac -= 1.0
    return integer_offset, frac


def apply_fractional_delay(samples: int, weight_q: int, coeffs_q: np.ndarray, offset: int) -> np.ndarray:
    """定点生成一个镜像声源在某频段的冲激贡献。"""

    out = np.zeros(samples, dtype=object)
    weighted = (weight_q * coeffs_q) // (1 << 24)
    end = offset + FRAC_TAPS
    if offset >= samples:
        return out
    stop = min(end, samples)
    out[offset:stop] = np.asarray([int(v) for v in weighted[: stop - offset]], dtype=object)
    return out
