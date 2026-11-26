"""Deterministic PEFT parameter updates with gradient accumulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .canonical import content_hash, normalize_number, stable_uint
from .errors import format_error
from .snapshots import Snapshot
from .tasks import NormalizedTask


ALGORITHM_VERSION = "adam-peft-v1"
BETA1 = 0.9
BETA2 = 0.999
ADAM_EPSILON = 1e-8


def _round_array(array: np.ndarray) -> np.ndarray:
    flat = [normalize_number(value) for value in np.asarray(array, dtype=np.float64).reshape(-1).tolist()]
    return np.asarray(flat, dtype=np.float64).reshape(np.asarray(array).shape)


def _array_bytes(array: np.ndarray) -> int:
    return int(np.asarray(array).nbytes)


@dataclass
class TrainLayer:
    key: str
    layer_idx: int
    shape: tuple[int, int]
    values: np.ndarray
    momentum: np.ndarray
    variance: np.ndarray

    def summary_dict(self) -> dict[str, Any]:
        return {
            "layer_idx": self.layer_idx,
            "shape": list(self.shape),
            "values": [normalize_number(v) for v in self.values.reshape(-1).tolist()],
        }

    def momentum_dict(self) -> dict[str, Any]:
        return {
            "shape": list(self.shape),
            "momentum": [normalize_number(v) for v in self.momentum.reshape(-1).tolist()],
        }

    def variance_dict(self) -> dict[str, Any]:
        return {
            "shape": list(self.shape),
            "variance": [normalize_number(v) for v in self.variance.reshape(-1).tolist()],
        }

    @property
    def resident_bytes(self) -> int:
        return _array_bytes(self.values) + _array_bytes(self.momentum) + _array_bytes(self.variance)


@dataclass
class TrainingState:
    task_id: str
    snapshot_id: str
    snapshot_semantic_hash: str
    snapshot_revision: int
    revision: int
    step: int
    micro_in_window: int
    update_count: int
    layers: dict[str, TrainLayer]
    gradient_accumulator: dict[str, np.ndarray]
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resident_bytes(self) -> int:
        base = sum(layer.resident_bytes for layer in self.layers.values())
        gradients = sum(_array_bytes(array) for array in self.gradient_accumulator.values())
        return base + gradients

    def clone(self) -> "TrainingState":
        return TrainingState(
            task_id=self.task_id,
            snapshot_id=self.snapshot_id,
            snapshot_semantic_hash=self.snapshot_semantic_hash,
            snapshot_revision=self.snapshot_revision,
            revision=self.revision,
            step=self.step,
            micro_in_window=self.micro_in_window,
            update_count=self.update_count,
            layers={
                key: TrainLayer(
                    key=layer.key,
                    layer_idx=layer.layer_idx,
                    shape=layer.shape,
                    values=layer.values.copy(),
                    momentum=layer.momentum.copy(),
                    variance=layer.variance.copy(),
                )
                for key, layer in self.layers.items()
            },
            gradient_accumulator={key: value.copy() for key, value in self.gradient_accumulator.items()},
            records=[dict(record) for record in self.records],
        )


def _summary(parameter_dict: dict[str, Any]) -> str:
    return content_hash(parameter_dict)


def parameter_summary(layers: dict[str, TrainLayer]) -> str:
    return _summary({key: layer.summary_dict() for key, layer in sorted(layers.items())})


def momentum_summary(layers: dict[str, TrainLayer]) -> str:
    return _summary({key: layer.momentum_dict() for key, layer in sorted(layers.items())})


def variance_summary(layers: dict[str, TrainLayer]) -> str:
    return _summary({key: layer.variance_dict() for key, layer in sorted(layers.items())})


def optimizer_summary(state: TrainingState) -> str:
    return _summary(
        {
            "update_count": state.update_count,
            "momentum": momentum_summary(state.layers),
            "variance": variance_summary(state.layers),
        }
    )


def gradient_summary(gradients: dict[str, np.ndarray]) -> str:
    return _summary(
        {
            key: {
                "shape": list(value.shape),
                "gradient": [normalize_number(v) for v in value.reshape(-1).tolist()],
            }
            for key, value in sorted(gradients.items())
        }
    )


def initialize_task(task: NormalizedTask, snapshot: Snapshot) -> TrainingState:
    snapshot.validate_against_task(task)
    layers = {
        key: TrainLayer(
            key=key,
            layer_idx=layer.layer_idx,
            shape=layer.shape,
            values=layer.values.copy(),
            momentum=layer.momentum.copy(),
            variance=layer.variance.copy(),
        )
        for key, layer in snapshot.layers.items()
    }
    accumulator = {
        key: np.zeros(layer.shape, dtype=np.float64)
        for key, layer in layers.items()
    }
    return TrainingState(
        task_id=task.case_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_semantic_hash=snapshot.semantic_hash,
        snapshot_revision=snapshot.revision,
        revision=task.revision,
        step=0,
        micro_in_window=0,
        update_count=0,
        layers=layers,
        gradient_accumulator=accumulator,
        records=[],
    )


def _micro_gradient(
    task: NormalizedTask,
    state: TrainingState,
    micro_index: int,
) -> dict[str, np.ndarray]:
    gradients: dict[str, np.ndarray] = {}
    for key, layer in sorted(state.layers.items()):
        count = int(layer.shape[0]) * int(layer.shape[1])
        index = np.arange(1, count + 1, dtype=np.float64)
        phase = stable_uint(
            "micro-gradient",
            task.case_id,
            task.seed,
            key,
            micro_index,
            task.revision,
            task.adapter_dim,
            bits=64,
        )
        phase_a = (phase % 1_000_003) / 1_000_003.0
        phase_b = ((phase >> 23) % 999_983) / 999_983.0
        wave = np.sin(index * (0.009 + 0.002 * phase_a) + micro_index * 0.041 + phase_b * 2.5)
        cosine = np.cos(index * (0.004 + 0.001 * phase_b) + phase_a * 1.7)
        gradient = 0.0012 * (wave + 0.3 * cosine) + 0.0004 * np.tanh(layer.values.reshape(-1))
        gradients[key] = _round_array(gradient.reshape(layer.shape))
    return gradients


def _record_accumulation(
    task: NormalizedTask,
    state: TrainingState,
    *,
    micro_index: int,
    gradient: dict[str, np.ndarray],
) -> None:
    state.records.append(
        {
            "step": micro_index,
            "phase": "GRAD_ACCUM",
            "optimizer_step": (micro_index + task.micro_batch - 1) // task.micro_batch,
            "micro_in_window": state.micro_in_window,
            "parameter_summary": parameter_summary(state.layers),
            "momentum_summary": momentum_summary(state.layers),
            "variance_summary": variance_summary(state.layers),
            "micro_gradient_summary": gradient_summary(gradient),
            "accumulated_gradient_summary": gradient_summary(state.gradient_accumulator),
        }
    )


def accumulate_micro_step(task: NormalizedTask, state: TrainingState) -> TrainingState:
    """Advance one micro step. The optimizer is not applied until the window closes."""
    if state.step >= task.steps:
        return state.clone()
    if state.micro_in_window >= task.micro_batch:
        raise format_error(
            "gradient window is unexpectedly full",
            task_id=task.case_id,
            field="micro_batch",
            details={"micro_in_window": state.micro_in_window},
        )
    state = state.clone()
    micro_index = state.step + 1
    gradient = _micro_gradient(task, state, micro_index)
    for key in state.gradient_accumulator:
        state.gradient_accumulator[key] = _round_array(
            state.gradient_accumulator[key] + gradient[key]
        )
    state.step = micro_index
    state.micro_in_window += 1
    _record_accumulation(task, state, micro_index=micro_index, gradient=gradient)
    return state


def apply_optimizer_step(task: NormalizedTask, state: TrainingState) -> TrainingState:
    """Apply one Adam update using the completed gradient-accumulation window."""
    if state.micro_in_window != task.micro_batch:
        raise format_error(
            "optimizer update requires a complete gradient accumulation window",
            task_id=task.case_id,
            field="micro_batch",
            details={"micro_in_window": state.micro_in_window, "micro_batch": task.micro_batch},
        )
    state = state.clone()
    state.update_count += 1
    update_index = state.update_count
    gradients = {
        key: _round_array(value / float(task.micro_batch))
        for key, value in state.gradient_accumulator.items()
    }
    for key, layer in state.layers.items():
        gradient = gradients[key]
        momentum = _round_array(BETA1 * layer.momentum + (1.0 - BETA1) * gradient)
        variance = _round_array(BETA2 * layer.variance + (1.0 - BETA2) * (gradient * gradient))
        momentum_hat = momentum / (1.0 - BETA1**update_index)
        variance_hat = variance / (1.0 - BETA2**update_index)
        updated = _round_array(
            layer.values - task.lr * momentum_hat / (np.sqrt(variance_hat) + ADAM_EPSILON)
        )
        layer.values = updated
        layer.momentum = momentum
        layer.variance = variance
    for key in state.gradient_accumulator:
        state.gradient_accumulator[key] = _round_array(np.zeros_like(state.gradient_accumulator[key]))
    state.micro_in_window = 0
    state.records.append(
        {
            "step": state.step,
            "phase": "OPTIMIZER_UPDATE",
            "optimizer_step": update_index,
            "micro_in_window": 0,
            "parameter_summary": parameter_summary(state.layers),
            "momentum_summary": momentum_summary(state.layers),
            "variance_summary": variance_summary(state.layers),
            "gradient_summary": gradient_summary(gradients),
        }
    )
    return state


def advance_task(
    task: NormalizedTask,
    state: TrainingState,
) -> TrainingState:
    state = accumulate_micro_step(task, state)
    if state.micro_in_window == task.micro_batch:
        state = apply_optimizer_step(task, state)
    return state


def run_task_range(
    task: NormalizedTask,
    snapshot: Snapshot,
    *,
    start_step: int = 0,
    end_step: int | None = None,
    initial_state: TrainingState | None = None,
) -> TrainingState:
    """Run a task from an explicit micro-step boundary."""
    end_step = task.steps if end_step is None else int(end_step)
    if start_step < 0 or start_step > task.steps or end_step < start_step or end_step > task.steps:
        raise format_error(
            "training step range is outside [0, steps]",
            task_id=task.case_id,
            field="steps",
            details={"start_step": start_step, "end_step": end_step, "steps": task.steps},
        )
    if initial_state is None:
        if start_step != 0:
            raise format_error(
                "non-zero start_step requires an explicit initial_state",
                task_id=task.case_id,
                field="start_step",
            )
        state = initialize_task(task, snapshot)
    else:
        state = initial_state.clone()
        if state.task_id != task.case_id or state.step != start_step:
            raise format_error(
                "resume state does not match task/start_step",
                task_id=task.case_id,
                field="start_step",
                details={"state_step": state.step, "requested": start_step},
            )
        if state.snapshot_semantic_hash != snapshot.semantic_hash:
            raise format_error(
                "resume state was produced from a different snapshot",
                task_id=task.case_id,
                field="snapshot_semantic_hash",
            )
    while state.step < end_step:
        state = advance_task(task, state)
    return state


def train_task(task: NormalizedTask, snapshot: Snapshot) -> TrainingState:
    state = run_task_range(task, snapshot, start_step=0, end_step=task.steps)
    if state.step != task.steps or state.micro_in_window != 0:
        raise format_error(
            "training did not finish at a clean optimizer boundary",
            task_id=task.case_id,
            field="steps",
        )
    return state


def final_task_summaries(state: TrainingState) -> dict[str, Any]:
    return {
        "final_param_summary": parameter_summary(state.layers),
        "final_optimizer_summary": optimizer_summary(state),
        "final_momentum_summary": momentum_summary(state.layers),
        "final_variance_summary": variance_summary(state.layers),
        "steps_completed": state.step,
        "optimizer_steps_completed": state.update_count,
    }


def state_to_dict(state: TrainingState) -> dict[str, Any]:
    return {
        "task_id": state.task_id,
        "snapshot_id": state.snapshot_id,
        "snapshot_semantic_hash": state.snapshot_semantic_hash,
        "snapshot_revision": state.snapshot_revision,
        "revision": state.revision,
        "step": state.step,
        "micro_in_window": state.micro_in_window,
        "update_count": state.update_count,
        "layers": {
            key: {
                "layer_idx": layer.layer_idx,
                "shape": list(layer.shape),
                "values": [normalize_number(v) for v in layer.values.reshape(-1).tolist()],
                "momentum": [normalize_number(v) for v in layer.momentum.reshape(-1).tolist()],
                "variance": [normalize_number(v) for v in layer.variance.reshape(-1).tolist()],
            }
            for key, layer in sorted(state.layers.items())
        },
        "gradient_accumulator": {
            key: [normalize_number(v) for v in value.reshape(-1).tolist()]
            for key, value in sorted(state.gradient_accumulator.items())
        },
        "records": state.records,
    }


def state_from_dict(data: dict[str, Any]) -> TrainingState:
    layers: dict[str, TrainLayer] = {}
    for key, layer in sorted(data["layers"].items()):
        shape = tuple(int(item) for item in layer["shape"])
        layers[key] = TrainLayer(
            key=key,
            layer_idx=int(layer["layer_idx"]),
            shape=shape,
            values=np.asarray(layer["values"], dtype=np.float64).reshape(shape),
            momentum=np.asarray(layer["momentum"], dtype=np.float64).reshape(shape),
            variance=np.asarray(layer["variance"], dtype=np.float64).reshape(shape),
        )
    gradients = {
        key: np.asarray(values, dtype=np.float64).reshape(layers[key].shape)
        for key, values in sorted(data["gradient_accumulator"].items())
    }
    return TrainingState(
        task_id=str(data["task_id"]),
        snapshot_id=str(data["snapshot_id"]),
        snapshot_semantic_hash=str(data["snapshot_semantic_hash"]),
        snapshot_revision=int(data["snapshot_revision"]),
        revision=int(data["revision"]),
        step=int(data["step"]),
        micro_in_window=int(data["micro_in_window"]),
        update_count=int(data["update_count"]),
        layers=layers,
        gradient_accumulator=gradients,
        records=list(data.get("records", [])),
    )


def state_digest(state: TrainingState) -> str:
    return content_hash(state_to_dict(state))


__all__ = [
    "ALGORITHM_VERSION",
    "TrainingState",
    "accumulate_micro_step",
    "advance_task",
    "apply_optimizer_step",
    "final_task_summaries",
    "initialize_task",
    "run_task_range",
    "state_digest",
    "state_from_dict",
    "state_to_dict",
    "train_task",
]

