from __future__ import annotations

from typing import Any

from .models import Budget, ExecutionConfig, SchedulerConfig, ToleranceConfig
from .samplegen import conflict_demo_jobs, default_sample_jobs
from .service import QConvService


def conflict_budget() -> Budget:
    return Budget(
        mac_budget=2_000_000,
        memory_budget_bytes=2_600_000,
        worker_slots=3,
    )


def normal_budget() -> Budget:
    return Budget(
        mac_budget=20_000_000,
        memory_budget_bytes=8_000_000,
        worker_slots=6,
    )


def run_budget_conflict_demo(service: QConvService) -> dict[str, Any]:
    jobs = conflict_demo_jobs()
    artifact = service.run_records(
        [job.to_dict() for job in jobs],
        conflict_budget(),
        SchedulerConfig(max_workers_per_task=2),
        ToleranceConfig(atol=0.05, rtol=0.01),
        ExecutionConfig(max_processes=3),
    )
    artifact["demo"] = {
        "name": "budget-conflict",
        "expectation": [
            "E_MAC_BUDGET",
            "E_MEMORY_LIMIT",
            "E_WORKER_SLOTS",
            "coarse quant tier may choose reduced reduction",
        ],
    }
    return artifact


def run_fault_attribution_demo(service: QConvService) -> dict[str, Any]:
    jobs = default_sample_jobs()
    target = next(job for job in jobs if job.job_id == "job_balanced_conv")
    injection = {
        "job_id": target.job_id,
        "chunk_id": 0,
        "stage": "fixed_mac",
        "code": "E_INJECTED_FAULT",
        "message": "demo fault injected before fixed-point MAC accumulation",
    }
    artifact = service.run_records(
        [job.to_dict() for job in jobs],
        normal_budget(),
        SchedulerConfig(max_workers_per_task=2),
        ToleranceConfig(atol=0.05, rtol=0.01),
        ExecutionConfig(max_processes=6, fault_injections=(injection,)),
    )
    artifact["demo"] = {
        "name": "fault-attribution",
        "injected": injection,
        "expectation": [
            "target job reports job_id, chunk_id and stage",
            "other jobs retain independent result summaries",
        ],
    }
    return artifact

