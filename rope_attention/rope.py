"""Deterministic position-angle and interleaved Q/K rotation layer."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .data import AttentionTask
from .errors import E_CONFIG_CASE_MISMATCH, RopeError
from .models import ParsedConfig


ROPE_PAIRING = "interleaved_even_odd"
ROPE_PAIRING_DESCRIPTION = (
    "dimension pairs are (0,1), (2,3), ...; even dimensions are the cosine "
    "components and odd dimensions are the sine components"
)


def validate_config_for_task(task: AttentionTask, config: ParsedConfig) -> None:
    """Reject silent differences between a task record and its config snapshot."""

    mismatches: dict[str, dict[str, Any]] = {}
    expected = {
        "config_version": config.source_version,
        "base_theta": config.base_theta,
        "pos_scale": config.pos_scale,
        "block_size": config.block_size,
        "pos_encoding_variant": config.effective_variant,
    }
    actual = {
        "config_version": task.config_version,
        "base_theta": task.base_theta,
        "pos_scale": task.pos_scale,
        "block_size": task.block_size,
        "pos_encoding_variant": task.pos_encoding_variant,
    }
    for field, wanted in expected.items():
        if actual[field] != wanted:
            mismatches[field] = {"task": actual[field], "config": wanted}
    if mismatches:
        raise RopeError(
            E_CONFIG_CASE_MISMATCH,
            "task record and config snapshot disagree",
            details={"case_id": task.case_id, "mismatches": mismatches},
        )
    if config.effective_variant == "block_freq":
        if config.effective_frequency_adjustment.method != "blockwise":
            raise RopeError(
                E_CONFIG_CASE_MISMATCH,
                "block_freq requires a blockwise frequency adjustment",
                details={"case_id": task.case_id},
            )
    if config.effective_variant == "ntk" and task.head_dim <= 2:
        raise RopeError(
            E_CONFIG_CASE_MISMATCH,
            "ntk variant requires head_dim greater than 2",
            details={"case_id": task.case_id, "head_dim": task.head_dim},
        )


def resolve_head_scales(config: ParsedConfig, num_heads: int, *, case_id: str) -> np.ndarray:
    scales = config.effective_per_head_scale
    if len(scales) == 1:
        values = np.full((num_heads,), scales[0], dtype=np.float64)
    elif len(scales) == num_heads:
        values = np.asarray(scales, dtype=np.float64)
    else:
        raise RopeError(
            E_CONFIG_CASE_MISMATCH,
            "per_head_scale length must be 1 or equal num_heads",
            details={
                "case_id": case_id,
                "num_heads": num_heads,
                "per_head_scale_length": len(scales),
            },
        )
    return np.ascontiguousarray(values)


def inverse_frequencies(
    *,
    head_dim: int,
    base_theta: float,
    variant: str,
) -> np.ndarray:
    """Return frequencies for dimensions [0, 2, 4, ...]."""

    if head_dim % 2:
        raise ValueError("head_dim must be even")
    exponents = np.arange(0, head_dim, 2, dtype=np.float64) / float(head_dim)
    if variant == "ntk":
        if head_dim <= 2:
            raise ValueError("ntk variant requires head_dim > 2")
        effective_base = float(base_theta) ** (float(head_dim) / float(head_dim - 2))
    elif variant in {"rope", "block_freq"}:
        effective_base = float(base_theta)
    else:
        raise ValueError(f"unsupported variant: {variant}")
    return np.power(effective_base, -exponents)


def compute_position_angles(
    task: AttentionTask,
    config: ParsedConfig,
) -> np.ndarray:
    """Compute float64 angles with shape (num_heads, seq_len, head_dim/2)."""

    validate_config_for_task(task, config)
    head_scales = resolve_head_scales(config, task.num_heads, case_id=task.case_id)
    inv_freq = inverse_frequencies(
        head_dim=task.head_dim,
        base_theta=config.base_theta,
        variant=config.effective_variant,
    )
    positions = np.arange(task.seq_len, dtype=np.float64)
    frequency = config.effective_frequency_adjustment
    if frequency.method == "identity":
        block_multiplier = np.ones((task.seq_len,), dtype=np.float64)
    elif frequency.method == "global_scale":
        block_multiplier = np.full(
            (task.seq_len,), float(frequency.factor), dtype=np.float64
        )
    elif frequency.method == "blockwise":
        if frequency.block_tokens is None:
            raise RopeError(
                E_CONFIG_CASE_MISMATCH,
                "blockwise frequency adjustment lacks block_tokens",
                details={"case_id": task.case_id},
            )
        block_ids = (positions // int(frequency.block_tokens)).astype(np.int64)
        block_multiplier = np.where(
            block_ids % 2 == 1,
            float(frequency.factor),
            1.0,
        ).astype(np.float64)
    else:
        raise RopeError(
            E_CONFIG_CASE_MISMATCH,
            "frequency adjustment method is unsupported",
            details={"case_id": task.case_id, "method": frequency.method},
        )

    angles = (
        head_scales[:, None, None]
        * float(config.pos_scale)
        * positions[None, :, None]
        * block_multiplier[None, :, None]
        * inv_freq[None, None, :]
    )
    if not np.isfinite(angles).all():
        raise RopeError(
            E_CONFIG_CASE_MISMATCH,
            "position angles overflowed or became non-finite",
            details={"case_id": task.case_id},
        )
    return np.ascontiguousarray(angles, dtype=np.float64)


def rotate_qk(
    q: np.ndarray,
    k: np.ndarray,
    angles: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply interleaved even/odd RoPE rotation to Q and K."""

    q64 = np.ascontiguousarray(q, dtype=np.float64)
    k64 = np.ascontiguousarray(k, dtype=np.float64)
    angle64 = np.ascontiguousarray(angles, dtype=np.float64)
    if q64.shape != k64.shape:
        raise ValueError("q and k shapes must match")
    if q64.ndim != 3:
        raise ValueError("q and k must have shape (heads, seq, head_dim)")
    if q64.shape[2] % 2:
        raise ValueError("head_dim must be even")
    expected_angles = (q64.shape[0], q64.shape[1], q64.shape[2] // 2)
    if angle64.shape != expected_angles:
        raise ValueError(
            f"angles shape {angle64.shape} does not match {expected_angles}"
        )
    cosine = np.cos(angle64)
    sine = np.sin(angle64)
    q_even = q64[..., 0::2]
    q_odd = q64[..., 1::2]
    k_even = k64[..., 0::2]
    k_odd = k64[..., 1::2]
    q_rot = np.empty_like(q64)
    k_rot = np.empty_like(k64)
    q_rot[..., 0::2] = q_even * cosine - q_odd * sine
    q_rot[..., 1::2] = q_even * sine + q_odd * cosine
    k_rot[..., 0::2] = k_even * cosine - k_odd * sine
    k_rot[..., 1::2] = k_even * sine + k_odd * cosine
    return np.ascontiguousarray(q_rot), np.ascontiguousarray(k_rot)


def rotation_summary(angles: np.ndarray) -> dict[str, Any]:
    """Small deterministic view suitable for CLI audit output."""

    first = angles[0]
    return {
        "angle_max": float(np.max(angles)),
        "angle_min": float(np.min(angles)),
        "head0_position0": [float(value) for value in first[0, :4]],
        "head0_position1": [float(value) for value in first[1, :4]],
        "pairing": ROPE_PAIRING,
        "shape": list(angles.shape),
    }

