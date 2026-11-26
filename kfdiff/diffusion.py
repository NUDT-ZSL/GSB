"""Deterministic condition embedding, noise phases and keyframe diffusion.

The whole-clip path and the block path share :func:`build_canonical_sources`
and :func:`generate_keyframe`.  Noise phase seeds, clip-wide normalisation
statistics, the diffusion schedule and condition/coupling fusion coefficients
therefore always come from one source with one normalisation.  This is what
makes chunked generation byte-identical to the one-shot pass.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from .clipmodel import ClipTask
from .hashing import array_digest, derive_seed, hash_json
from .text import condition_embedding, prompt_channel_projection


NOISE_NAMESPACE = "kfdiff.noise.v1"
MIX_NAMESPACE = "kfdiff.channelmix.v1"


def noise_phase_seed(task: ClipTask, ordinal: int) -> int:
    """Canonical per-keyframe noise phase seed.

    A targeted revision can supply a deterministic phase salt for selected
    ordinals; every other ordinal keeps the base phase.
    """
    salt = task.phase_salts.get(ordinal, "base")
    return derive_seed(NOISE_NAMESPACE, task.clip_id, task.noise_seed, int(ordinal), salt)


def keyframe_noise(task: ClipTask, ordinal: int) -> np.ndarray:
    seed = noise_phase_seed(task, ordinal)
    rng = np.random.default_rng(seed)
    return rng.standard_normal(task.frame_shape).astype(np.float64)


_NOISE_STATS_CACHE: Dict[str, Dict[str, Any]] = {}


def canonical_noise_stats(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fixed-seed calibration normalisation shared by every clip and path.

    Because the statistics come from the canonical calibration pool rather
    than a chunk-local (or revision-sensitive) sample, block generation and
    one-shot generation always use the exact same normalisation, and a targeted
    noise revision cannot perturb unrelated keyframes through global stats.
    """
    eps = float(config["diffusion"]["normalization_eps"])
    cache_key = f"{int(config['master_seed'])}:{eps}"
    cached = _NOISE_STATS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    seed = derive_seed(int(config["master_seed"]), "canonical-noise-stats-v1")
    sample = np.random.default_rng(seed).standard_normal(131072)
    mean = float(sample.mean())
    std = float(sample.std()) + eps
    stats = {
        "mean": mean,
        "std": std,
        "eps": eps,
        "source": "fixed_seed_calibration_pool",
        "sample_size": int(sample.size),
        "stats_hash": hash_json({"mean": mean, "std": std, "eps": eps,
                                 "source": "canonical-noise-stats-v1"}),
    }
    _NOISE_STATS_CACHE[cache_key] = stats
    return stats


def cosine_alpha_bars(steps: int, s: float = 0.008) -> np.ndarray:
    """Canonical cosine schedule sampled at ``steps`` grid positions."""
    if steps <= 1:
        return np.array([0.999], dtype=np.float64)
    reference = math.cos((s / (1.0 + s)) * math.pi * 0.5) ** 2
    bars = []
    for index in range(steps):
        progress = index / (steps - 1)
        factor = math.cos(((progress + s) / (1.0 + s)) * math.pi * 0.5) ** 2
        bars.append(min(0.9999, max(1e-4, factor / reference)))
    return np.asarray(bars, dtype=np.float64)


def effective_step_indices(steps: int, strength: float) -> np.ndarray:
    """Strided step indices for diffusion-step approximation strength."""
    strength = min(1.0, max(0.1, float(strength)))
    effective = max(2, int(round(steps * strength)))
    if effective >= steps:
        return np.arange(steps, dtype=np.int64)
    indices = np.unique(np.round(np.linspace(0, steps - 1, effective)).astype(np.int64))
    return indices


def avg_pool_five(z: np.ndarray) -> np.ndarray:
    padded = np.pad(z, ((1, 1), (1, 1), (0, 0)), mode="edge")
    return (
        padded[:-2, 1:-1, :]
        + padded[2:, 1:-1, :]
        + padded[1:-1, :-2, :]
        + padded[1:-1, 2:, :]
        + z
    ) / 5.0


def build_canonical_sources(
    task: ClipTask,
    config: Dict[str, Any],
    *,
    strength: float = 1.0,
    embedding_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build every shared canonical source for one clip plan."""
    diffusion_cfg = config["diffusion"]
    eps = float(diffusion_cfg["normalization_eps"])
    keyframe_indices = task.keyframe_indices()
    k_count = len(keyframe_indices)

    embedding_bundle = embedding_bundle or condition_embedding(task.prompt, config)
    channels = task.channels
    c_scale, c_shift = prompt_channel_projection(
        embedding_bundle["embedding"], channels, int(config["vocab_seed"])
    )

    noise_stack = np.stack([keyframe_noise(task, ordinal) for ordinal in range(k_count)], axis=0)
    noise_stats = canonical_noise_stats(config)

    alpha_bars = cosine_alpha_bars(int(task.diffusion_steps))
    step_indices = effective_step_indices(int(task.diffusion_steps), strength)
    condition_scale = float(diffusion_cfg["condition_scale"])
    coupling_weight = float(diffusion_cfg["coupling_weight"])
    fusion_coefficients: List[float] = []
    coupling_coefficients: List[float] = []
    for index in step_indices:
        ab = float(alpha_bars[int(index)])
        fusion_coefficients.append(condition_scale * math.sqrt(max(1.0 - ab, 0.0)))
        coupling_coefficients.append(coupling_weight * (1.0 - ab))
    schedule_hash = hash_json(
        {
            "kind": "cosine",
            "steps": int(task.diffusion_steps),
            "alpha_bars": [round(float(v), 10) for v in alpha_bars],
            "step_indices": [int(v) for v in step_indices],
            "strength": float(strength),
            "fusion": [round(v, 10) for v in fusion_coefficients],
            "coupling": [round(v, 10) for v in coupling_coefficients],
        }
    )

    mix_seed = derive_seed(MIX_NAMESPACE, int(config["vocab_seed"]), channels)
    rng = np.random.default_rng(mix_seed)
    channel_mix = rng.standard_normal((channels, channels)).astype(np.float64) / math.sqrt(channels)

    low_pct = float(diffusion_cfg.get("quant_low_pct", 2.0))
    high_pct = float(diffusion_cfg.get("quant_high_pct", 98.0))
    sampler_config_hash = hash_json({
        "condition_scale": condition_scale,
        "coupling_weight": coupling_weight,
        "normalization_eps": eps,
        "quant_low_pct": low_pct,
        "quant_high_pct": high_pct,
        "channel_mix_seed": mix_seed,
        "sampler": "cosine_ddim_surrogate_v1",
    })

    return {
        "task": task,
        "config": config,
        "strength": float(strength),
        "keyframe_indices": keyframe_indices,
        "embedding": embedding_bundle,
        "c_scale": c_scale.astype(np.float64),
        "c_shift": c_shift.astype(np.float64),
        "noise_stack": noise_stack,
        "noise_stats": noise_stats,
        "alpha_bars": alpha_bars,
        "step_indices": step_indices,
        "fusion_coefficients": fusion_coefficients,
        "coupling_coefficients": coupling_coefficients,
        "channel_mix": channel_mix,
        "schedule_hash": schedule_hash,
        "quant_low_pct": low_pct,
        "quant_high_pct": high_pct,
        "sampler_config_hash": sampler_config_hash,
    }


def coupling_context(sources: Dict[str, Any], ordinal: int) -> Dict[str, Any]:
    """Canonical adjacent-keyframe coupling context (order independent)."""
    noise_stack: np.ndarray = sources["noise_stack"]
    count = noise_stack.shape[0]
    channels = sources["task"].channels
    accum = np.zeros((channels,), dtype=np.float64)
    neighbors: List[int] = []
    for neighbor in (ordinal - 1, ordinal + 1):
        if 0 <= neighbor < count:
            accum += noise_stack[neighbor].mean(axis=(0, 1))
            neighbors.append(neighbor)
    if neighbors:
        accum /= len(neighbors)
    stats = sources["noise_stats"]
    context = accum / stats["std"]
    return {
        "ordinal": ordinal,
        "neighbors": neighbors,
        "links": len(neighbors),
        "context": context,
        "context_hash": array_digest(context.astype(np.float64)),
    }


def boundary_phase_record(sources: Dict[str, Any], left_ordinal: int, right_ordinal: int) -> Dict[str, Any]:
    """Shared provenance record for one block/internal keyframe boundary."""
    task: ClipTask = sources["task"]
    left_noise = sources["noise_stack"][left_ordinal]
    right_noise = sources["noise_stack"][right_ordinal]
    left_ctx = coupling_context(sources, left_ordinal)
    right_ctx = coupling_context(sources, right_ordinal)
    return {
        "left_ordinal": left_ordinal,
        "right_ordinal": right_ordinal,
        "left_frame_index": sources["keyframe_indices"][left_ordinal],
        "right_frame_index": sources["keyframe_indices"][right_ordinal],
        "left_phase_seed": noise_phase_seed(task, left_ordinal),
        "right_phase_seed": noise_phase_seed(task, right_ordinal),
        "left_noise_hash": array_digest(left_noise),
        "right_noise_hash": array_digest(right_noise),
        "left_context_hash": left_ctx["context_hash"],
        "right_context_hash": right_ctx["context_hash"],
        "noise_stats_hash": sources["noise_stats"]["stats_hash"],
        "schedule_hash": sources["schedule_hash"],
    }


def _quantize(x: np.ndarray, low_pct: float, high_pct: float) -> tuple[np.ndarray, float, float]:
    lo = float(np.percentile(x, low_pct))
    hi = float(np.percentile(x, high_pct))
    if hi - lo < 1e-6:
        lo, hi = float(x.min()), float(x.max()) + 1e-6
    scaled = (x - lo) * (255.0 / (hi - lo))
    quantized = np.clip(np.rint(scaled), 0, 255).astype(np.uint8)
    return quantized, lo, hi


def generate_keyframe(sources: Dict[str, Any], ordinal: int) -> Dict[str, Any]:
    """Run the deterministic diffusion sampler for one keyframe ordinal."""
    task: ClipTask = sources["task"]
    initial_noise = np.asarray(sources["noise_stack"][ordinal], dtype=np.float64)
    stats = sources["noise_stats"]
    coupling_info = coupling_context(sources, ordinal)
    mix = sources["channel_mix"]
    c_scale = sources["c_scale"]
    c_shift = sources["c_shift"]
    step_indices: np.ndarray = sources["step_indices"]
    alpha_bars: np.ndarray = sources["alpha_bars"]

    x = initial_noise.copy()
    trajectory = np.empty((len(step_indices),) + task.frame_shape, dtype=np.float32)
    step_stats: List[Dict[str, float]] = []

    for position, schedule_index in enumerate(step_indices):
        index = int(schedule_index)
        ab_bar = float(alpha_bars[index])
        ab_prev = float(alpha_bars[index - 1]) if index > 0 else 1.0
        fuse = float(sources["fusion_coefficients"][position])
        gamma = float(sources["coupling_coefficients"][position])

        normalized = (x - stats["mean"]) / stats["std"]
        smooth = avg_pool_five(normalized)
        mixed = np.tanh(normalized @ mix)
        modulated = np.tanh(normalized * (1.0 + 0.35 * c_scale[None, None, :]) + c_shift[None, None, :])
        condition_term = fuse * (
            0.5 * c_scale[None, None, :] * normalized + c_shift[None, None, :]
        )
        denoised = (
            0.50 * normalized
            + 0.20 * smooth
            + 0.15 * mixed
            + 0.15 * modulated
            + condition_term
        )
        x = math.sqrt(ab_prev) * denoised + math.sqrt(max(1.0 - ab_prev, 0.0)) * normalized
        trajectory[position] = x.astype(np.float32)
        step_stats.append(
            {
                "step_position": position,
                "schedule_index": index,
                "alpha_bar": round(ab_bar, 8),
                "fusion_coefficient": round(fuse, 8),
                "coupling_coefficient": round(gamma, 8),
                "state_mean": round(float(x.mean()), 8),
                "state_std": round(float(x.std()), 8),
            }
        )

    pixels, quant_lo, quant_hi = _quantize(
        x, sources["quant_low_pct"], sources["quant_high_pct"]
    )
    pixels = np.ascontiguousarray(pixels, dtype=np.uint8)
    trajectory = np.ascontiguousarray(trajectory, dtype=np.float32)

    provenance = {
        "clip_id": task.clip_id,
        "ordinal": ordinal,
        "frame_index": sources["keyframe_indices"][ordinal],
        "revision": task.revision,
        "noise_phase_seed": noise_phase_seed(task, ordinal),
        "noise_hash": array_digest(initial_noise),
        "noise_stats": stats,
        "embedding_hash": sources["embedding"]["embedding_hash"],
        "norm_stats_hash": sources["embedding"]["norm_stats_hash"],
        "schedule_hash": sources["schedule_hash"],
        "sampler_config_hash": sources["sampler_config_hash"],
        "effective_steps": int(len(step_indices)),
        "diffusion_steps": int(task.diffusion_steps),
        "strength": float(sources["strength"]),
        "step_indices": [int(v) for v in step_indices],
        "coupling": {
            "neighbors": coupling_info["neighbors"],
            "links": coupling_info["links"],
            "context_hash": coupling_info["context_hash"],
            "affects_pixels": False,
        },
        "step_stats": step_stats,
        "trajectory_hash": array_digest(trajectory),
        "pixels_hash": array_digest(pixels),
        "quant_lo": round(quant_lo, 6),
        "quant_hi": round(quant_hi, 6),
    }
    return {
        "ordinal": ordinal,
        "frame_index": provenance["frame_index"],
        "pixels": pixels,
        "trajectory": trajectory,
        "provenance": provenance,
    }


def generate_all_keyframes(
    task: ClipTask, config: Dict[str, Any], *, strength: float = 1.0
) -> Dict[str, Any]:
    """One-shot whole-clip canonical generation (reference path)."""
    sources = build_canonical_sources(task, config, strength=strength)
    frames = [generate_keyframe(sources, ordinal) for ordinal in range(task.keyframe_count)]
    return {"sources": sources, "keyframes": frames}
