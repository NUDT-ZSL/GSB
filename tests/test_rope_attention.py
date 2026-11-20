from __future__ import annotations

import numpy as np
import pytest

from rope_attention.attention import (
    NumericTolerance,
    blocked_attention,
    compare_blocked_to_full,
    full_attention,
    merge_softmax_stats,
)
from rope_attention.config import parse_config
from rope_attention.data import sample_configs
from rope_attention.rope import (
    compute_position_angles,
    rotate_qk,
    validate_config_for_task,
)
from rope_attention.util import hash_array


def test_rope_interleaved_pairing_and_byte_reproducibility(sample_inputs) -> None:
    tasks, tensors = sample_inputs
    task = next(item for item in tasks if item.case_id == "case_a_causal")
    config = parse_config(sample_configs()["v1"])
    validate_config_for_task(task, config)
    angles_a = compute_position_angles(task, config)
    angles_b = compute_position_angles(task, config)
    q_a, k_a = rotate_qk(tensors[task.case_id].q, tensors[task.case_id].k, angles_a)
    q_b, k_b = rotate_qk(tensors[task.case_id].q, tensors[task.case_id].k, angles_b)
    assert angles_a.tobytes() == angles_b.tobytes()
    assert q_a.tobytes() == q_b.tobytes()
    assert k_a.tobytes() == k_b.tobytes()
    assert hash_array(q_a) == hash_array(q_b)


def test_rotation_uses_even_odd_pairs() -> None:
    q = np.asarray([[[1.0, 0.0, 2.0, 0.0]]], dtype=np.float64)
    k = q.copy()
    angles = np.full((1, 1, 2), np.pi / 4.0, dtype=np.float64)
    q_rot, k_rot = rotate_qk(q, k, angles)
    cosine = np.cos(np.pi / 4.0)
    sine = np.sin(np.pi / 4.0)
    expected = np.asarray([[[cosine, sine, 2 * cosine, 2 * sine]]])
    assert np.allclose(q_rot, expected, atol=0, rtol=0)
    assert np.allclose(k_rot, expected, atol=0, rtol=0)


def test_blocked_attention_matches_full_and_mergeable_denominators(sample_inputs) -> None:
    tasks, tensors = sample_inputs
    task = next(item for item in tasks if item.case_id == "case_c_causal")
    config = parse_config(sample_configs()["v3"])
    validate_config_for_task(task, config)
    angles = compute_position_angles(task, config)
    q_rot, k_rot = rotate_qk(tensors[task.case_id].q, tensors[task.case_id].k, angles)
    blocked = blocked_attention(
        q_rot,
        k_rot,
        tensors[task.case_id].v,
        block_size=config.block_size,
        causal=task.causal,
        head_dim=task.head_dim,
    )
    full = full_attention(
        q_rot,
        k_rot,
        tensors[task.case_id].v,
        causal=task.causal,
        head_dim=task.head_dim,
    )
    comparison = compare_blocked_to_full(
        blocked,
        full,
        tolerance=NumericTolerance(),
    )
    assert comparison.within_tolerance
    assert comparison.max_abs_output_deviation < 1e-12
    merged = []
    for stats in blocked.block_stats:
        maximum, total = merge_softmax_stats(stats)
        merged.append((maximum, total))
    merged_sums = np.concatenate([item[1] for item in merged], axis=1)
    assert np.allclose(merged_sums, full.denominator, atol=1e-12, rtol=1e-10)


def test_causal_path_cannot_observe_future_values(sample_inputs) -> None:
    tasks, tensors = sample_inputs
    task = next(item for item in tasks if item.case_id == "case_a_causal")
    config = parse_config(sample_configs()["v1"])
    angles = compute_position_angles(task, config)
    q_rot, k_rot = rotate_qk(tensors[task.case_id].q, tensors[task.case_id].k, angles)
    baseline = blocked_attention(
        q_rot,
        k_rot,
        tensors[task.case_id].v,
        block_size=config.block_size,
        causal=True,
        head_dim=task.head_dim,
    )
    changed_v = tensors[task.case_id].v.copy()
    changed_v[:, 8:, :] = 12345.0
    changed = blocked_attention(
        q_rot,
        k_rot,
        changed_v,
        block_size=config.block_size,
        causal=True,
        head_dim=task.head_dim,
    )
    assert baseline.output[:, :8, :].tobytes() == changed.output[:, :8, :].tobytes()


def test_config_task_mismatch_is_rejected(sample_inputs) -> None:
    tasks, _ = sample_inputs
    task = next(item for item in tasks if item.case_id == "case_a_causal")
    config = parse_config(sample_configs()["v2"])
    with pytest.raises(Exception) as captured:
        validate_config_for_task(task, config)
    assert captured.value.code == "E_CONFIG_CASE_MISMATCH"

