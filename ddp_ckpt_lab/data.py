"""Deterministic synthetic data, worker sharding, and resumable data cursors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .canonical import (
    digest_array_map,
    generator_from_state,
    jsonable,
    rng_state_digest,
    rng_state_to_json,
    sha256_json,
)
from .config import TrainingConfig


NUM_CLASSES = 2
DATA_STREAM_SEED = 0xDA7A5EED


@dataclass(frozen=True)
class Dataset:
    features: np.ndarray
    labels: np.ndarray
    digest: str


def generate_synthetic_dataset(config: TrainingConfig) -> Dataset:
    """Generate a fixed two-class dataset from integer seeds and dimensions."""

    rng = np.random.default_rng(
        [config.data_seed, config.seed, DATA_STREAM_SEED, config.samples, config.features]
    )
    features = np.asarray(
        rng.standard_normal((config.samples, config.features)), dtype=np.float64
    )
    teacher = np.asarray(
        rng.standard_normal(config.features, dtype=np.float64), dtype=np.float64
    )
    bias = float(rng.standard_normal())
    signal = features @ teacher + bias
    threshold = float(np.median(signal))
    labels = np.asarray(signal > threshold, dtype=np.int64)
    arrays = {
        "features": features.astype(np.dtype("<f8"), copy=False),
        "labels": labels.astype(np.dtype("<i8"), copy=False),
    }
    return Dataset(
        features=arrays["features"],
        labels=arrays["labels"],
        digest=digest_array_map(arrays),
    )


@dataclass
class DataCursor:
    epoch: int
    position: int
    order: np.ndarray
    rng_state: dict[str, Any]

    def clone(self) -> "DataCursor":
        return DataCursor(
            epoch=int(self.epoch),
            position=int(self.position),
            order=np.asarray(self.order, dtype=np.int64).copy(),
            rng_state=jsonable(self.rng_state),
        )

    def digest(self) -> str:
        return sha256_json(
            {
                "epoch": int(self.epoch),
                "position": int(self.position),
                "order_digest": digest_array_map(
                    {"order": np.asarray(self.order, dtype=np.int64)}
                ),
                "rng_state_digest": rng_state_digest(self.rng_state),
            }
        )


def worker_shard_indices(config: TrainingConfig, worker_id: int) -> np.ndarray:
    if not 0 <= worker_id < config.workers:
        raise ValueError(f"worker_id out of range: {worker_id}")
    sample_ids = np.arange(config.samples, dtype=np.int64)
    if config.sharding == "modulo":
        return sample_ids[worker_id::config.workers]
    if config.sharding == "contiguous":
        boundaries = np.linspace(
            0, config.samples, config.workers + 1, dtype=np.int64
        )
        return sample_ids[boundaries[worker_id] : boundaries[worker_id + 1]]
    raise ValueError(f"unsupported sharding mode: {config.sharding}")


def initial_data_cursor(config: TrainingConfig, worker_id: int) -> DataCursor:
    worker_rng = np.random.default_rng(
        [
            config.data_seed,
            config.seed,
            DATA_STREAM_SEED,
            config.sharding == "contiguous",
            worker_id,
        ]
    )
    return DataCursor(
        epoch=0,
        position=0,
        order=np.asarray([], dtype=np.int64),
        rng_state=rng_state_to_json(worker_rng),
    )


class WorkerDataStream:
    """A resumable per-worker epoch permutation stream.

    The current permutation plus its position are stored in DataCursor, so a
    restore can resume in the middle of an epoch without regenerating history.
    """

    def __init__(
        self,
        config: TrainingConfig,
        worker_id: int,
        cursor: DataCursor | None = None,
    ) -> None:
        self.config = config
        self.worker_id = worker_id
        self.local_indices = worker_shard_indices(config, worker_id)
        self.cursor = (cursor.clone() if cursor is not None else initial_data_cursor(config, worker_id))
        self.rng = generator_from_state(self.cursor.rng_state)
        self.cursor.order = np.asarray(self.cursor.order, dtype=np.int64).copy()
        if self.local_indices.size % config.local_batch_size != 0:
            raise ValueError("worker shard size is not divisible by local batch size")
        if self.cursor.order.size and not np.isin(
            self.cursor.order, self.local_indices
        ).all():
            raise ValueError("cursor order contains indices outside the worker shard")
        if self.cursor.position > self.cursor.order.size:
            raise ValueError("cursor position exceeds current permutation length")

    @property
    def local_batch_size(self) -> int:
        return self.config.local_batch_size

    def next_batch(self) -> np.ndarray:
        if self.cursor.order.size == 0 or self.cursor.position >= self.cursor.order.size:
            permuted = self.rng.permutation(self.local_indices)
            self.cursor.order = np.asarray(permuted, dtype=np.int64).copy()
            self.cursor.epoch += 1
            self.cursor.position = 0
        start = self.cursor.position
        stop = start + self.local_batch_size
        batch = self.cursor.order[start:stop].copy()
        if batch.size != self.local_batch_size:
            raise ValueError("incomplete local batch; check samples/batch configuration")
        self.cursor.position = stop
        self.cursor.rng_state = rng_state_to_json(self.rng)
        return batch


def initial_batch_plan_digest(config: TrainingConfig) -> str:
    """Digest the first train_steps of each worker's batch stream."""

    batches: dict[str, np.ndarray] = {}
    for worker_id in range(config.workers):
        stream = WorkerDataStream(config, worker_id)
        for step in range(1, config.train_steps + 1):
            batches[f"worker_{worker_id:02d}/step_{step:06d}"] = stream.next_batch()
    return digest_array_map(batches)
