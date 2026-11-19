"""运行配置：容差、稳定性保护与资源上限。

配置是确定性契约的一部分：同一 case + 同一 config + 固定 seed
必须产出逐字节一致的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class RunConfig:
    # 数值容差
    block_tol: float = 1e-12        # 分块 vs 整段输出最大绝对偏差容差
    angle_tol: float = 1e-9         # 累计角度 vs 理论角度相对容差
    variant_tol: float = 1e-9       # 跨变体等价裁决容差
    # 稳定性保护
    subnormal_guard: bool = True    # 次正规数保护开关
    rounding_mode: str = "nearest"  # nearest | toward_zero
    angle_overflow_threshold: float = 1e12  # 角度绝对值上限
    # 资源上限
    max_seq_len: int = 1 << 16
    max_block_resident_bytes: int = 64 << 20   # 单分块打分驻留内存估算上限
    max_comparisons: int = 1 << 28             # 跨变体裁决总比较次数上限
    resource_policy: str = "fail"              # fail | degrade

    def to_dict(self):
        return asdict(self)


DEFAULT_CONFIG = RunConfig()
