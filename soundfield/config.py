"""链路规格常量。

任何改动这些常量的行为都会改变状态目录的版本标识，从而强制旧状态作废、
从头重建，避免新旧版本冲激响应混用。
"""

from __future__ import annotations

import math

#: 定点标度 Q = 2**24，所有参与累乘/判定的量先量化到该标度。
FP_SCALE_BITS = 24
FP_SCALE = 1 << FP_SCALE_BITS

#: 线性相位分数延迟滤波器抽头数（规格固定，内存超限时不允许缩短）。
FRAC_TAPS = 8

#: 采样点级定点 / float64 残差容差（反量化后的幅度单位）。
#: 取 2e-8 与 Q24 单系数量化误差（约 6e-8 半 LSB）同量级，可在确定性样本上
#: 真实触发逐采样点溯源，同时远小于有效信号幅度（权重约 0.01~1）。
AMP_RESIDUAL_TOL = 2e-8

#: 单镜像声源能量（反量化幅度平方和）残差容差。
ENERGY_RESIDUAL_TOL = 1e-12

#: “能量可忽略”阈值（float64 表述，内部换算为定点阈值；裁定只看定点）。
NEGLIGIBLE_ENERGY = 1e-10

#: 允许的采样率集合。
ALLOWED_SAMPLE_RATES = (48000, 96000)

#: 允许的声道布局 -> 扬声器数量。
ALLOWED_LAYOUTS = {"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8}

#: 六面墙标识，顺序即字典序基准：x-/x+/y-/y+/z-/z+。
WALL_IDS = ("x-", "x+", "y-", "y+", "z-", "z+")

#: 声速（m/s）。
SPEED_OF_SOUND = 343.0

#: 中断退出码（可处理终止信号，分块边界生效）。
EXIT_INTERRUPTED = 3
#: 业务错误退出码。
EXIT_BUSINESS_ERROR = 1
#: 未预期运行时错误退出码。
EXIT_RUNTIME_ERROR = 2


class Spec:
    """把规格常量汇总成版本摘要的一部分。"""

    NAMES = (
        "FP_SCALE_BITS",
        "FRAC_TAPS",
        "AMP_RESIDUAL_TOL",
        "ENERGY_RESIDUAL_TOL",
        "NEGLIGIBLE_ENERGY",
        "SPEED_OF_SOUND",
    )

    @classmethod
    def canonical(cls) -> dict[str, float | int]:
        return {
            "FP_SCALE_BITS": FP_SCALE_BITS,
            "FRAC_TAPS": FRAC_TAPS,
            "AMP_RESIDUAL_TOL": AMP_RESIDUAL_TOL,
            "ENERGY_RESIDUAL_TOL": ENERGY_RESIDUAL_TOL,
            "NEGLIGIBLE_ENERGY": NEGLIGIBLE_ENERGY,
            "SPEED_OF_SOUND": SPEED_OF_SOUND,
        }

    @classmethod
    def negligible_energy_q(cls) -> int:
        # 定点能量以 sum(x_q**2) 形式保存，量纲为 Q**2。
        return int(math.floor(NEGLIGIBLE_ENERGY * FP_SCALE * FP_SCALE + 0.5))

    @classmethod
    def energy_tol_q2(cls) -> int:
        return int(math.floor(ENERGY_RESIDUAL_TOL * FP_SCALE * FP_SCALE + 0.5))
