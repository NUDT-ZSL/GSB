from __future__ import annotations

from dataclasses import replace

from numpy.testing import assert_array_equal

from latent_diag.arrayio import BlobStore
from latent_diag.hashing import hash_array
from latent_diag.mathops import make_ops
from latent_diag.pipeline import (
    block_schedule,
    compute_task,
    condition_embedding,
    decode_rgb,
    initial_noise,
)
from latent_diag.task import TaskSpec


def _task(task_id: str = "determinism", **overrides):
    payload = {
        "task_id": task_id,
        "prompt": "Red Fox in Quiet Forest",
        "prompt_seed": 424242,
        "latent_shape": [4, 16, 24],
        "block_grid": [2, 3],
        "sampler_steps": 4,
        "noise_scale": 0.55,
        "quant_scale": 1024,
        "rounding_mode": "nearest_even",
        "dtype_profile": "float64",
        "revision": 1,
        "depends_on": [],
    }
    payload.update(overrides)
    return TaskSpec.from_dict(payload)


def test_case_insensitive_embedding_and_noise_are_byte_deterministic(sample_config):
    lower = _task(prompt="red fox in quiet forest")
    upper = _task(prompt="RED FOX IN QUIET FOREST")
    assert_array_equal(
        condition_embedding(lower.prompt, sample_config),
        condition_embedding(upper.prompt, sample_config),
    )
    assert hash_array(initial_noise(lower)) == hash_array(initial_noise(upper))

    first = compute_task(lower, sample_config)
    second = compute_task(upper, sample_config)
    assert first["input_hash"] == second["input_hash"]
    assert first["ledger_hash"] == second["ledger_hash"]


def test_float64_blockwise_boundaries_match_full_reference(sample_config, sample_tasks):
    for task in sample_tasks:
        if task.dtype_profile != "float64":
            continue
        result = compute_task(task, sample_config)
        metrics = result["metrics"]
        assert metrics["max_abs"] == 0.0
        assert metrics["boundary_max_abs"] == 0.0
        assert metrics["profile_consistent_scheduling_max_abs"] == 0.0
        assert metrics["dominant_source"] == "none"
        assert result["status"] == "ok"
        assert metrics["tolerance_ok"] is True


def test_dtype_profiles_keep_schedule_and_decode_common_semantics(
    sample_config, tmp_path
):
    base = _task("profile_switch", prompt="blue city street at night")
    variants = [
        replace(base, dtype_profile="float64"),
        replace(base, dtype_profile="float32"),
        replace(base, dtype_profile="fixed"),
    ]
    schedule_hash = {result["ledger"]["block_schedule_hash"] for result in (
        compute_task(task, sample_config) for task in variants
    )}
    assert len(schedule_hash) == 1
    assert block_schedule(base)[0] == {
        "block_index": 0,
        "block_row": 0,
        "block_col": 0,
        "y0": 0,
        "y1": 8,
        "x0": 0,
        "x1": 8,
    }

    blobs = BlobStore(tmp_path / "cache")
    for task in variants:
        result = compute_task(task, sample_config, blobs=blobs)
        assert result["metrics"]["profile_consistent_scheduling_max_abs"] == 0.0
        artifact_by_kind = {item["kind"]: item for item in result["artifacts"]}
        candidate = blobs.load_array(
            artifact_by_kind["final_candidate"]["artifact_path"],
            artifact_by_kind["final_candidate"]["content_hash"],
        )
        ops = make_ops(task.dtype_profile, task.quant_scale, task.rounding_mode)
        rgb_float, rgb_u8 = decode_rgb(ops.to_float64(candidate), sample_config)
        assert hash_array(rgb_float) == result["output_hashes"]["rgb_float"]
        assert hash_array(rgb_u8) == result["output_hashes"]["rgb_u8"]

    float64 = compute_task(variants[0], sample_config)
    float32 = compute_task(variants[1], sample_config)
    fixed = compute_task(variants[2], sample_config)
    assert float64["metrics"]["max_abs"] == 0.0
    assert float32["metrics"]["max_abs"] > 0.0
    assert fixed["metrics"]["max_abs"] > 0.0
    assert (
        float32["output_hashes"]["rgb_float"]
        != float64["output_hashes"]["rgb_float"]
    )
    assert fixed["output_hashes"]["rgb_u8"] != float64["output_hashes"]["rgb_u8"]

