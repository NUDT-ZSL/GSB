"""可切换 dtype_profile 执行层：float64 / float32 / 定点缩放整数。

三档共用同一算子语义：所有算子一律在 float64 上求值，
精度档只在每个阶段边界对中间结果做 cast/量化，
因此不同档之间的结果差异仅来自精度损失本身，
不改变参与计算的 task 集合与随机数消费顺序。
"""

from __future__ import annotations

import numpy as np

from .errors import (
    StageError, STAGE_DECODE,
    E_INVALID_QUANT_SCALE, E_UNKNOWN_ROUNDING_MODE, E_UNKNOWN_DTYPE_PROFILE,
)

PROFILE_FLOAT64 = "float64"
PROFILE_FLOAT32 = "float32"
PROFILE_FIXED = "fixed"
DTYPE_PROFILES = (PROFILE_FLOAT64, PROFILE_FLOAT32, PROFILE_FIXED)

ROUND_HALF_EVEN = "half_even"
ROUND_HALF_UP = "half_up"
ROUND_FLOOR = "floor"
ROUND_TRUNC = "trunc"
ROUNDING_MODES = (ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_FLOOR, ROUND_TRUNC)

_INT32_LO = -(2 ** 31)
_INT32_HI = 2 ** 31 - 1


def _round_mantissa(scaled, mode):
    if mode == ROUND_HALF_EVEN:
        return np.rint(scaled)
    if mode == ROUND_HALF_UP:
        return np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    if mode == ROUND_FLOOR:
        return np.floor(scaled)
    if mode == ROUND_TRUNC:
        return np.trunc(scaled)
    raise ValueError(f"unknown rounding mode: {mode}")


def quantize(x, scale, mode):
    """定点缩放整数量化：mantissa = round(x*scale) 截断到 int32，再 /scale。

    返回值用 float64 承载，但取值严格落在 scale 网格上，
    等价于 int32 定点存储的数值语义。
    """
    mantissa = _round_mantissa(np.asarray(x, dtype=np.float64) * scale, mode)
    mantissa = np.clip(mantissa, _INT32_LO, _INT32_HI)
    return mantissa / scale


class DtypeProfile:
    """精度档执行层。

    cast           : 阶段边界 cast（cond/noise/denoise/norm）。
    cast_quantize  : 定点量化级 cast（解码链路入口）。
    float64 两者皆恒等；float32 两者皆为 float32 舍入；
    fixed 在量化级之前按 float64 计算，量化级执行定点缩放整数舍入。
    """

    def __init__(self, name, quant_scale=1.0, rounding_mode=ROUND_HALF_EVEN):
        self.name = name
        self.quant_scale = float(quant_scale)
        self.rounding_mode = rounding_mode

    def cast(self, x):
        if self.name == PROFILE_FLOAT64:
            return np.asarray(x, dtype=np.float64)
        if self.name == PROFILE_FLOAT32:
            return np.asarray(x, dtype=np.float32).astype(np.float64)
        if self.name == PROFILE_FIXED:
            return np.asarray(x, dtype=np.float64)
        raise ValueError(f"unknown dtype profile: {self.name}")

    def cast_quantize(self, x):
        if self.name == PROFILE_FIXED:
            return quantize(x, self.quant_scale, self.rounding_mode)
        return self.cast(x)

    def quantization_grain(self):
        """量化级固有分辨率（float 档为 0）。"""
        if self.name == PROFILE_FIXED:
            return 1.0 / self.quant_scale
        return 0.0

    def describe(self):
        return {
            "dtype_profile": self.name,
            "quant_scale": self.quant_scale,
            "rounding_mode": self.rounding_mode,
        }


def validate_quant_params(task):
    """定点档参数校验，归因到 decode 阶段（量化在解码链路入口生效）。"""
    if task.dtype_profile not in DTYPE_PROFILES:
        raise StageError(
            STAGE_DECODE, E_UNKNOWN_DTYPE_PROFILE, task.task_id,
            f"unknown dtype_profile: {task.dtype_profile!r}",
            {"dtype_profile": task.dtype_profile})
    if task.dtype_profile != PROFILE_FIXED:
        return
    if not (isinstance(task.quant_scale, (int, float))
            and np.isfinite(task.quant_scale) and task.quant_scale > 0):
        raise StageError(
            STAGE_DECODE, E_INVALID_QUANT_SCALE, task.task_id,
            f"quant_scale must be positive, got {task.quant_scale!r}",
            {"quant_scale": task.quant_scale})
    if task.rounding_mode not in ROUNDING_MODES:
        raise StageError(
            STAGE_DECODE, E_UNKNOWN_ROUNDING_MODE, task.task_id,
            f"unknown rounding_mode: {task.rounding_mode!r}",
            {"rounding_mode": task.rounding_mode,
             "known_modes": list(ROUNDING_MODES)})


def get_profile(task):
    validate_quant_params(task)
    return DtypeProfile(task.dtype_profile, task.quant_scale, task.rounding_mode)
