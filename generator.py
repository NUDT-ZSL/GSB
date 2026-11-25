#!/usr/bin/env python3
"""Deterministic sample generator for the GPU scheduling engine.

The fixed seed is part of the reproducible experiment contract.  The crafted
records intentionally cover quota, compute/memory rejection, fragmentation,
preemption, concurrency and all four injection faults.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


SEED = 20261126
ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "data" / "samples"


def build_cards():
    return {
        "schema": "gpu-scheduler/cards-v1",
        "seed": SEED,
        "cards": [
            {
                "card_id": "gpu-frag",
                "total_compute": 100,
                "total_memory": 100,
                "granularity": 10,
                "health": "healthy",
                "splittable": True,
                "labels": {"purpose": "fragmentation-demo"},
            },
            {
                "card_id": "gpu-preempt",
                "total_compute": 100,
                "total_memory": 100,
                "granularity": 10,
                "health": "healthy",
                "splittable": True,
                "labels": {"purpose": "preemption-demo"},
            },
            {
                "card_id": "gpu-small",
                "total_compute": 60,
                "total_memory": 60,
                "granularity": 10,
                "health": "healthy",
                "splittable": True,
                "labels": {"purpose": "general"},
            },
            {
                "card_id": "gpu-mp",
                "total_compute": 40,
                "total_memory": 40,
                "granularity": 10,
                "health": "healthy",
                "splittable": True,
                "labels": {"purpose": "multiprocess-contention"},
            },
            {
                "card_id": "gpu-locked",
                "total_compute": 60,
                "total_memory": 60,
                "granularity": 10,
                "health": "healthy",
                "splittable": False,
                "labels": {"purpose": "unsplittable-control"},
            },
            {
                "card_id": "gpu-dead",
                "total_compute": 80,
                "total_memory": 80,
                "granularity": 10,
                "health": "unhealthy",
                "splittable": True,
                "labels": {"purpose": "health-control"},
            },
        ],
    }


def job(job_id, compute, memory, priority, group, duration, **extra):
    record = {
        "job_id": job_id,
        "compute_request": compute,
        "memory_request": memory,
        "priority": priority,
        "quota_group": group,
        "lease_duration_ms": duration,
    }
    record.update(extra)
    return record


def build_jobs():
    frag_jobs = [
        job("job-frag-01", 20, 20, "P2", "qg-frag", 500),
        job("job-frag-02", 20, 20, "P2", "qg-frag", 100),
        job("job-frag-03", 20, 20, "P2", "qg-frag", 500),
        job("job-frag-04", 20, 20, "P2", "qg-frag", 150),
        job("job-frag-05", 20, 20, "P2", "qg-frag", 500),
        job("job-frag-target", 30, 30, "P1", "qg-frag", 300),
    ]
    preempt_jobs = [
        job("job-prey-a", 30, 40, "P2", "qg-batch", 300),
        job("job-prey-b", 30, 30, "P2", "qg-batch", 200),
        job("job-prey-c", 40, 30, "P1", "qg-batch", 500),
        job("job-high-priority", 60, 70, "P0", "qg-core", 400),
        job("job-small-fill", 60, 20, "P2", "qg-core", 1000),
    ]
    rejection_jobs = [
        job("job-quota-blocked", 40, 20, "P2", "qg-batch", 200),
        job("job-compute-too-big", 120, 10, "P2", "qg-batch", 100),
        job("job-memory-too-big", 10, 999, "P2", "qg-batch", 100),
    ]
    mp_jobs = [job(f"job-mp-{index:02d}", 20, 20, "P2", "qg-mp", 1000) for index in range(1, 7)]
    injection_jobs = [
        job("job-inj-overlap", 10, 10, "P3", "qg-batch", 1000),
    ]
    return {
        "schema": "gpu-scheduler/jobs-v1",
        "seed": SEED,
        "jobs": frag_jobs + preempt_jobs + rejection_jobs + mp_jobs + injection_jobs,
    }


def build_config(rng):
    nonce = int(rng.integers(100_000, 1_000_000))
    return {
        "schema": "gpu-scheduler/config-v1",
        "seed": SEED,
        "deterministic_nonce": nonce,
        "logical_clock_start_ms": 0,
        "round_time_budget_ms": 100,
        "max_concurrent_grants": 12,
        "renew_grace_ms": 20,
        "preemption": {
            "enabled": True,
            "max_victims": 4,
            "order": ["priority_desc", "remaining_ms_asc", "lease_id_asc"],
        },
        "quota_groups": {
            "qg-core": {"compute_limit": 240, "memory_limit": 400},
            "qg-frag": {"compute_limit": 220, "memory_limit": 300},
            "qg-batch": {"compute_limit": 120, "memory_limit": 260},
            "qg-mp": {"compute_limit": 200, "memory_limit": 200},
        },
    }


def build_injections():
    return {
        "schema": "gpu-scheduler/inject-v1",
        "seed": SEED,
        "injections": [
            {
                "injection_id": "inj-grant-overlap",
                "phase": "grant",
                "fault_type": "interval_overlap",
                "enabled": True,
                "once": True,
                "target": {
                    "round_id": "inj-grant-overlap",
                    "card_id": "gpu-frag",
                    "job_id": "job-inj-overlap",
                },
            },
            {
                "injection_id": "inj-renew-rollback",
                "phase": "renew",
                "fault_type": "generation_rollback",
                "enabled": True,
                "once": True,
                "target": {
                    "round_id": "inj-renew-rollback",
                    "job_id": "job-frag-01",
                },
            },
            {
                "injection_id": "inj-recover-imbalance",
                "phase": "recover",
                "fault_type": "ledger_imbalance",
                "enabled": True,
                "once": True,
                "target": {
                    "round_id": "inj-recover-imbalance",
                    "card_id": "gpu-frag",
                    "job_id": "job-frag-01",
                },
            },
            {
                "injection_id": "inj-reorg-digest",
                "phase": "reorganize",
                "fault_type": "reorg_digest_mismatch",
                "enabled": True,
                "once": True,
                "target": {
                    "round_id": "inj-reorg-mismatch",
                    "job_id": "job-frag-target",
                    "card_id": "gpu-frag",
                },
            },
        ],
    }


def main() -> None:
    rng = np.random.default_rng(SEED)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    documents = {
        "cards.json": build_cards(),
        "jobs.json": build_jobs(),
        "config.json": build_config(rng),
        "inject.json": build_injections(),
    }
    for filename, document in documents.items():
        path = SAMPLE_DIR / filename
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
