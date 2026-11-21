"""确定性四级生成流水：

  1. cond_embed       文本条件嵌入（prompt_seed + prompt 哈希驱动）
  2. noise_construct  初始噪声构造（prompt_seed 派生种子驱动）
  3. chunk_denoise    按 block_grid 分块逐步去噪 + 边界归一化（整块口径统计量）
  4. decode           潜帧解码为 RGB 图像

同一 task_id 与同一配置下逐字节可复现：所有随机性来自 prompt_seed
显式派生的子种子，不依赖系统时间、字典遍历顺序或任何未声明状态。

同时提供整块参考路径（ref_mode="whole"）：分块路径的归一化统计量
按整块口径在拼接后的完整潜帧上计算，因此 float64 下逐块拼接结果
与一次性整块去噪结果逐元素（bit 级）一致。
"""

from __future__ import annotations

import hashlib

import numpy as np

from .errors import (
    StageError,
    STAGE_COND_EMBED, STAGE_NOISE_CONSTRUCT, STAGE_CHUNK_DENOISE, STAGE_DECODE,
    E_BAD_PROMPT_SEED, E_BAD_LATENT_SHAPE, E_BLOCK_GRID_INDIVISIBLE,
    E_BAD_SAMPLER_STEPS, E_UNKNOWN_DECODE_MODE, E_CHUNK_MISMATCH,
)
from .profiles import get_profile, validate_quant_params, PROFILE_FLOAT64

DECODE_MODES = ("tanh", "sigmoid", "linear")

_SEED_MOD = 2 ** 63


# ---------------------------------------------------------------- 种子派生

def _prompt_hash(prompt: str) -> int:
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _derive_seed(prompt_seed: int, prompt: str, stage_tag: int) -> int:
    """由 prompt_seed 确定性派生各阶段子种子（纯函数，无外部状态）。"""
    mixed = (prompt_seed * 6364136223846793005
             + _prompt_hash(prompt) + stage_tag * 1442695040888963407) % _SEED_MOD
    return mixed


# ---------------------------------------------------------------- 阶段 1：条件嵌入

def stage_cond_embed(task, cond_dim: int, profile) -> np.ndarray:
    """文本条件向量：单位范数、形状 (cond_dim,)。"""
    if not (isinstance(task.prompt_seed, int) and 0 <= task.prompt_seed < 2 ** 31):
        raise StageError(
            STAGE_COND_EMBED, E_BAD_PROMPT_SEED, task.task_id,
            f"prompt_seed must be int in [0, 2**31), got {task.prompt_seed!r}",
            {"prompt_seed": task.prompt_seed})
    rng = np.random.default_rng(_derive_seed(task.prompt_seed, task.prompt, 1))
    cond = rng.standard_normal(cond_dim)
    norm = float(np.linalg.norm(cond))
    cond = cond / norm
    return profile.cast(cond)


# ---------------------------------------------------------------- 阶段 2：初始噪声

def _validate_latent_shape(task) -> None:
    shape = task.latent_shape
    ok = (isinstance(shape, (list, tuple)) and len(shape) == 3
          and all(isinstance(d, int) for d in shape)
          and shape[0] in (1, 3, 4)
          and shape[1] >= 8 and shape[2] >= 8)
    if not ok:
        raise StageError(
            STAGE_NOISE_CONSTRUCT, E_BAD_LATENT_SHAPE, task.task_id,
            f"latent_shape must be [C,H,W] with C in (1,3,4), H,W >= 8, got {shape!r}",
            {"latent_shape": list(shape) if isinstance(shape, (list, tuple)) else shape})


def stage_noise_construct(task, profile) -> np.ndarray:
    """初始潜空间噪声：形状 latent_shape，尺度 noise_scale。"""
    _validate_latent_shape(task)
    rng = np.random.default_rng(_derive_seed(task.prompt_seed, task.prompt, 2))
    latent = rng.standard_normal(tuple(task.latent_shape)) * float(task.noise_scale)
    return profile.cast(latent)


# ---------------------------------------------------------------- 阶段 3：分块去噪 + 边界归一化

def _cond_channel_bias(task, cond: np.ndarray, channels: int) -> np.ndarray:
    """把条件向量确定性地投影为每通道偏置，形状 (C,1,1)。"""
    rng = np.random.default_rng(_derive_seed(task.prompt_seed, task.prompt, 3))
    proj = rng.standard_normal((cond.shape[0], channels)) / np.sqrt(cond.shape[0])
    bias = cond @ proj
    return bias.reshape(channels, 1, 1)


def _denoise_op(x: np.ndarray, bias: np.ndarray, step: int, noise_scale: float) -> np.ndarray:
    """单步去噪算子：纯逐元素运算，对分块/整块语义完全一致。"""
    step_size = noise_scale / (step + 1)
    return x - step_size * np.tanh(x * 0.5 + bias)


def _validate_grid(task) -> None:
    _, h, w = task.latent_shape
    gh, gw = task.block_grid
    if not (isinstance(task.sampler_steps, int) and task.sampler_steps >= 1):
        raise StageError(
            STAGE_CHUNK_DENOISE, E_BAD_SAMPLER_STEPS, task.task_id,
            f"sampler_steps must be >= 1, got {task.sampler_steps!r}",
            {"sampler_steps": task.sampler_steps})
    if gh < 1 or gw < 1 or h % gh != 0 or w % gw != 0:
        raise StageError(
            STAGE_CHUNK_DENOISE, E_BLOCK_GRID_INDIVISIBLE, task.task_id,
            f"block_grid {task.block_grid} does not divide latent HxW=({h},{w})",
            {"block_grid": list(task.block_grid), "latent_hw": [h, w]})


def _block_bounds(length: int, parts: int) -> list:
    step = length // parts
    return [(i * step, (i + 1) * step) for i in range(parts)]


def boundary_mask(shape, block_grid) -> np.ndarray:
    """分块边界元素掩码：内部块边界两侧各 1 像素。"""
    _, h, w = shape
    gh, gw = block_grid
    mask = np.zeros(shape, dtype=bool)
    for lo, hi in _block_bounds(h, gh)[1:]:
        mask[:, lo - 1:lo + 1, :] = True
    for lo, hi in _block_bounds(w, gw)[1:]:
        mask[:, :, lo - 1:lo + 1] = True
    return mask


def _normalize_whole_frame(latent: np.ndarray):
    """整块口径归一化统计量：在完整潜帧上求均值/标准差。"""
    mean = float(np.mean(latent))
    std = float(np.std(latent))
    if std == 0.0:
        std = 1.0
    return mean, std


def denoise_whole(latent0, cond, task, profile):
    """整块参考路径：一次性整块去噪 + 整块归一化。"""
    _validate_grid(task)
    bias = _cond_channel_bias(task, cond, task.latent_shape[0])
    latent = np.array(latent0, dtype=np.float64, copy=True)
    for step in range(task.sampler_steps):
        latent = _denoise_op(latent, bias, step, float(task.noise_scale))
    denoised = profile.cast(latent)
    mean, std = _normalize_whole_frame(denoised)
    normed = profile.cast((denoised - mean) / std)
    return denoised, normed, {"mean": mean, "std": std}


def denoise_chunked(latent0, cond, task, profile):
    """分块路径：逐块去噪；归一化统计量按整块口径在拼接后的完整潜帧上计算，
    再逐块应用，因此与整块路径在 float64 下逐元素一致。"""
    _validate_grid(task)
    c, h, w = task.latent_shape
    gh, gw = task.block_grid
    bias = _cond_channel_bias(task, cond, c)
    latent = np.array(latent0, dtype=np.float64, copy=True)
    rows = _block_bounds(h, gh)
    cols = _block_bounds(w, gw)
    for step in range(task.sampler_steps):
        for r0, r1 in rows:
            for c0, c1 in cols:
                block = latent[:, r0:r1, c0:c1]
                latent[:, r0:r1, c0:c1] = _denoise_op(block, bias, step,
                                                      float(task.noise_scale))
    denoised = profile.cast(latent)
    # 整块口径统计量：在拼接后的完整潜帧上计算
    mean, std = _normalize_whole_frame(denoised)
    normed = np.empty_like(denoised)
    for r0, r1 in rows:
        for c0, c1 in cols:
            normed[:, r0:r1, c0:c1] = (denoised[:, r0:r1, c0:c1] - mean) / std
    normed = profile.cast(normed)
    return denoised, normed, {"mean": mean, "std": std}


def compare_chunked_vs_whole(chunked, whole, task, tolerance: float):
    """逐元素对照分块拼接潜帧与整块潜帧；越界则抛 E_CHUNK_MISMATCH 并给出
    首个越界 block 坐标。返回边界元素偏差明细。"""
    shape = task.latent_shape
    gh, gw = task.block_grid
    diff = np.abs(chunked - whole)
    mask = boundary_mask(shape, task.block_grid)
    bnd_diff = diff[mask]
    ref_abs = np.abs(whole[mask])
    rel = bnd_diff / np.maximum(ref_abs, np.finfo(np.float64).tiny)
    metrics = {
        "boundary_max_abs_dev": float(bnd_diff.max()) if bnd_diff.size else 0.0,
        "boundary_max_rel_dev": float(rel.max()) if rel.size else 0.0,
        "frame_max_abs_dev": float(diff.max()),
        "elementwise_equal": bool(np.array_equal(chunked, whole)),
    }
    if float(diff.max()) > tolerance:
        rows = _block_bounds(shape[1], gh)
        cols = _block_bounds(shape[2], gw)
        first = None
        for bi, (r0, r1) in enumerate(rows):
            for bj, (c0, c1) in enumerate(cols):
                if float(diff[:, r0:r1, c0:c1].max()) > tolerance:
                    first = [bi, bj]
                    break
            if first is not None:
                break
        raise StageError(
            STAGE_CHUNK_DENOISE, E_CHUNK_MISMATCH, task.task_id,
            f"chunked latent deviates from whole-frame reference at block {first}",
            {"first_block": first, "frame_max_abs_dev": metrics["frame_max_abs_dev"],
             "tolerance": tolerance})
    return metrics


# ---------------------------------------------------------------- 阶段 4：解码

def stage_decode(latent, task, profile):
    """潜帧 -> RGB float 图（[H,W,3]，[0,1]）。量化参数校验归因到本阶段。"""
    validate_quant_params(task)
    if task.decode_mode not in DECODE_MODES:
        raise StageError(
            STAGE_DECODE, E_UNKNOWN_DECODE_MODE, task.task_id,
            f"unknown decode_mode: {task.decode_mode!r}",
            {"decode_mode": task.decode_mode, "known_modes": list(DECODE_MODES)})
    x = np.asarray(latent, dtype=np.float64)
    x = x[:3] if x.shape[0] >= 3 else np.repeat(x, 3, axis=0)[:3]
    if task.decode_mode == "tanh":
        img = (np.tanh(x) + 1.0) * 0.5
    elif task.decode_mode == "sigmoid":
        img = 1.0 / (1.0 + np.exp(-x))
    else:  # linear
        img = np.clip((x + 3.0) / 6.0, 0.0, 1.0)
    img = profile.cast(img)
    return np.transpose(img, (1, 2, 0))


def to_rgb_uint8(img_float) -> np.ndarray:
    return np.rint(np.clip(img_float, 0.0, 1.0) * 255.0).astype(np.uint8)


# ---------------------------------------------------------------- 任务级编排

def run_task_pipeline(task, cfg):
    """执行单个任务的活动路径（配置精度档 + 分块）与参考路径（float64 整块）。

    返回 (active, reference, boundary_metrics)：
      active/reference 均为 dict，含各级输出：
      cond / latent0 / denoised / normed / quantized / decoded。
    """
    from .profiles import DtypeProfile  # 局部导入避免循环

    active_profile = get_profile(task)
    ref_profile = DtypeProfile(PROFILE_FLOAT64)

    # 参考路径：float64 整块（本快照内参考真值）
    ref_cond = stage_cond_embed(task, cfg.cond_dim, ref_profile)
    ref_latent0 = stage_noise_construct(task, ref_profile)
    ref_denoised, ref_normed, ref_stats = denoise_whole(
        ref_latent0, ref_cond, task, ref_profile)
    ref_quantized = ref_normed  # float64 档量化为恒等
    ref_decoded = stage_decode(ref_quantized, task, ref_profile)

    # 活动路径：配置精度档 + 分块
    cond = stage_cond_embed(task, cfg.cond_dim, active_profile)
    latent0 = stage_noise_construct(task, active_profile)
    denoised, normed, stats = denoise_chunked(latent0, cond, task, active_profile)
    quantized = active_profile.cast_quantize(normed)  # 定点量化级
    decoded = stage_decode(quantized, task, active_profile)

    # float64 活动档下，分块路径必须与整块参考逐元素一致
    if active_profile.name == PROFILE_FLOAT64:
        boundary_metrics = compare_chunked_vs_whole(
            normed, ref_normed, task, cfg.chunk_tolerance)
    else:
        # 其他精度档同样给出分块/整块对照（同档内语义一致）
        whole_denoised, whole_normed, _ = denoise_whole(
            latent0, cond, task, active_profile)
        boundary_metrics = compare_chunked_vs_whole(
            normed, whole_normed, task, cfg.chunk_tolerance)

    active = {
        "cond": cond, "latent0": latent0, "denoised": denoised,
        "normed": normed, "quantized": quantized, "decoded": decoded,
        "norm_stats": stats, "profile": active_profile.describe(),
    }
    reference = {
        "cond": ref_cond, "latent0": ref_latent0, "denoised": ref_denoised,
        "normed": ref_normed, "quantized": ref_quantized, "decoded": ref_decoded,
        "norm_stats": ref_stats, "profile": ref_profile.describe(),
    }
    return active, reference, boundary_metrics
