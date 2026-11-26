"""可切换的精度档执行层。

三档精度（float64 / float32 / 定点缩放整数）共用同一算子语义、同一变换
顺序、同一分块边界与同一归约口径，差异只允许来自数值精度损失本身。
"""

import numpy as np

from .errors import EValidation
from .model import ALPHA, BETA, GAMMA, DELTA

PRECISIONS = ("float64", "float32", "fixed")
ROUNDING_MODES = ("half-even", "half-up", "truncate")


def quantize(value, mode):
    """按声明的舍入模式把浮点值换算为定点整数。"""
    if mode == "half-even":
        return int(np.rint(value))
    if mode == "half-up":
        return int(np.floor(np.asarray(value) + 0.5))
    if mode == "truncate":
        return int(np.trunc(value))
    raise EValidation("未知舍入模式", details={"rounding_mode": mode})


class FloatBackend:
    """float64 与 float32 共用的浮点后端。"""

    def __init__(self, precision):
        if precision not in ("float64", "float32"):
            raise EValidation("非法浮点精度", details={"precision": precision})
        self.precision = precision
        self.dtype = np.float64 if precision == "float64" else np.float32
        self.is_fixed = False
        self.alpha = self.dtype(ALPHA)
        self.beta = self.dtype(BETA)
        self.gamma = self.dtype(GAMMA)
        self.delta = self.dtype(DELTA)

    def initial(self, values):
        return np.asarray(values, dtype=self.dtype)

    def step_vector(self, step):
        return self.dtype(DELTA * (step + 1))

    def kahan_sum(self, terms):
        """块内/跨块 float 补偿求和，归约顺序由 terms 顺序决定。"""
        total = self.dtype(0.0)
        comp = self.dtype(0.0)
        for term in terms:
            term = self.dtype(term)
            y = term - comp
            t = total + y
            comp = (t - total) - y
            total = t
        return total

    def update_block(self, block, left_boundary, right_boundary, step):
        """对一个块执行单步显式 Jacobi 更新，块内采用 float64 补偿求和。

        边界邻居缺失时按 0 处理（边界条件），保证分块串行与整段一次
        执行调用完全相同的逐元素算子。
        """
        cur = block
        n = cur.shape[0]
        nxt = np.empty(n, dtype=self.dtype)
        step_term = self.step_vector(step)
        for i in range(n):
            left = cur[i - 1] if i > 0 else self.dtype(left_boundary)
            right = cur[i + 1] if i < n - 1 else self.dtype(right_boundary)
            nxt[i] = self.kahan_sum(
                (
                    self.alpha * cur[i],
                    self.beta * left,
                    self.gamma * right,
                    step_term,
                )
            )
        return nxt


class FixedBackend:
    """定点缩放整数后端：状态以 int64 表示，缩放因子来自配置。"""

    is_fixed = True
    precision = "fixed"
    dtype = np.int64

    def __init__(self, scale, rounding_mode):
        if scale <= 0:
            raise EValidation("定点缩放因子必须为正数", details={"scale": scale})
        if rounding_mode not in ROUNDING_MODES:
            raise EValidation("未知舍入模式", details={"rounding_mode": rounding_mode})
        self.scale = int(scale)
        self.rounding_mode = rounding_mode
        self.alpha = ALPHA
        self.beta = BETA
        self.gamma = GAMMA
        self.delta = DELTA
        # 系数乘以缩放因子后预舍入为整数，运算全程保持整数语义。
        self.alpha_q = quantize(ALPHA * scale, rounding_mode)
        self.beta_q = quantize(BETA * scale, rounding_mode)
        self.gamma_q = quantize(GAMMA * scale, rounding_mode)

    def initial(self, values):
        scaled = np.asarray(values, dtype=np.float64) * self.scale
        return np.array(
            [quantize(v, self.rounding_mode) for v in scaled], dtype=np.int64
        )

    def step_vector(self, step):
        # 定点偏置 = round(DELTA * (step+1) * scale^2)，再除以 scale 归位。
        return quantize(DELTA * (step + 1) * self.scale * self.scale,
                        self.rounding_mode)

    def kahan_sum(self, terms):
        """定点“补偿求和”即确定性整数累加（整数运算无舍入误差）。"""
        total = 0
        for term in terms:
            total += int(term)
        return total

    def update_block(self, block, left_boundary, right_boundary, step):
        cur = block
        n = cur.shape[0]
        nxt = np.empty(n, dtype=np.int64)
        step_q = self.step_vector(step)
        for i in range(n):
            left = int(cur[i - 1]) if i > 0 else int(left_boundary)
            right = int(cur[i + 1]) if i < n - 1 else int(right_boundary)
            raw = (
                self.alpha_q * int(cur[i])
                + self.beta_q * left
                + self.gamma_q * right
                + step_q
            )
            nxt[i] = quantize(raw / self.scale, self.rounding_mode)
        return nxt

    def to_float(self, block):
        return np.asarray(block, dtype=np.float64) / self.scale


def make_backend(precision, quant_scale=1000, rounding_mode="half-even"):
    if precision in ("float64", "float32"):
        return FloatBackend(precision)
    if precision == "fixed":
        return FixedBackend(quant_scale, rounding_mode)
    raise EValidation("未知精度档", details={"precision": precision})
