"""确定性演示场景。

三个场景覆盖验收点：

* :func:`demo_budget_conflict` —— 收紧计算/内存预算，制造拒绝与降级，
  展示 E_COMPUTE_BUDGET / E_MEMORY_BUDGET / E_SLOT_BUDGET 裁决；
* :func:`demo_worker_failure` —— 故障注入制造并行 worker 异常，
  验证结构化回传、失败隔离与摘要中的不可用标记；
* :func:`demo_threshold_stability` —— 门限附近任务的重复裁决稳定性。
"""

from __future__ import annotations
from dataclasses import replace
from pathlib import Path

from .config import AppConfig, BudgetConfig, ExecutionConfig, GateConfig, PipelineConfig
from .data import generate_task_catalog, write_catalog
from .executor import FaultInjection
from .service import WorkflowService


def default_config() -> AppConfig:
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


def prepare_workspace(base_dir: str | Path, master_seed: int = 20261121) -> tuple[Path, list]:
    """生成任务清单 JSONL 与目录骨架，返回清单路径与任务列表。"""
    base = Path(base_dir)
    (base / "var" / "data").mkdir(parents=True, exist_ok=True)
    (base / "var" / "cache").mkdir(parents=True, exist_ok=True)
    (base / "var" / "snapshots").mkdir(parents=True, exist_ok=True)
    tasks = generate_task_catalog(master_seed)
    catalog_path = write_catalog(tasks, base / "var" / "data" / "tasks.jsonl")
    return catalog_path, tasks


def demo_budget_conflict(tasks: list, snapshot_dir: str | Path) -> object:
    """场景一：预算冲突拒绝与降级。"""
    tight_budget = BudgetConfig(
        parallel_slots=2,
        compute_budget=180_000,
        memory_budget_bytes=3_000_000,
        max_queued_tasks=6,
    )
    config = replace(default_config(), budget=tight_budget)
    service = WorkflowService(config, snapshot_dir)
    return service.run(
        tasks,
        run_id="demo-budget-conflict",
        plan_id="plan-budget-0001",
        in_process=True,
    )


def demo_worker_failure(tasks: list, snapshot_dir: str | Path, in_process: bool = False) -> object:
    """场景二：并行 worker 异常回传（注入两个任务的不同阶段故障）。"""
    config = default_config()
    service = WorkflowService(config, snapshot_dir)
    target_ids = sorted(task.task_id for task in tasks)[:2]
    injection = FaultInjection(
        failures={
            target_ids[0]: "adapter_train",
            target_ids[1]: "target_evaluate",
        }
    )
    return service.run(
        tasks,
        run_id="demo-worker-failure",
        plan_id="plan-failure-0001",
        fault_injection=injection,
        in_process=in_process,
        skip_serial_check=in_process is False,
    )


def demo_threshold_stability() -> dict:
    """场景三：门限附近重复裁决不翻转（严格 vs 容差带）。"""
    from .threshold import evaluate_gate

    strict_gate = GateConfig(threshold=0.5, strategy="strict", field_name="evaluation_score")
    band_gate = GateConfig(
        threshold=0.5,
        strategy="band",
        tolerance=1e-9,
        band_on_tie="reject",
        field_name="evaluation_score",
    )
    probe_values = (0.5, 0.5 + 1e-12, 0.5 - 1e-12, 0.5 + 1e-8, 0.4999)
    decisions = {"strict": [], "band": []}
    for value in probe_values:
        strict = evaluate_gate(value, strict_gate)
        band = evaluate_gate(value, band_gate)
        decisions["strict"].append({"value": value, "accepted": strict.accepted, "reason": strict.reason})
        decisions["band"].append({"value": value, "accepted": band.accepted, "reason": band.reason})
    # 重复运行 50 次，验证裁决序列逐字节稳定。
    stable = True
    reference = [item["accepted"] for item in decisions["band"]]
    for _ in range(50):
        current = [evaluate_gate(value, band_gate).accepted for value in probe_values]
        stable = stable and current == reference
    decisions["repeat_stable"] = stable
    return decisions
