"""确定性去噪模拟、图生替代步、共享段判定、档位上采样与像素解码。

所有随机量均由 (seed, clip_id, tier, step) 经稳定哈希派生，去噪函数对
帧子集与整帧数组给出逐元素一致的结果，因此批量调度方式不影响产物。
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np

from .config import Config, Tier
from .hashing import DeterministicRNG, fingerprint_array
from .samples import Clip

TEXT = "text"
IMAGE = "image"


def motion_matrix(clip: Clip) -> np.ndarray:
    return np.stack([frame.vector() for frame in clip.frames], axis=0)


def _grid(height: int, width: int, step: int) -> np.ndarray:
    rows = np.linspace(-1.0, 1.0, height, dtype=np.float32)
    cols = np.linspace(-1.0, 1.0, width, dtype=np.float32)
    yy, xx = np.meshgrid(rows, cols, indexing="ij")
    pattern = np.sin((step + 1) * yy) * np.cos((step + 1) * xx)
    return pattern.astype(np.float32)[None, None, :, :]


def shared_step(latent: np.ndarray, frame_indices: Sequence[int], step: int,
                config: Config, tier: Tier, clip_id: str,
                motion: np.ndarray) -> np.ndarray:
    """文生/图生共享去噪计算。帧切片结果与整帧计算逐元素一致。"""
    total_frames, channels, height, width = latent.shape if latent.ndim == 4 else (
        1, *latent.shape)
    full_shape = (config.frames, channels,
                  tier.latent_shape[1], tier.latent_shape[2])
    rng = DeterministicRNG("denoise-shared", config.seed,
                           clip_id, tier.name, step)
    sigma = float(config.noise_tables[tier.name][step])
    eps_full = rng.normal(full_shape, scale=sigma)
    weight = rng.uniform((motion.shape[1], channels), -1.0, 1.0)
    guidance = config.guidance_scale * float(
        config.guidance_tables[tier.name][step])

    indices = np.asarray(list(frame_indices), dtype=np.int64)
    eps = eps_full[indices]
    motion_bias = (motion[indices] @ weight).reshape(
        len(indices), channels, 1, 1).astype(np.float32)
    spatial = _grid(tier.latent_shape[1], tier.latent_shape[2], step)
    updated = latent * (1.0 - 0.5 * sigma) \
        + guidance * (motion_bias + spatial) + eps
    return np.ascontiguousarray(updated, dtype=np.float32)


def replace_step(latent: np.ndarray, frame_indices: Sequence[int], step: int,
                 config: Config, tier: Tier, clip_id: str,
                 motion: np.ndarray) -> np.ndarray:
    """图生路径替代步：携带图像先验偏置，语义上区别于共享去噪。"""
    channels = latent.shape[1]
    rng = DeterministicRNG("denoise-replace", config.seed,
                           clip_id, tier.name, step)
    sigma = float(config.noise_tables[tier.name][step])
    full_shape = (config.frames, channels,
                  tier.latent_shape[1], tier.latent_shape[2])
    eps_full = rng.normal(full_shape, scale=sigma)
    weight = rng.uniform((motion.shape[1], channels), -1.0, 1.0)
    guidance = config.guidance_scale * float(
        config.guidance_tables[tier.name][step])
    indices = np.asarray(list(frame_indices), dtype=np.int64)
    eps = eps_full[indices]
    motion_bias = (motion[indices] @ weight).reshape(
        len(indices), channels, 1, 1).astype(np.float32)
    image_prior = 0.25 * latent / (1.0 + step)
    updated = latent * (1.0 - 0.5 * sigma) \
        + 0.5 * guidance * motion_bias + eps + image_prior
    return np.ascontiguousarray(updated, dtype=np.float32)


def upsample_latent(latent: np.ndarray, source: Tier, target: Tier) -> np.ndarray:
    """真实空间上采样（最近邻块放大），用于档位升级而非终帧缩放。"""
    scale_h = target.latent_shape[1] // source.latent_shape[1]
    scale_w = target.latent_shape[2] // source.latent_shape[2]
    if scale_h < 1 or scale_w < 1:
        raise ValueError("档位升级目标空间尺寸不得小于来源档位")
    up = np.repeat(np.repeat(latent, scale_h, axis=-2), scale_w, axis=-1)
    return np.ascontiguousarray(up, dtype=np.float32)


def checkpoint_for_tier(tier_index: int, replace_steps: int,
                        denoise_steps: int) -> int:
    """档位 t 复用上一档潜变量的检查点步序号（复用 0..checkpoint 步）。"""
    if tier_index == 0:
        return -1
    return min(denoise_steps - 1, replace_steps + tier_index)


def decode_frame(latent_frame: np.ndarray, config: Config, tier: Tier,
                 clip_id: str) -> np.ndarray:
    """确定性解码器：潜变量 8 倍上采样后线性映射到 RGB，输出 uint8。"""
    channels, _, _ = latent_frame.shape
    up = np.repeat(np.repeat(latent_frame, 8, axis=1), 8, axis=2)
    rng = DeterministicRNG("decode", config.seed, clip_id, tier.name)
    mix = rng.uniform((channels, 3), -1.0, 1.0)
    rgb = np.einsum("chw,co->ohw", up, mix)
    rgb = (np.tanh(rgb) + 1.0) * 127.5
    return np.clip(rgb, 0.0, 255.0).transpose(1, 2, 0).astype(np.uint8)


def max_channel_diff_pixels(frames_a: np.ndarray,
                            frames_b: np.ndarray) -> int:
    return int(np.abs(frames_a.astype(np.int16) - frames_b.astype(np.int16)).max())


def max_channel_diff_latent(latent_a: np.ndarray,
                            latent_b: np.ndarray) -> float:
    return float(np.abs(latent_a - latent_b).max())


def shared_segment(replace_steps: int, denoise_steps: int
                   ) -> Tuple[List[int], List[int]]:
    divergent = list(range(0, replace_steps))
    shared = list(range(replace_steps, denoise_steps))
    return divergent, shared
