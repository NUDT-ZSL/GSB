import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transfer_scheduler.config import (  # noqa: E402
    AppConfig,
    BudgetConfig,
    ExecutionConfig,
    GateConfig,
    PipelineConfig,
)
from transfer_scheduler.data import generate_task_catalog  # noqa: E402


@pytest.fixture
def tasks():
    return generate_task_catalog(master_seed=20261121, task_count=8)


@pytest.fixture
def config():
    return AppConfig(
        pipeline=PipelineConfig(),
        budget=BudgetConfig(
            parallel_slots=2,
            compute_budget=2_000_000,
            memory_budget_bytes=20_000_000,
        ),
        execution=ExecutionConfig(worker_count=2, chunksize=1, serial_precision=1e-12),
        transfer_gate=GateConfig(threshold=0.0, field_name="transfer_strength"),
        score_gate=GateConfig(threshold=0.5, field_name="evaluation_score"),
    )


@pytest.fixture
def tight_config(config):
    from dataclasses import replace

    return replace(
        config,
        budget=BudgetConfig(
            parallel_slots=2,
            compute_budget=180_000,
            memory_budget_bytes=3_000_000,
            max_queued_tasks=6,
        ),
    )
