from __future__ import annotations

import copy

import pytest

from latent_diag.config import validate_config
from latent_diag.errors import EngineError
from latent_diag.scheduler import admit_tasks, plan_invalidations
from latent_diag.task import TaskSpec, validate_manifest


def _payload(task_id: str, **overrides):
    payload = {
        "task_id": task_id,
        "prompt": "red fox",
        "prompt_seed": 123,
        "latent_shape": [4, 16, 24],
        "block_grid": [2, 2],
        "sampler_steps": 3,
        "noise_scale": 0.5,
        "quant_scale": 1024,
        "rounding_mode": "nearest_even",
        "dtype_profile": "float64",
        "revision": 1,
        "depends_on": [],
    }
    payload.update(overrides)
    return payload


def test_validation_business_codes(sample_config):
    duplicate = [
        TaskSpec.from_dict(_payload("same")),
        TaskSpec.from_dict(_payload("same")),
    ]
    with pytest.raises(EngineError) as caught:
        validate_manifest(duplicate, sample_config)
    assert caught.value.code == "E_DUPLICATE_TASK_ID"

    with pytest.raises(EngineError) as caught:
        TaskSpec.from_dict(_payload("shape", noise_shape=[4, 8, 24]))
    assert caught.value.code == "E_LATENT_SHAPE_MISMATCH"

    with pytest.raises(EngineError) as caught:
        TaskSpec.from_dict(_payload("grid", block_grid=[3, 3]))
    assert caught.value.code == "E_BLOCK_GRID_NOT_DIVISIBLE"

    with pytest.raises(EngineError) as caught:
        TaskSpec.from_dict(_payload("quant", quant_scale=0))
    assert caught.value.code == "E_QUANT_SCALE_INVALID"

    bad_budget = copy.deepcopy(sample_config.raw)
    bad_budget["budget"]["max_total_denoise_iterations"] = 0
    with pytest.raises(EngineError) as caught:
        validate_config(bad_budget)
    assert caught.value.code == "E_BUDGET_INVALID"


def test_dual_budget_priority_and_rejections(sample_config, sample_tasks):
    iteration_config = copy.deepcopy(sample_config.raw)
    iteration_config["budget"]["max_total_denoise_iterations"] = 50
    admitted = admit_tasks(sample_tasks, validate_config(iteration_config))
    assert admitted.rejected
    assert {item["code"] for item in admitted.rejected.values()} == {"E_ITER_BUDGET"}
    assert admitted.total_iterations <= 50

    memory_config = copy.deepcopy(sample_config.raw)
    memory_config["budget"]["max_resident_latent_bytes"] = 700_000
    admitted_memory = admit_tasks(sample_tasks, validate_config(memory_config))
    assert admitted_memory.rejected
    assert {item["code"] for item in admitted_memory.rejected.values()} == {
        "E_MEMORY_BUDGET"
    }

    both = copy.deepcopy(sample_config.raw)
    both["budget"]["max_total_denoise_iterations"] = 1
    both["budget"]["max_resident_latent_bytes"] = 1
    both["budget"]["priority"] = ["iterations", "memory"]
    first = admit_tasks([sample_tasks[0]], validate_config(both))
    assert first.rejected[sample_tasks[0].task_id]["code"] == "E_ITER_BUDGET"
    both["budget"]["priority"] = ["memory", "iterations"]
    second = admit_tasks([sample_tasks[0]], validate_config(both))
    assert second.rejected[sample_tasks[0].task_id]["code"] == "E_MEMORY_BUDGET"


def test_invalidation_includes_downstream_and_is_disjoint(sample_tasks):
    changed = copy.deepcopy(sample_tasks)
    target = next(task for task in changed if task.task_id == "t002_city_f32")
    changed[changed.index(target)] = TaskSpec.from_dict(
        {**target.storage_dict(), "dtype_profile": "float64", "revision": 2}
    )
    plan = plan_invalidations(sample_tasks, changed)
    assert plan.affected == ["t002_city_f32", "t004_robot_f64"]
    assert set(plan.affected).isdisjoint(plan.reuse_candidates)
    assert set(plan.affected) | set(plan.reuse_candidates) == {
        task.task_id for task in changed
    }

    revision_only = copy.deepcopy(sample_tasks)
    first_task = revision_only[0]
    revision_only[0] = TaskSpec.from_dict(
        {**first_task.storage_dict(), "revision": first_task.revision + 1}
    )
    bump_plan = plan_invalidations(sample_tasks, revision_only)
    assert bump_plan.affected == [first_task.task_id]
    assert "revision_bump_only" in bump_plan.reasons[first_task.task_id]

