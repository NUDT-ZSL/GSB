"""Deterministic four-worker data-parallel training loop and init checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .canonical import digest_array_map, sha256_json
from .config import TrainingConfig
from .data import (
    Dataset,
    WorkerDataStream,
    generate_synthetic_dataset,
    initial_batch_plan_digest,
)
from .errors import BusinessError
from .model import (
    PARAMETER_NAMES,
    apply_sgd_with_momentum,
    cross_entropy_loss,
    gradients_for_batch,
    initial_parameter_digest,
)
from .state import (
    TrainingState,
    create_initial_state,
    initial_state_digest,
)


@dataclass(frozen=True)
class InitBundle:
    config: TrainingConfig
    dataset: Dataset
    state: TrainingState
    data_digest: str
    parameter_digest: str
    batch_plan_digest: str
    state_digest: str

    def as_report(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "config_digest": self.config.config_digest,
            "data_digest": self.data_digest,
            "parameter_digest": self.parameter_digest,
            "batch_plan_digest": self.batch_plan_digest,
            "initial_state_digest": self.state_digest,
            "global_step": 0,
            "train_steps": self.config.train_steps,
            "workers": self.config.workers,
            "dataset_shape": list(self.dataset.features.shape),
            "config": self.config.as_dict(),
        }


@dataclass(frozen=True)
class TrainingRecord:
    step: int
    loss: float
    progress_digest: str
    state_digest: str
    param_digests: tuple[str, ...]
    momentum_digests: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "loss": self.loss,
            "progress_digest": self.progress_digest,
            "state_digest": self.state_digest,
            "param_digests": list(self.param_digests),
            "momentum_digests": list(self.momentum_digests),
        }


def initialize_training(
    config: TrainingConfig,
    expected_digests: dict[str, str] | None = None,
    *,
    verify_determinism: bool = True,
) -> InitBundle:
    dataset = generate_synthetic_dataset(config)
    parameters_digest = initial_parameter_digest(config)
    plan_digest = initial_batch_plan_digest(config)
    state = create_initial_state(config)
    state_digest = state.state_digest(config)

    if verify_determinism:
        second_dataset = generate_synthetic_dataset(config)
        second_parameter_digest = initial_parameter_digest(config)
        second_plan_digest = initial_batch_plan_digest(config)
        second_state_digest = initial_state_digest(config)
        mismatches: list[dict[str, Any]] = []
        if second_dataset.digest != dataset.digest:
            mismatches.append(
                {
                    "name": "data_digest",
                    "first": dataset.digest,
                    "second": second_dataset.digest,
                }
            )
        if second_parameter_digest != parameters_digest:
            mismatches.append(
                {
                    "name": "parameter_digest",
                    "first": parameters_digest,
                    "second": second_parameter_digest,
                }
            )
        if second_plan_digest != plan_digest:
            mismatches.append(
                {
                    "name": "batch_plan_digest",
                    "first": plan_digest,
                    "second": second_plan_digest,
                }
            )
        if second_state_digest != state_digest:
            mismatches.append(
                {
                    "name": "initial_state_digest",
                    "first": state_digest,
                    "second": second_state_digest,
                }
            )
        if mismatches:
            raise BusinessError(
                "E_INIT",
                "相同整数种子与维度生成了不一致的初始化摘要",
                mismatches=mismatches,
            )

    actual_digests = {
        "data_digest": dataset.digest,
        "parameter_digest": parameters_digest,
        "batch_plan_digest": plan_digest,
        "initial_state_digest": state_digest,
    }
    if expected_digests:
        mismatches = [
            {
                "name": name,
                "expected": expected,
                "actual": actual_digests.get(name),
            }
            for name, expected in expected_digests.items()
            if actual_digests.get(name) != expected
        ]
        if mismatches:
            raise BusinessError(
                "E_INIT",
                "初始化摘要与训练报告记录不一致",
                mismatches=mismatches,
            )

    return InitBundle(
        config=config,
        dataset=dataset,
        state=state,
        data_digest=dataset.digest,
        parameter_digest=parameters_digest,
        batch_plan_digest=plan_digest,
        state_digest=state_digest,
    )


class Trainer:
    """Owns runtime state plus a deterministic stream per logical worker."""

    def __init__(
        self,
        config: TrainingConfig,
        dataset: Dataset,
        state: TrainingState,
        *,
        clone_state: bool = True,
    ) -> None:
        if dataset.features.shape != (config.samples, config.features):
            raise ValueError("dataset feature shape does not match configuration")
        if dataset.labels.shape != (config.samples,):
            raise ValueError("dataset label shape does not match configuration")
        state.validate(config)
        self.config = config
        self.dataset = dataset
        self.state = state.clone() if clone_state else state
        self.streams = [
            WorkerDataStream(config, worker_id, self.state.workers[worker_id].cursor)
            for worker_id in range(config.workers)
        ]
        self.progress_digest = sha256_json(
            {
                "config_digest": config.config_digest,
                "step": self.state.global_step,
                "state_digest": self.state.state_digest(config),
            }
        )
        self.records: list[TrainingRecord] = []

    def step(self) -> TrainingRecord:
        if self.state.global_step != self._start_step + len(self.records):
            raise RuntimeError("training step bookkeeping is inconsistent")

        batches: list[tuple[np.ndarray, np.ndarray]] = []
        worker_losses: list[float] = []
        gradients: list[dict[str, np.ndarray]] = []
        for worker_id, worker in enumerate(self.state.workers):
            indices = self.streams[worker_id].next_batch()
            features = self.dataset.features[indices]
            labels = self.dataset.labels[indices]
            batches.append((features, labels))
            worker_losses.append(cross_entropy_loss(worker.parameters, features, labels))
            gradients.append(gradients_for_batch(worker.parameters, features, labels))

        averaged: dict[str, np.ndarray] = {}
        for name in PARAMETER_NAMES:
            accumulator = np.zeros_like(gradients[0][name])
            for worker_id in range(self.config.workers):
                accumulator = accumulator + gradients[worker_id][name]
            averaged[name] = accumulator / float(self.config.workers)

        for worker in self.state.workers:
            apply_sgd_with_momentum(
                worker.parameters,
                worker.momentum,
                averaged,
                self.config.effective_learning_rate,
                self.config.effective_momentum,
            )
            worker.cursor = self.streams[worker.worker_id].cursor.clone()

        self.state.global_step += 1
        state_digest = self.state.state_digest(self.config)
        previous_digest = self.progress_digest
        self.progress_digest = sha256_json(
            {
                "previous_progress_digest": previous_digest,
                "step": self.state.global_step,
                "state_digest": state_digest,
            }
        )
        if self.progress_digest == previous_digest:
            raise BusinessError(
                "E_DIVERGE",
                "训练进度摘要未随全局步数变化",
                step=self.state.global_step,
            )

        loss = math.fsum(worker_losses) / float(self.config.workers)
        record = TrainingRecord(
            step=self.state.global_step,
            loss=loss,
            progress_digest=self.progress_digest,
            state_digest=state_digest,
            param_digests=tuple(worker.params_digest for worker in self.state.workers),
            momentum_digests=tuple(
                worker.momentums_digest for worker in self.state.workers
            ),
        )
        self.records.append(record)
        return record

    @property
    def _start_step(self) -> int:
        return self.state.global_step - len(self.records)

    def train_steps(
        self,
        count: int,
        callback: Callable[[TrainingRecord], None] | None = None,
    ) -> list[TrainingRecord]:
        if count < 0:
            raise ValueError("count must be non-negative")
        produced: list[TrainingRecord] = []
        for _ in range(count):
            record = self.step()
            produced.append(record)
            if callback is not None:
                callback(record)
        return produced

    def train_until(
        self,
        target_step: int,
        callback: Callable[[TrainingRecord], None] | None = None,
    ) -> list[TrainingRecord]:
        if target_step < self.state.global_step:
            raise ValueError(
                f"target_step {target_step} is behind current step {self.state.global_step}"
            )
        return self.train_steps(target_step - self.state.global_step, callback=callback)

    def training_report(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "config_digest": self.config.config_digest,
            "start_step": self._start_step,
            "final_step": self.state.global_step,
            "progress_digest": self.progress_digest,
            "state_digest": self.state.state_digest(self.config),
            "payload_digest": self.state.payload_digest(),
            "records": [record.as_dict() for record in self.records],
            "workers": [worker.as_summary() for worker in self.state.workers],
        }


def run_training(
    config: TrainingConfig,
    steps: int,
    *,
    expected_initial_digests: dict[str, str] | None = None,
) -> tuple[InitBundle, Trainer, list[TrainingRecord]]:
    bundle = initialize_training(config, expected_initial_digests)
    trainer = Trainer(config, bundle.dataset, bundle.state)
    records = trainer.train_steps(steps)
    return bundle, trainer, records
