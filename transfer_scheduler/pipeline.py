"""确定性任务执行核心（四阶段适配流水）。

阶段固定为：

1. ``standardize`` —— 用源域经验统计量标准化特征；
2. ``transfer_map`` —— 按 ``transfer_strength`` 在源域/目标域统计量之间
   线性插值，构造确定性仿射迁移映射；
3. ``adapter_train`` —— 适配层全批量确定性梯度步进（固定顺序、固定步数）；
4. ``target_evaluate`` —— 在独立生成的目标域评估集上计算 MSE 与准入分数。

同一 ``task_id + seed + 配置`` 下，所有中间量与最终分数逐字节一致：
随机流全部由派生种子驱动，不使用系统时间，也不依赖字典遍历顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import ADAPTER_KIND_HYPERPARAMS, PipelineConfig
from .data import TaskSpec, domain_statistics, generate_task_arrays
from .determinism import derive_seed, fingerprint_arrays, numpy_rng
from .errors import EConfigInvalid, EWorkerFailure
from .threshold import GateDecision, evaluate_gate, sanitize_array, validate_transfer_strength

STAGE_STANDARDIZE = "standardize"
STAGE_TRANSFER_MAP = "transfer_map"
STAGE_ADAPTER_TRAIN = "adapter_train"
STAGE_TARGET_EVALUATE = "target_evaluate"
PIPELINE_STAGES = (
    STAGE_STANDARDIZE,
    STAGE_TRANSFER_MAP,
    STAGE_ADAPTER_TRAIN,
    STAGE_TARGET_EVALUATE,
)


@dataclass(frozen=True)
class StageRecord:
    """单个阶段的审计记录：名称、关键指标与中间量字节指纹。"""

    stage: str
    fingerprint: str
    metrics: dict[str, float]


@dataclass(frozen=True)
class TaskResult:
    """任务成功执行的结构化结果（可 pickle 跨进程回传）。"""

    task_id: str
    status: str
    score: float
    test_mse: float
    gate: GateDecision
    stages: tuple[StageRecord, ...]
    fingerprints: dict[str, str]
    effective_sample_count: int


@dataclass(frozen=True)
class FailedTask:
    """任务失败的结构化回传，携带业务码、阶段名与 task_id。"""

    task_id: str
    status: str
    code: str
    stage: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _dtype(config: PipelineConfig) -> np.dtype:
    try:
        return np.dtype(config.float_dtype)
    except TypeError as exc:
        raise EConfigInvalid(
            "float_dtype 非法",
            field="pipeline.float_dtype",
            value=repr(config.float_dtype),
        ) from exc


def validate_task_spec(task: TaskSpec, transfer_gate) -> None:
    if task.adapter_kind not in ADAPTER_KIND_HYPERPARAMS:
        raise EConfigInvalid(
            "adapter_kind 非法",
            field="adapter_kind",
            value=repr(task.adapter_kind),
        )
    if task.input_dim <= 0 or task.sample_count <= 0:
        raise EConfigInvalid(
            "input_dim 与 sample_count 必须为正整数",
            field="input_dim/sample_count",
            value=f"{task.input_dim}/{task.sample_count}",
        )
    validate_transfer_strength(task.transfer_strength, transfer_gate)


def standardize_features(features: np.ndarray, floor: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """源域特征标准化：经验均值/标准差（固定 ddof=0），次正规数保护。"""
    mean = features.mean(axis=0)
    std = features.std(axis=0)
    safe_std = sanitize_array(std, floor)
    safe_std = np.where(safe_std == 0.0, 1.0, safe_std)
    standardized = (features - mean) / safe_std
    return standardized, mean, safe_std


def build_transfer_map(
    standardized: np.ndarray,
    task: TaskSpec,
    strength: float,
    floor: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按迁移强度在源域与目标域统计量之间插值构造仿射映射。

    ``mapped = z * ((1-s)*src_std + s*tgt_std) + ((1-s)*src_mean + s*tgt_mean)``
    s=0 完全保留源域分布，s=1 完全对齐目标域，中间为线性插值。
    """
    src = domain_statistics(task.source_domain, task.input_dim)
    tgt = domain_statistics(task.target_domain, task.input_dim)
    interp_std = (1.0 - strength) * src["std"] + strength * tgt["std"]
    interp_mean = (1.0 - strength) * src["mean"] + strength * tgt["mean"]
    interp_std = sanitize_array(interp_std, floor)
    mapped = standardized * interp_std + interp_mean
    return mapped, interp_mean, interp_std


def split_train_eval(
    mapped: np.ndarray,
    weights: np.ndarray,
    task: TaskSpec,
    config: PipelineConfig,
    effective_samples: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """确定性切分训练/评估样本：固定排列，分块边界仅由种子决定。"""
    rng = numpy_rng("split", task.seed, task.task_id)
    permutation = rng.permutation(effective_samples)
    cut = int(np.floor(config.train_fraction * effective_samples))
    train_idx = np.sort(permutation[:cut])
    eval_idx = np.sort(permutation[cut:])
    x_train, x_eval = mapped[train_idx], mapped[eval_idx]
    y_train = x_train @ weights
    y_eval = x_eval @ weights
    return x_train, y_train, x_eval, y_eval


def train_adapter(
    x_train: np.ndarray,
    y_train: np.ndarray,
    task: TaskSpec,
    config: PipelineConfig,
) -> tuple[np.ndarray, float, float, np.ndarray]:
    """适配层确定性增量训练。

    线性适配层 ``y = X w + b``，全批量固定顺序梯度步进；
    参数由任务种子零扰动初始化（这里用固定零初始化，保证逐字节可复现），
    梯度缓冲显式分配并在每步原地复用。
    """
    hyper = ADAPTER_KIND_HYPERPARAMS[task.adapter_kind]
    steps = hyper["steps"]
    learning_rate = hyper["learning_rate"]
    dim = x_train.shape[1]
    weight = np.zeros(dim, dtype=x_train.dtype)
    bias = np.float64(0.0)
    grad_w = np.empty_like(weight)
    grad_b = np.float64(0.0)
    row_count = np.float64(x_train.shape[0])
    for _ in range(steps):
        residual = x_train @ weight + bias - y_train
        np.dot(residual, x_train, out=grad_w)
        np.multiply(grad_w, 1.0 / row_count, out=grad_w)
        # grad_w = grad_w / N + lambda * w，仍然落在同一梯度缓冲内。
        np.add(grad_w, config.l2_regularization * weight, out=grad_w)
        grad_b = residual.mean()
        np.subtract(weight, learning_rate * grad_w, out=weight)
        bias = bias - learning_rate * grad_b
    train_mse = float(np.mean((x_train @ weight + bias - y_train) ** 2))
    return weight, float(bias), train_mse, grad_w


def evaluate_on_target(
    weight: np.ndarray,
    bias: float,
    task: TaskSpec,
    config: PipelineConfig,
    effective_samples: int,
) -> tuple[float, np.ndarray]:
    """目标域评估：独立派生的目标域评估集，统计量与共享域统计同源。"""
    rng = numpy_rng("target-eval", task.seed, task.task_id)
    tgt = domain_statistics(task.target_domain, task.input_dim)
    eval_count = max(16, effective_samples // 4)
    x_target = rng.normal(size=(eval_count, task.input_dim)) * tgt["std"] + tgt["mean"]
    x_target = x_target.astype(weight.dtype)
    truth = x_target @ generate_task_arrays(task, weight.dtype)["weights"]
    prediction = x_target @ weight + bias
    mse = float(np.mean((prediction - truth) ** 2))
    return mse, x_target


def run_task(
    task: TaskSpec,
    config: PipelineConfig,
    transfer_gate,
    score_gate,
    effective_sample_count: int | None = None,
) -> TaskResult:
    """执行单个任务的完整流水，任何阶段异常都包装为带阶段名的业务异常。"""
    dtype = _dtype(config)
    try:
        validate_task_spec(task, transfer_gate)
        strength = validate_transfer_strength(task.transfer_strength, transfer_gate)
        transfer_decision = evaluate_gate(strength, transfer_gate, config.subnormal_floor)
        if not transfer_decision.accepted:
            raise EConfigInvalid(
                "迁移强度未通过迁移门限",
                field="transfer_strength",
                value=repr(strength),
                gate_reason=transfer_decision.reason,
            )
        effective = effective_sample_count or task.sample_count
        if effective <= 0 or effective > task.sample_count:
            raise EConfigInvalid(
                "effective_sample_count 非法",
                field="effective_sample_count",
                value=repr(effective),
            )

        arrays = generate_task_arrays(task, dtype)
        features = arrays["features"][:effective]
        weights = arrays["weights"]

        standardized, src_mean, src_std = standardize_features(features, config.subnormal_floor)
        mapped, interp_mean, interp_std = build_transfer_map(
            standardized, task, strength, config.subnormal_floor
        )
        x_train, y_train, x_eval, y_eval = split_train_eval(
            mapped, weights, task, config, effective
        )
        adapter_weight, adapter_bias, train_mse, grad_buffer = train_adapter(
            x_train, y_train, task, config
        )
        test_mse, x_target = evaluate_on_target(
            adapter_weight, adapter_bias, task, config, effective
        )
        score = 1.0 / (1.0 + test_mse)
        score_decision = evaluate_gate(score, score_gate, config.subnormal_floor)

        stages = (
            StageRecord(
                STAGE_STANDARDIZE,
                fingerprint_arrays([standardized, src_mean, src_std]),
                {"mean_abs": float(np.mean(np.abs(standardized)))},
            ),
            StageRecord(
                STAGE_TRANSFER_MAP,
                fingerprint_arrays([mapped, interp_mean, interp_std]),
                {"interp_mean_norm": float(np.linalg.norm(interp_mean))},
            ),
            StageRecord(
                STAGE_ADAPTER_TRAIN,
                fingerprint_arrays([adapter_weight, grad_buffer]),
                {"train_mse": train_mse},
            ),
            StageRecord(
                STAGE_TARGET_EVALUATE,
                fingerprint_arrays([x_target, np.asarray([test_mse])]),
                {"test_mse": test_mse, "score": score},
            ),
        )
        return TaskResult(
            task_id=task.task_id,
            status="ok",
            score=score,
            test_mse=test_mse,
            gate=score_decision,
            stages=stages,
            fingerprints={
                "features": fingerprint_arrays([features, weights]),
                "mapped": fingerprint_arrays([mapped]),
                "score": fingerprint_arrays([np.asarray([score, test_mse])]),
            },
            effective_sample_count=effective,
        )
    except (EWorkerFailure, EConfigInvalid):
        raise
    except Exception as exc:  # noqa: BLE001 - worker 边界必须收口所有异常
        stage = _infer_stage(exc)
        raise EWorkerFailure(
            f"任务 {task.task_id} 在 {stage} 阶段失败: {exc}",
            task_id=task.task_id,
            stage=stage,
        ) from exc


def _infer_stage(exc: Exception) -> str:
    text = str(exc)
    for stage in PIPELINE_STAGES:
        if stage in text:
            return stage
    return "unknown"


def run_serial(
    tasks: list[TaskSpec],
    config: PipelineConfig,
    transfer_gate,
    score_gate,
    effective_samples: dict[str, int] | None = None,
) -> dict[str, TaskResult]:
    """按 task_id 升序逐任务串行执行的确定性基线。"""
    effective_samples = effective_samples or {}
    results: dict[str, TaskResult] = {}
    for task in sorted(tasks, key=lambda item: item.task_id):
        results[task.task_id] = run_task(
            task,
            config,
            transfer_gate,
            score_gate,
            effective_samples.get(task.task_id),
        )
    return results


def deterministic_seed_anchor(task: TaskSpec) -> int:
    """对外暴露的任务级种子锚点，便于外部集成核对。"""
    return derive_seed("task-anchor", task.seed, task.task_id)
