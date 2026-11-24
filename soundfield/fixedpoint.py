"""定点数学层。

所有参与累乘与判定的物理量（吸声乘积、路径衰减、频段增益、滤波器系数、
能量）都先量化到 Q24 定点域，以 Python ``int`` / ``numpy.int64`` 整数运算，
同时维护对应的 float64 参考值。

定点乘法约定::

    mul_q(a_q, b_q) = (a_q * b_q) // Q      # 对称向零舍入

整数加法严格满足结合律，因此无论分块顺序、并发度如何，定点累加结果逐采样点
恒定；float64 参考路径按镜像声源索引的规范顺序求和，同样是确定性的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import FP_SCALE


def quantize(x: float) -> int:
    """把一个 float64 量量化为 Q24 整数（四舍五入，溢出上抛 OverflowError）。"""

    if not math.isfinite(x):
        raise OverflowError(f"non-finite value cannot be quantized: {x!r}")
    return int(math.floor(x * FP_SCALE + 0.5))


def dequantize(x_q: int) -> float:
    """Q24 整数 -> float64。"""

    return int(x_q) / FP_SCALE


def mul_q(a_q: int, b_q: int) -> int:
    """定点乘法，结果仍在 Q24 标度上。"""

    return (int(a_q) * int(b_q)) // FP_SCALE


def chain_mul_q(values_q: list[int]) -> int:
    """沿反射链逐步定点累乘，模拟镜像声源沿反射历史的舍入传播。"""

    acc = FP_SCALE  # 定点 1.0
    for value_q in values_q:
        acc = mul_q(acc, value_q)
    return acc


def quantize_array(arr: np.ndarray) -> np.ndarray:
    """float64 数组 -> int64 Q24 数组。"""

    if not np.all(np.isfinite(arr)):
        raise OverflowError("non-finite element in array quantization")
    return np.floor(np.asarray(arr, dtype=np.float64) * FP_SCALE + 0.5).astype(np.int64)


def dequantize_array(arr_q: np.ndarray) -> np.ndarray:
    return np.asarray(arr_q, dtype=np.int64).astype(np.float64) / FP_SCALE


def mul_array_q(a_q: np.ndarray, b_q: np.ndarray) -> np.ndarray:
    """逐元素定点乘法（数组版）。"""

    return (np.asarray(a_q, dtype=np.int64) * np.asarray(b_q, dtype=np.int64)) // FP_SCALE


def fixed_convolve(x_q: np.ndarray, h_q: np.ndarray) -> np.ndarray:
    """Q24 × Q24 全卷积，结果除回 Q24 标度，全程 Python 大整数运算。

    两个 Q24 量相乘后标度变为 Q48，按定点乘法规则整除 Q 回到 Q24；
    分块用 int64 累加（块和保持在 int64 范围内），块间用 Python ``int``
    任意精度求总，兼顾速度与无溢出。
    """

    x = np.asarray(x_q, dtype=np.int64)
    h = np.asarray(h_q, dtype=np.int64)
    n_x, n_h = x.shape[0], h.shape[0]
    out = [0] * (n_x + n_h - 1)
    # 每块 256 个 Q24 乘积的块和不超过 int64 上限（约 9.2e18）。
    block = 256
    for start in range(0, n_x, block):
        stop = min(start + block, n_x)
        partial = np.zeros(n_x + n_h - 1, dtype=np.int64)
        for i in range(start, stop):
            xv = int(x[i])
            if xv == 0:
                continue
            shifted = (xv * h) // FP_SCALE
            partial[i : i + n_h] += shifted
        for index, value in enumerate(partial.tolist()):
            if value:
                out[index] += int(value)
    return np.asarray(out, dtype=object)


def energy_q(signal_q: np.ndarray) -> int:
    """定点能量（Q24 平方量纲），纯整数求和。"""

    total = 0
    for value in np.asarray(signal_q).tolist():
        iv = int(value)
        total += iv * iv
    return total


def energy_f64(signal: np.ndarray) -> float:
    """float64 参考能量。"""

    arr = np.asarray(signal, dtype=np.float64)
    return float(np.sum(arr * arr, dtype=np.float64))


@dataclass(frozen=True)
class DualValue:
    """同一物理量的定点/浮点双表示。"""

    q: int
    f: float

    @classmethod
    def from_float(cls, value: float) -> "DualValue":
        return cls(q=quantize(value), f=float(value))

    @property
    def abs_diff(self) -> float:
        return abs(dequantize(self.q) - self.f)


def to_int16(signal_q: np.ndarray) -> np.ndarray:
    """把 Q24 定点信号缩放到 int16 PCM，饱和截断。"""

    scaled = np.asarray([int(v) >> (24 - 15) for v in np.asarray(signal_q).tolist()], dtype=np.int64)
    clipped = np.clip(scaled, -32768, 32767)
    return clipped.astype("<i2")


def max_int64_abs(signal_q: np.ndarray) -> int:
    return max(abs(int(v)) for v in np.asarray(signal_q).tolist())
