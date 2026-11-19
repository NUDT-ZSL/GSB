"""Deterministic resource estimation for train and inference jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import PipelineConfig, Task


@dataclass(frozen=True)
class ResourceEstimate:
    parameter_count: int
    weight_bytes: int
    private_bytes: int
    weight_charged_bytes: int
    memory_bytes: int
    forward_ops: int
    backward_ops: int
    compute_units: int
    slot_units: int
    weight_reused: bool

    @property
    def memory_mb(self) -> float:
        return round(self.memory_bytes / (1024 * 1024), 9)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["memory_mb"] = self.memory_mb
        return payload


def estimate_task(
    task: Task,
    config: PipelineConfig,
    *,
    infer_weight_reused: bool = False,
) -> ResourceEstimate:
    """Estimate integer compute units and resident bytes.

    The formula is intentionally explicit and platform-independent. It models
    one scalar multiply-add as two ops and is used only for admission decisions;
    actual determinism is provided by the execution core.
    """

    scalar = config.bytes_per_scalar
    activation = task.batch_size * task.seq_len * task.feature_dim * scalar
    parameter_count = task.parameter_count
    weight_bytes = parameter_count * scalar
    forward_ops = (
        2 * task.batch_size * task.seq_len * task.feature_dim * task.feature_dim
        + task.batch_size * task.seq_len * task.feature_dim
    )
    backward_ops = 2 * forward_ops + 2 * parameter_count
    slot_units = 1

    if task.job_kind == "train":
        private_bytes = (
            weight_bytes
            + config.gradient_buffer_copies * weight_bytes
            + config.activation_copies * activation
        )
        weight_charged = weight_bytes
        compute_units = task.iterations * (forward_ops + backward_ops) + forward_ops
        reused = False
    else:
        private_bytes = config.activation_copies * activation
        weight_charged = 0 if infer_weight_reused else weight_bytes
        compute_units = task.iterations * forward_ops
        reused = infer_weight_reused

    return ResourceEstimate(
        parameter_count=parameter_count,
        weight_bytes=weight_bytes,
        private_bytes=private_bytes,
        weight_charged_bytes=weight_charged,
        memory_bytes=private_bytes + weight_charged,
        forward_ops=forward_ops,
        backward_ops=backward_ops,
        compute_units=compute_units,
        slot_units=slot_units,
        weight_reused=reused,
    )
