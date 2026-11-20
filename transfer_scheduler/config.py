"""全部可配置项。

配置对象均为不可变 dataclass，可 pickle 后传入 spawn 进程；
数值约束在 :mod:`transfer_scheduler.threshold` 与各构造入口处统一校验，
非法配置抛出 :class:`EConfigInvalid`，错误上下文带字段名与取值。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateConfig:
    """门限判定配置（迁移强度门限与评估准入分门限共用）。"""

    threshold: float
    strategy: str = "strict"  # strict | band
    tolerance: float = 0.0
    band_on_tie: str = "accept"  # accept | reject，容差带内或严格相等时的裁决
    field_name: str = "threshold"


@dataclass(frozen=True)
class PipelineConfig:
    """适配流水超参数。"""

    float_dtype: str = "float64"
    train_fraction: float = 0.8
    default_steps: int = 20
    default_learning_rate: float = 0.05
    l2_regularization: float = 1e-4
    subnormal_floor: float = 1e-300


@dataclass(frozen=True)
class BudgetConfig:
    """三重资源约束与裁决顺序声明。"""

    parallel_slots: int = 2
    compute_budget: int = 100_000
    memory_budget_bytes: int = 8_000_000
    max_queued_tasks: int | None = None
    arbitration_order: tuple[str, ...] = ("priority", "shared_stat_benefit", "sample_count")
    degrade_on_compute: bool = True
    min_sample_fraction: float = 0.5


@dataclass(frozen=True)
class ExecutionConfig:
    """进程池执行配置。"""

    worker_count: int = 2
    chunksize: int = 1
    serial_precision: float = 1e-12


@dataclass(frozen=True)
class AppConfig:
    """应用总配置。"""

    pipeline: PipelineConfig = PipelineConfig()
    budget: BudgetConfig = BudgetConfig()
    execution: ExecutionConfig = ExecutionConfig()
    transfer_gate: GateConfig = GateConfig(threshold=0.0, field_name="transfer_strength")
    score_gate: GateConfig = GateConfig(threshold=0.5, field_name="evaluation_score")


ADAPTER_KIND_HYPERPARAMS = {
    "linear": {"steps": 20, "learning_rate": 0.05},
    "lora_light": {"steps": 30, "learning_rate": 0.03},
    "adapter_pro": {"steps": 40, "learning_rate": 0.02},
}
