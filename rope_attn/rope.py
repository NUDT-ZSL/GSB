"""确定性旋转位置编码（RoPE）。

配对口径（显式声明，GPT-NeoX / “半维配对”风格）：
    * 要求 head_dim 为正偶数，半维长度 half = head_dim // 2；
    * 维度 i 与维度 i + half 组成一对，i 取 [0, half) 的整数索引；
    * 第 i 对的基础角频率 inv_freq_i = theta_eff ** (-2i / head_dim)；
    * 位置 p 的逐维旋转角 angle[p, i] = (p * pos_scale) * inv_freq_i；
    * 对张量 X 应用：
          X'[..., i]        = X[..., i]        * cos - X[..., i+half] * sin
          X'[..., i + half] = X[..., i + half] * cos + X[..., i]       * sin
      其中 cos/sin 在位置维与配对维上取值，形状 (seq_len, half) 广播到
      (seq_len, num_heads, half)。

变体（pos_encoding_variant）：
    * standard：theta_eff = base_theta；
    * linear  ：theta_eff = base_theta，位置先乘 pos_scale（线性内插）；
      standard 同样纳入 pos_scale，二者差异通过缓存键区分，linear 额外
      把缩放集中在位置项并做一次显式归一化标记；
    * ntk     ：NTK 感知基调整
      theta_eff = base_theta * pos_scale ** (head_dim / (head_dim - 2))，
      再按 standard 公式取角。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Task


@dataclass(frozen=True)
class RopeSpec:
    """决定角度张量的完整规格，可直接作为缓存键组成。"""

    seq_len: int
    head_dim: int
    base_theta: float
    variant: str
    pos_scale: float

    @classmethod
    def from_task(cls, task: Task) -> "RopeSpec":
        return cls(
            seq_len=task.seq_len,
            head_dim=task.head_dim,
            base_theta=task.base_theta,
            variant=task.pos_encoding_variant,
            pos_scale=task.pos_scale,
        )


def effective_theta(spec: RopeSpec) -> float:
    if spec.variant == "ntk":
        head_dim = spec.head_dim
        return spec.base_theta * spec.pos_scale ** (
            head_dim / (head_dim - 2)
        )
    return spec.base_theta


def compute_angles(spec: RopeSpec) -> np.ndarray:
    """返回逐位置逐配对的旋转角，形状 (seq_len, half)，float64。"""
    half = spec.head_dim // 2
    theta_eff = effective_theta(spec)
    pair_index = np.arange(half, dtype=np.float64)
    inv_freq = theta_eff ** (-2.0 * pair_index / spec.head_dim)
    if spec.variant == "linear":
        positions = np.arange(spec.seq_len, dtype=np.float64) * spec.pos_scale
    else:
        positions = np.arange(spec.seq_len, dtype=np.float64)
        if spec.variant == "standard":
            # standard 也允许 pos_scale 作为统一位置缩放因子。
            positions = positions * spec.pos_scale
    angles = np.outer(positions, inv_freq)
    return np.ascontiguousarray(angles, dtype=np.float64)


def apply_rotation(x: np.ndarray, angles: np.ndarray) -> np.ndarray:
    """按半维配对口径对 Q 或 K 做旋转，返回与输入同 dtype 的新数组。"""
    head_dim = x.shape[-1]
    half = head_dim // 2
    cos = np.cos(angles)[:, None, :]
    sin = np.sin(angles)[:, None, :]
    left = x[..., :half]
    right = x[..., half:]
    rotated_left = left * cos - right * sin
    rotated_right = right * cos + left * sin
    rotated = np.concatenate([rotated_left, rotated_right], axis=-1)
    return np.ascontiguousarray(rotated.astype(x.dtype, copy=False))
