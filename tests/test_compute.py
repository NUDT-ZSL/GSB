from __future__ import annotations

import json
from pathlib import Path

from mixedpipe.canonical import canonical_json_bytes
from mixedpipe.compute import run_job
from mixedpipe.errors import E_INJECTED_FAULT
from mixedpipe.executor import SpawnExecutor, serial_run
from mixedpipe.models import PipelineConfig, Task
from mixedpipe.samples import generate_dataset
from mixedpipe.scheduler import build_plan


def test_train_and_infer_summaries_are_byte_deterministic() -> None:
    config = PipelineConfig(max_slots=2, compute_budget=10_000_000)
    train = Task(
        "train-a",
        "train",
        "model-a",
        3,
        5,
        7,
        seed=101,
        priority=10,
        steps=4,
    )
    infer = Task(
        "infer-a",
        "infer",
        "model-a",
        3,
        5,
        7,
        seed=102,
        priority=9,
        warmup=3,
    )

    train_first = run_job(train, config)
    train_second = run_job(train, config)
    infer_first = run_job(infer, config)
    infer_second = run_job(infer, config)

    assert canonical_json_bytes(train_first) == canonical_json_bytes(train_second)
    assert canonical_json_bytes(infer_first) == canonical_json_bytes(infer_second)
    assert train_first["summary"]["loss_first"] != train_first["summary"]["loss_last"]
    assert len(train_first["summary"]["loss_sequence"]) == train.steps
    assert train_first["summary"]["grad_norm"] > 0
    assert len(infer_first["summary"]["loss_sequence"]) == infer.warmup
    assert infer_first["summary"]["grad_norm"] == 0.0


def test_spawn_pool_matches_serial_and_attributes_fault() -> None:
    config = PipelineConfig(
        max_slots=2,
        compute_budget=10_000_000,
        memory_budget_mb=8,
    )
    tasks = [
        Task(
            "job-b",
            "train",
            "m1",
            2,
            4,
            5,
            seed=20,
            priority=8,
            steps=2,
        ),
        Task(
            "job-a",
            "infer",
            "m1",
            2,
            4,
            5,
            seed=21,
            priority=8,
            warmup=2,
        ),
        Task(
            "job-c",
            "infer",
            "m2",
            2,
            4,
            5,
            seed=22,
            priority=4,
            warmup=2,
            fault="score",
        ),
    ]
    plan = build_plan(tasks, config)
    execution = SpawnExecutor(config).run(plan)
    assert execution["pool_mode"] == "spawn"
    assert execution["merge_order"] == plan["execution_order"]
    assert [result["job_id"] for result in execution["results"]] == plan["execution_order"]
    assert len(execution["results"]) == len(plan["admitted_job_ids"])

    effective = [
        Task.from_dict(decision["effective_task"])
        for decision in plan["decisions"]
        if decision["decision"] == "admitted"
    ]
    baseline = {
        result["job_id"]: result for result in serial_run(effective, config)
    }
    for result in execution["results"]:
        assert canonical_json_bytes(result) == canonical_json_bytes(
            baseline[result["job_id"]]
        )
    fault = next(result for result in execution["results"] if result["job_id"] == "job-c")
    assert fault["status"] == "error"
    assert fault["business_code"] == E_INJECTED_FAULT
    assert fault["stage"] == "score"
    assert fault["details"]["job_id"] == "job-c"


def test_fixed_seed_sample_generation_is_reproducible(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = generate_dataset(first_root, force=True)
    second = generate_dataset(second_root, force=True)
    assert first["manifest_checksum"] == second["manifest_checksum"]
    assert (
        first_root / "data" / "jobs.jsonl"
    ).read_bytes() == (second_root / "data" / "jobs.jsonl").read_bytes()
    first_tensor_manifest = json.loads(
        (first_root / "data" / "tensors" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    second_tensor_manifest = json.loads(
        (second_root / "data" / "tensors" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_tensor_manifest["artifacts"] == second_tensor_manifest["artifacts"]
    assert (
        first_root / "data" / "tensors" / "features" / "job-001.npy"
    ).read_bytes() == (
        second_root / "data" / "tensors" / "features" / "job-001.npy"
    ).read_bytes()
