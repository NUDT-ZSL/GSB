from __future__ import annotations

import math
from typing import Iterable, Sequence

from .errors import E_CONFIG_INVALID, PipelineError
from .models import ExpandedSample, TrainingChunk, TrainingConfig


def _invalid(field: str, message: str, **extra: object) -> PipelineError:
    return PipelineError(
        E_CONFIG_INVALID,
        "config_validation",
        message,
        {"field": field, **extra},
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_training_config(
    config: TrainingConfig,
    *,
    feature_dimension: int | None = None,
    sample_count: int | None = None,
) -> None:
    if not _is_int(config.chunk_size) or config.chunk_size <= 0:
        raise _invalid("chunk_size", "chunk_size must be a positive integer", actual=config.chunk_size)
    if not _is_int(config.batch_sample_size) or config.batch_sample_size <= 0:
        raise _invalid(
            "batch_sample_size",
            "batch_sample_size must be a positive integer",
            actual=config.batch_sample_size,
        )
    if not _is_int(config.workers) or config.workers <= 0 or config.workers > 64:
        raise _invalid(
            "workers",
            "workers must be an integer in [1, 64]",
            actual=config.workers,
        )
    if not _is_int(config.max_rounds) or config.max_rounds <= 0:
        raise _invalid(
            "max_rounds",
            "max_rounds must be a positive integer",
            actual=config.max_rounds,
        )
    if (
        isinstance(config.learning_rate, bool)
        or not isinstance(config.learning_rate, (int, float))
        or not math.isfinite(float(config.learning_rate))
        or float(config.learning_rate) <= 0.0
    ):
        raise _invalid(
            "learning_rate",
            "learning_rate must be a finite positive number",
            actual=config.learning_rate,
        )
    if (
        isinstance(config.tolerance, bool)
        or not isinstance(config.tolerance, (int, float))
        or not math.isfinite(float(config.tolerance))
        or float(config.tolerance) < 0.0
    ):
        raise _invalid(
            "tolerance",
            "tolerance must be a finite non-negative number",
            actual=config.tolerance,
        )
    if (
        isinstance(config.l2, bool)
        or not isinstance(config.l2, (int, float))
        or not math.isfinite(float(config.l2))
        or float(config.l2) < 0.0
    ):
        raise _invalid("l2", "l2 must be a finite non-negative number", actual=config.l2)
    if feature_dimension is not None and (
        not _is_int(feature_dimension) or feature_dimension <= 0
    ):
        raise _invalid(
            "feature_dimension",
            "feature_dimension must be a positive integer",
            actual=feature_dimension,
        )
    if sample_count is not None and (
        not _is_int(sample_count) or sample_count <= 0
    ):
        raise _invalid("sample_count", "sample_count must be positive", actual=sample_count)


def make_training_chunks(
    samples: Sequence[ExpandedSample],
    *,
    chunk_size: int,
    batch_sample_size: int,
) -> tuple[TrainingChunk, ...]:
    if chunk_size <= 0 or batch_sample_size <= 0:
        raise _invalid("chunk_size", "chunk and batch sizes must be positive")
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    chunks: list[TrainingChunk] = []
    batch_index = 0
    batch_sample_count = 0
    chunk_index_in_batch = 0
    for chunk_id, start in enumerate(range(0, len(ordered), chunk_size)):
        end = min(start + chunk_size, len(ordered))
        chunk_samples = tuple(ordered[start:end])
        if chunks and batch_sample_count + len(chunk_samples) > batch_sample_size:
            batch_index += 1
            batch_sample_count = 0
            chunk_index_in_batch = 0
        chunks.append(
            TrainingChunk(
                chunk_id=chunk_id,
                batch_index=batch_index,
                chunk_index=chunk_index_in_batch,
                start=start,
                end=end,
                samples=chunk_samples,
            )
        )
        batch_sample_count += len(chunk_samples)
        chunk_index_in_batch += 1
    return tuple(chunks)


def batches_from_chunks(
    chunks: Sequence[TrainingChunk],
) -> tuple[tuple[TrainingChunk, ...], ...]:
    if not chunks:
        return ()
    grouped: list[list[TrainingChunk]] = []
    current_batch: int | None = None
    for chunk in chunks:
        if current_batch is None or chunk.batch_index != current_batch:
            grouped.append([])
            current_batch = chunk.batch_index
        grouped[-1].append(chunk)
    return tuple(tuple(group) for group in grouped)


def candidate_chunk_sizes(requested: int, limit: int = 64) -> tuple[int, ...]:
    values = {max(1, requested)}
    current = max(1, requested)
    while current > 1 and len(values) < limit:
        current = max(1, current // 2)
        values.add(current)
    for power in range(0, 8):
        value = 1 << power
        if value <= requested:
            values.add(value)
    return tuple(sorted(values, reverse=True))


def candidate_worker_counts(requested: int, limit: int = 64) -> tuple[int, ...]:
    values = set(range(1, min(requested, limit) + 1))
    return tuple(sorted(values, reverse=True))


def sample_order_checksum_ids(samples: Iterable[ExpandedSample]) -> tuple[str, ...]:
    return tuple(sample.sample_id for sample in sorted(samples, key=lambda item: item.sample_id))
