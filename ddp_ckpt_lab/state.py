"""Runtime state for four logical workers and canonical state digests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import digest_array_map, jsonable, sha256_json
from .config import TrainingConfig
from .data import DataCursor, initial_data_cursor
from .model import (
    PARAMETER_NAMES,
    initial_momentum,
    initial_parameters,
    initial_parameter_digest,
    parameter_shapes,
)


def parameter_digest(parameters: dict[str, np.ndarray]) -> str:
    return digest_array_map({f"parameter/{name}": parameters[name] for name in PARAMETER_NAMES})


def momentum_digest(momentum: dict[str, np.ndarray]) -> str:
    return digest_array_map({f"momentum/{name}": momentum[name] for name in PARAMETER_NAMES})


def cursor_payload(cursor: DataCursor) -> dict[str, Any]:
    return {
        "epoch": int(cursor.epoch),
        "position": int(cursor.position),
        "order": jsonable(np.asarray(cursor.order, dtype=np.int64)),
        "rng_state": jsonable(cursor.rng_state),
    }


@dataclass
class WorkerState:
    worker_id: int
    parameters: dict[str, np.ndarray]
    momentum: dict[str, np.ndarray]
    cursor: DataCursor

    def clone(self) -> "WorkerState":
        return WorkerState(
            worker_id=self.worker_id,
            parameters={name: self.parameters[name].copy() for name in PARAMETER_NAMES},
            momentum={name: self.momentum[name].copy() for name in PARAMETER_NAMES},
            cursor=self.cursor.clone(),
        )

    @property
    def params_digest(self) -> str:
        return parameter_digest(self.parameters)

    @property
    def momentums_digest(self) -> str:
        return momentum_digest(self.momentum)

    def payload_digest(self) -> str:
        return sha256_json(
            {
                "worker_id": int(self.worker_id),
                "params_digest": self.params_digest,
                "momentums_digest": self.momentums_digest,
                "cursor_digest": self.cursor.digest(),
                "rng_state_digest": sha256_json(jsonable(self.cursor.rng_state)),
            }
        )

    def as_summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "params_digest": self.params_digest,
            "momentums_digest": self.momentums_digest,
            "cursor_epoch": self.cursor.epoch,
            "cursor_position": self.cursor.position,
            "cursor_order_length": int(self.cursor.order.size),
            "worker_state_digest": self.payload_digest(),
        }


@dataclass
class TrainingState:
    global_step: int
    workers: list[WorkerState]

    def clone(self) -> "TrainingState":
        return TrainingState(
            global_step=int(self.global_step),
            workers=[worker.clone() for worker in self.workers],
        )

    def validate(self, config: TrainingConfig) -> None:
        if self.global_step < 0:
            raise ValueError("global_step must be non-negative")
        if len(self.workers) != config.workers:
            raise ValueError(
                f"worker count {len(self.workers)} != configured {config.workers}"
            )
        expected_shapes = parameter_shapes(config)
        for expected_id, worker in enumerate(self.workers):
            if worker.worker_id != expected_id:
                raise ValueError(
                    f"worker order mismatch: expected {expected_id}, got {worker.worker_id}"
                )
            for name in PARAMETER_NAMES:
                for label, mapping in (
                    ("parameter", worker.parameters),
                    ("momentum", worker.momentum),
                ):
                    value = np.asarray(mapping[name])
                    if value.shape != expected_shapes[name] or value.dtype != np.dtype("float64"):
                        raise ValueError(
                            f"worker {expected_id} {label} {name}: shape/dtype "
                            f"{value.shape}/{value.dtype}, expected "
                            f"{expected_shapes[name]}/float64"
                        )

    def state_digest(self, config: TrainingConfig) -> str:
        payload = {
            "config_digest": config.config_digest,
            "global_step": int(self.global_step),
            "workers": [
                {
                    "worker_id": worker.worker_id,
                    "payload_digest": worker.payload_digest(),
                    "params_digest": worker.params_digest,
                    "momentums_digest": worker.momentums_digest,
                    "cursor_digest": worker.cursor.digest(),
                }
                for worker in self.workers
            ],
        }
        return sha256_json(payload)

    def payload_digest(self) -> str:
        """Digest worker content but not the training configuration."""

        return sha256_json(
            {
                "global_step": int(self.global_step),
                "workers": [
                    worker.as_summary() for worker in self.workers
                ],
            }
        )

    def as_summary(self, config: TrainingConfig) -> dict[str, Any]:
        return {
            "global_step": self.global_step,
            "state_digest": self.state_digest(config),
            "payload_digest": self.payload_digest(),
            "workers": [worker.as_summary() for worker in self.workers],
        }


def create_initial_state(config: TrainingConfig) -> TrainingState:
    parameters = initial_parameters(config)
    momentum = initial_momentum(config)
    workers = [
        WorkerState(
            worker_id=worker_id,
            parameters={name: parameters[name].copy() for name in PARAMETER_NAMES},
            momentum={name: momentum[name].copy() for name in PARAMETER_NAMES},
            cursor=initial_data_cursor(config, worker_id),
        )
        for worker_id in range(config.workers)
    ]
    state = TrainingState(global_step=0, workers=workers)
    state.validate(config)
    return state


def initial_state_digest(config: TrainingConfig) -> str:
    return create_initial_state(config).state_digest(config)


def expected_initial_param_digest(config: TrainingConfig) -> str:
    return initial_parameter_digest(config)
