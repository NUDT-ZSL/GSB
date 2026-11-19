from __future__ import annotations

from pathlib import Path

import numpy as np

from qconv.demo import normal_budget
from qconv.executor import execute_plan
from qconv.models import (
    ExecutionConfig,
    SchedulerConfig,
    ToleranceConfig,
)
from qconv.samplegen import default_sample_jobs
from qconv.scheduler import schedule_batch
from qconv.service import QConvService


def test_parallel_execution_summary_is_deterministic() -> None:
    jobs = default_sample_jobs()
    budget = normal_budget()
    config = SchedulerConfig(max_workers_per_task=2)
    scheduled = schedule_batch(jobs, budget, config)
    execution_config = ExecutionConfig(max_processes=4)
    outputs_a, summary_a = execute_plan(
        scheduled["jobs_by_id"], scheduled["plan"], execution_config
    )
    outputs_b, summary_b = execute_plan(
        scheduled["jobs_by_id"], scheduled["plan"], execution_config
    )
    assert summary_a == summary_b
    assert sorted(outputs_a) == sorted(outputs_b)
    for job_id in outputs_a:
        assert np.array_equal(outputs_a[job_id], outputs_b[job_id])
    assert summary_a["chunks_failed"] == 0


def test_error_spectrum_and_tolerance_location(tmp_path: Path) -> None:
    job = default_sample_jobs()[0]
    service = QConvService(tmp_path / "snap", tmp_path / "cache")
    artifact = service.run_records(
        [job.to_dict()],
        normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(atol=0.0, rtol=0.0),
        ExecutionConfig(max_processes=1),
    )
    assert artifact["status"] == "tolerance_exceeded"
    assert artifact["code"] == "E_TOLERANCE_EXCEEDED"
    assert artifact["self_check"]["ok"]
    diagnostic = artifact["error_spectrum"]["jobs"][0]
    assert [stage["stage"] for stage in diagnostic["stages"]] == [
        "quantized_input",
        "fixed_mac",
        "accumulator_reduction",
        "dequantized_output",
    ]
    assert diagnostic["dominant_stage"] in {
        "quantized_input",
        "fixed_mac",
        "accumulator_reduction",
    }
    assert diagnostic["dominant_trace"]
    assert diagnostic["violation_count"] > 0
    violation = diagnostic["violations"][0]
    assert len(violation["index"]) == 4
    assert violation["abs_diff"] > violation["allowed"]
    assert violation["stage"] == "dequantized_output"
    assert diagnostic["stages"][0]["abs_error_quantiles"].keys() == {
        "p50",
        "p90",
        "p99",
    }


def test_worker_failure_is_attributed_and_isolated(tmp_path: Path) -> None:
    jobs = default_sample_jobs()[:2]
    baseline_service = QConvService(tmp_path / "snap-a", tmp_path / "cache-a")
    baseline = baseline_service.run_records(
        [job.to_dict() for job in jobs],
        normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
        ExecutionConfig(max_processes=2),
        commit_snapshot=False,
    )
    target = jobs[0]
    fault_service = QConvService(tmp_path / "snap-b", tmp_path / "cache-b")
    faulted = fault_service.run_records(
        [job.to_dict() for job in jobs],
        normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
        ExecutionConfig(
            max_processes=2,
            fault_injections=(
                {
                    "job_id": target.job_id,
                    "chunk_id": 0,
                    "stage": "fixed_mac",
                    "code": "E_INJECTED_FAULT",
                },
            ),
        ),
        commit_snapshot=False,
    )
    assert faulted["status"] == "partial_failure"
    assert faulted["self_check"]["ok"]
    assert faulted["execution"]["failed_job_ids"] == [target.job_id]
    assert faulted["execution"]["completed_job_ids"] == [jobs[1].job_id]
    error = faulted["execution"]["worker_errors"][0]
    assert error["job_id"] == target.job_id
    assert error["chunk_id"] == 0
    assert error["stage"] == "fixed_mac"
    assert error["code"] == "E_INJECTED_FAULT"
    baseline_other = next(
        item for item in baseline["execution"]["jobs"] if item["job_id"] == jobs[1].job_id
    )
    faulted_other = next(
        item for item in faulted["execution"]["jobs"] if item["job_id"] == jobs[1].job_id
    )
    assert baseline_other == faulted_other
    assert baseline["execution"]["result_digest"] != faulted["execution"]["result_digest"]

