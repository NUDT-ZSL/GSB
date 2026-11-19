"""确定性旋转位置编码层。

配对口径（显式固定，禁止变更）：
  head_dim 维向量按交错半维配对 (x[2i], x[2i+1])，i = 0..half-1，
  half = head_dim // 2。旋转角表形状 (seq_len, half)，第 i 列角为 theta_i。
  旋转公式：
    x'[2i]   = x[2i]*cos(theta_i) - x[2i+1]*sin(theta_i)
    x'[2i+1] = x[2i]*sin(theta_i) + x[2i+1]*cos(theta_i)

变体口径（角度映射，均为 float64 累计）：
  rope_classic:           angle(p, i) = p * theta^(-2i/d)
  position_interpolation: angle(p, i) = (p / s) * theta^(-2i/d)，s = pos_scale
  ntk_aware:              theta' = theta * s^(d/(d-2))，
                          angle(p, i) = p * theta'^(-2i/d)
  blockwise_freq:         b = p // B，local = p - b*B，
                          angle(p, i) = local * theta^(-2i/d) * theta^(-2b/d)
                          （分块频率调整：每跨一块频率基准衰减 theta^(-2/d)）

确定性约束：不读系统时间、不依赖字典遍历顺序、无未声明全局状态。
"""

from __future__ import annotations

import numpy as np

from .errors import (
    E_ANGLE_OVERFLOW, E_INJECT_ANGLE, E_PARAM_INVALID,
    STAGE_ANGLE, StageError,
)

VARIANTS = ("rope_classic", "position_interpolation", "ntk_aware", "blockwise_freq")

_F64 = np.float64
_TINY = float(np.finfo(_F64).tiny)  # 最小正规数，次正规数保护阈值


def _check_params(case_id, head_dim, base_theta, pos_scale, variant):
    if variant not in VARIANTS:
        raise StageError(E_PARAM_INVALID, STAGE_ANGLE, case_id,
                         f"未知位置编码变体: {variant!r}", field="pos_encoding_variant")
    if not (isinstance(head_dim, (int, np.integer))) or head_dim <= 0:
        raise StageError(E_PARAM_INVALID, STAGE_ANGLE, case_id,
                         f"head_dim 必须为正整数, got {head_dim!r}", field="head_dim")
    if not (1.0 < float(base_theta) <= 1e12):
        raise StageError(E_PARAM_INVALID, STAGE_ANGLE, case_id,
                         f"base_theta 越界 (1, 1e12], got {base_theta!r}",
                         field="base_theta")
    # 注入点（angle 阶段）：pos_scale 非正，由参数触发，不改源码
    if float(pos_scale) <= 0.0:
        raise StageError(E_INJECT_ANGLE, STAGE_ANGLE, case_id,
                         f"pos_scale 非正: {pos_scale!r}", field="pos_scale")


def inv_freq_table(variant, head_dim, base_theta, pos_scale, config,
                   case_id=None):
    """逐维逆频率 inv_freq[i]，形状 (half,)，float64。

    次正规数保护：开启时把幅值低于最小正规数的逆频率钳到 tiny，
    并在返回的标记中记录介入。
    """
    _check_params(case_id, head_dim, base_theta, pos_scale, variant)
    half = head_dim // 2
    idx = np.arange(half, dtype=_F64)
    if variant == "ntk_aware" and head_dim > 2:
        theta_eff = float(base_theta) * float(pos_scale) ** (head_dim / (head_dim - 2.0))
    else:
        theta_eff = float(base_theta)
    inv = theta_eff ** (-2.0 * idx / head_dim)
    if variant == "position_interpolation":
        inv = inv / float(pos_scale)
    guard_hit = False
    if config.subnormal_guard:
        small = np.abs(inv) < _TINY
        if bool(small.any()):
            inv = np.where(small, _TINY, inv)
            guard_hit = True
    return inv.astype(_F64), guard_hit


def _apply_rounding(angles, mode):
    """舍入模式：nearest 为 IEEE 最近舍入（原样）；toward_zero 向零截断到 1e-12 网格。"""
    if mode == "nearest":
        return angles
    if mode == "toward_zero":
        return np.trunc(angles * 1e12) / 1e12
    raise ValueError(f"未知舍入模式: {mode!r}")


def angle_table(case, config):
    """计算旋转角表，返回 (angles, diag)。

    angles: (seq_len, half) float64；diag 含次正规保护标记与角度累计核对结果。
    越界（|angle| 超阈值或累计角度超容差）抛 E_ANGLE_OVERFLOW。
    """
    case_id = case["case_id"]
    variant = case["pos_encoding_variant"]
    seq_len = int(case["seq_len"])
    head_dim = int(case["head_dim"])
    base_theta = float(case["base_theta"])
    pos_scale = float(case["pos_scale"])
    block_size = int(case["block_size"])

    inv, guard_hit = inv_freq_table(variant, head_dim, base_theta, pos_scale,
                                    config, case_id=case_id)
    pos = np.arange(seq_len, dtype=_F64)
    # 前置廉价越界检查：避免为必然越界的 case 分配巨大角表
    if variant == "blockwise_freq":
        max_pos = float(max(1, block_size) - 1)
    else:
        max_pos = float(seq_len - 1) if seq_len else 0.0
    max_inv = float(np.max(np.abs(inv))) if inv.size else 0.0
    if max_pos * max_inv > config.angle_overflow_threshold:
        raise StageError(E_ANGLE_OVERFLOW, STAGE_ANGLE, case_id,
                         f"角度上界 {max_pos * max_inv:.6e} 超过阈值 "
                         f"{config.angle_overflow_threshold:.6e}",
                         field="seq_len",
                         detail={"max_abs_angle_bound": max_pos * max_inv})
    if variant == "blockwise_freq":
        bs = max(1, block_size)
        blk = pos // bs
        local = pos - blk * bs
        decay = base_theta ** (-2.0 * blk / head_dim)
        angles = local[:, None] * inv[None, :] * decay[:, None]
    else:
        angles = pos[:, None] * inv[None, :]
    angles = _apply_rounding(np.asarray(angles, dtype=_F64), config.rounding_mode)

    # 越界保护：角度绝对值超阈值
    max_abs = float(np.max(np.abs(angles))) if angles.size else 0.0
    if max_abs > config.angle_overflow_threshold:
        raise StageError(E_ANGLE_OVERFLOW, STAGE_ANGLE, case_id,
                         f"角度越界 max|angle|={max_abs:.6e} > "
                         f"{config.angle_overflow_threshold:.6e}",
                         field="seq_len",
                         detail={"max_abs_angle": max_abs})

    # 累计角度 vs 理论角度（longdouble 参考）核对
    ref = _theoretical_angles(variant, seq_len, head_dim, base_theta,
                              pos_scale, block_size)
    denom = np.maximum(np.abs(ref), 1.0)
    rel_err = float(np.max(np.abs(angles.astype(np.longdouble) - ref) / denom))
    if rel_err > config.angle_tol:
        raise StageError(E_ANGLE_OVERFLOW, STAGE_ANGLE, case_id,
                         f"累计角度超容差 rel_err={rel_err:.3e} > {config.angle_tol:.3e}",
                         field="seq_len", detail={"angle_rel_err": rel_err})

    diag = {
        "subnormal_guard_triggered": bool(guard_hit),
        "rounding_mode": config.rounding_mode,
        "max_abs_angle": max_abs,
        "angle_rel_err": rel_err,
        "half_dim": head_dim // 2,
        "pairing": "interleaved(2i,2i+1)",
    }
    return angles, diag


def _theoretical_angles(variant, seq_len, head_dim, base_theta, pos_scale,
                        block_size):
    """理论角度参考（longdouble 逐步累计），用于核对累计角度。"""
    ld = np.longdouble
    half = head_dim // 2
    idx = np.arange(half, dtype=ld)
    if variant == "ntk_aware" and head_dim > 2:
        theta_eff = ld(base_theta) * ld(pos_scale) ** (ld(head_dim) / ld(head_dim - 2))
    else:
        theta_eff = ld(base_theta)
    inv = theta_eff ** (-ld(2.0) * idx / ld(head_dim))
    if variant == "position_interpolation":
        inv = inv / ld(pos_scale)
    pos = np.arange(seq_len, dtype=ld)
    if variant == "blockwise_freq":
        bs = max(1, int(block_size))
        blk = pos // ld(bs)
        local = pos - blk * ld(bs)
        decay = ld(base_theta) ** (-ld(2.0) * blk / ld(head_dim))
        return local[:, None] * inv[None, :] * decay[:, None]
    return pos[:, None] * inv[None, :]


def apply_rotation(x, angles, case_id, head_dim):
    """对张量 x (..., seq_len, head_dim) 按交错半维配对施加旋转。

    注入点（rotate 阶段）：head_dim 为奇数时半维配对越界。
    该异常在 pipeline 的 rotate 阶段捕获并记录，此处只做形状守卫。
    """
    half = angles.shape[1]
    if head_dim != 2 * half:
        # 由 pipeline 在 rotate 阶段转为 E_INJECT_ROTATE；这里防御性报错
        raise StageError(E_PARAM_INVALID, STAGE_ANGLE, case_id,
                         f"head_dim={head_dim} 与角表半维 {half} 不配对",
                         field="head_dim")
    xe = x[..., 0::2]
    xo = x[..., 1::2]
    cos = np.cos(angles)
    sin = np.sin(angles)
    out = np.empty_like(x)
    out[..., 0::2] = xe * cos - xo * sin
    out[..., 1::2] = xe * sin + xo * cos
    return out
