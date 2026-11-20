from __future__ import annotations

import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Iterable, Sequence

import numpy as np

from .canonical import checksum
from .errors import E_WORKER, PipelineError
from .models import ExpandedSample, TrainingChunk, TrainingConfig
from .scheduling import make_training_chunks, validate_training_config


def stable_sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def logistic_loss(logit: float, label: int) -> float:
    return float(np.logaddexp(0.0, logit) - label * logit)


def compute_accumulators(
    samples: Sequence[ExpandedSample],
    parameters: np.ndarray,
) -> tuple[float, np.ndarray]:
    gradient = np.zeros_like(parameters)
    loss_sum = 0.0
    for sample in samples:
        logit = float(parameters[-1])
        for index, value in zip(sample.indices, sample.values):
            logit += float(parameters[index]) * value
        probability = stable_sigmoid(logit)
        error = probability - sample.label
        loss_sum += logistic_loss(logit, sample.label)
        gradient[-1] += error
        for index, value in zip(sample.indices, sample.values):
            gradient[index] += error * value
    return loss_sum, gradient


def _chunk_worker(task: dict[str, Any]) -> dict[str, Any]:
    chunk: TrainingChunk = task["chunk"]
    parameters: np.ndarray = task["parameters"]
    fault_chunk_ids: frozenset[int] = task["fault_chunk_ids"]
    base = {
        "batch_index": chunk.batch_index,
        "chunk_id": chunk.chunk_id,
        "chunk_index": chunk.chunk_index,
        "sample_count": chunk.sample_count,
    }
    try:
        if chunk.chunk_id in fault_chunk_ids:
            raise RuntimeError(f"injected failure at chunk {chunk.chunk_id}")
        loss_sum, gradient = compute_accumulators(chunk.samples, parameters)
        return {
            **base,
            "gradient": gradient,
            "loss_sum": loss_sum,
            "ok": True,
            "sample_ids": [sample.sample_id for sample in chunk.samples],
        }
    except BaseException as exc:
        return {
            **base,
            "error": {
                "code": E_WORKER,
                "details": {
                    **base,
                    "first_sample_id": chunk.samples[0].sample_id,
                    "last_sample_id": chunk.samples[-1].sample_id,
                    "sample_ids": [sample.sample_id for sample in chunk.samples],
                },
                "message": str(exc) or exc.__class__.__name__,
                "stage": "chunk_gradient",
            },
            "ok": False,
        }


def _dispatch_round(
    chunks: Sequence[TrainingChunk],
    parameters: np.ndarray,
    *,
    round_number: int,
    workers: int,
    fault_chunk_ids: frozenset[int],
    executor: ProcessPoolExecutor | None,
) -> list[dict[str, Any]]:
    tasks = [
        {
            "chunk": chunk,
            "fault_chunk_ids": fault_chunk_ids,
            "parameters": parameters.copy(),
            "round_number": round_number,
        }
        for chunk in chunks
    ]
    outcomes: list[dict[str, Any]] = []
    if executor is None:
        for task in tasks:
            outcomes.append(_chunk_worker(task))
        return outcomes

    futures = [executor.submit(_chunk_worker, task) for task in tasks]
    for task, future in zip(tasks, futures):
        chunk: TrainingChunk = task["chunk"]
        try:
            outcomes.append(future.result())
        except BaseException as exc:
            outcomes.append(
                {
                    "batch_index": chunk.batch_index,
                    "chunk_id": chunk.chunk_id,
                    "chunk_index": chunk.chunk_index,
                    "error": {
                        "code": E_WORKER,
                        "details": {
                            "batch_index": chunk.batch_index,
                            "chunk_id": chunk.chunk_id,
                            "chunk_index": chunk.chunk_index,
                            "sample_ids": [
                                sample.sample_id for sample in chunk.samples
                            ],
                        },
                        "message": str(exc) or exc.__class__.__name__,
                        "stage": "chunk_gradient",
                    },
                    "ok": False,
                    "sample_count": chunk.sample_count,
                }
            )
    outcomes.sort(key=lambda item: (item["batch_index"], item["chunk_index"]))
    return outcomes


def _serialize_gradient(gradient: Any) -> list[float]:
    array = np.asarray(gradient, dtype=np.float64)
    return [float(value) for value in array.tolist()]


def _serialize_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    serialized = {
        key: value
        for key, value in outcome.items()
        if key not in {"gradient"}
    }
    if "gradient" in outcome:
        serialized["gradient"] = _serialize_gradient(outcome["gradient"])
    return serialized


def _update_parameters(
    parameters: np.ndarray,
    total_gradient: np.ndarray,
    sample_count: int,
    *,
    learning_rate: float,
    l2: float,
) -> np.ndarray:
    mean_gradient = total_gradient / sample_count
    if l2:
        mean_gradient = mean_gradient + l2 * parameters
    return parameters - learning_rate * mean_gradient


def _parameters_to_list(parameters: np.ndarray) -> list[float]:
    return [float(value) for value in parameters.tolist()]


def train_chunked(
    samples: Sequence[ExpandedSample],
    *,
    feature_dimension: int,
    config: TrainingConfig,
    chunk_size: int,
    workers: int,
) -> dict[str, Any]:
    validate_training_config(
        config,
        feature_dimension=feature_dimension,
        sample_count=len(samples),
    )
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    chunks = make_training_chunks(
        ordered,
        chunk_size=chunk_size,
        batch_sample_size=config.batch_sample_size,
    )
    initial_parameters = np.zeros(feature_dimension + 1, dtype=np.float64)
    parameters = initial_parameters.copy()
    rounds: list[dict[str, Any]] = []
    partial_chunks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    converged_round: int | None = None
    previous_loss: float | None = None
    fault_chunk_ids = frozenset(config.fault_chunk_ids)

    executor: ProcessPoolExecutor | None = None
    try:
        if workers > 1:
            executor = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("fork"),
            )
        else:
            executor = None

        for round_number in range(1, config.max_rounds + 1):
            outcomes = _dispatch_round(
                chunks,
                parameters,
                round_number=round_number,
                workers=workers,
                fault_chunk_ids=fault_chunk_ids,
                executor=executor,
            )
            failures = [outcome for outcome in outcomes if not outcome["ok"]]
            if failures:
                partial_chunks = [_serialize_outcome(outcome) for outcome in outcomes]
                errors = [dict(outcome["error"]) for outcome in failures]
                break

            total_loss = 0.0
            total_gradient = np.zeros_like(parameters)
            for outcome in outcomes:
                total_loss += float(outcome["loss_sum"])
                total_gradient += np.asarray(outcome["gradient"], dtype=np.float64)
            sample_count = sum(int(outcome["sample_count"]) for outcome in outcomes)
            loss = total_loss / sample_count
            parameters = _update_parameters(
                parameters,
                total_gradient,
                sample_count,
                learning_rate=float(config.learning_rate),
                l2=float(config.l2),
            )
            rounds.append(
                {
                    "chunk_results": [
                        _serialize_outcome(outcome) for outcome in outcomes
                    ],
                    "gradient": _serialize_gradient(total_gradient / sample_count),
                    "loss": float(loss),
                    "loss_sum": float(total_loss),
                    "parameters": _parameters_to_list(parameters),
                    "round": round_number,
                    "sample_count": sample_count,
                }
            )
            if (
                previous_loss is not None
                and abs(loss - previous_loss) <= float(config.tolerance)
            ):
                converged_round = round_number
                break
            previous_loss = loss
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    complete = not errors
    scores: list[dict[str, Any]] = []
    metrics: dict[str, Any] | None = None
    if complete:
        scores = score_samples(ordered, parameters)
        metrics = compute_metrics(scores)

    return {
        "chunk_size": chunk_size,
        "chunks": [chunk.to_dict() for chunk in chunks],
        "complete": complete,
        "converged_round": converged_round,
        "errors": errors,
        "feature_dimension": feature_dimension,
        "initial_parameters": _parameters_to_list(initial_parameters),
        "metrics": metrics,
        "partial_chunks": partial_chunks,
        "parameters": _parameters_to_list(parameters),
        "rounds": rounds,
        "sample_ids": [sample.sample_id for sample in ordered],
        "scores": scores,
        "worker_count": workers,
    }


def train_full_batch(
    samples: Sequence[ExpandedSample],
    *,
    feature_dimension: int,
    config: TrainingConfig,
) -> dict[str, Any]:
    validate_training_config(
        config,
        feature_dimension=feature_dimension,
        sample_count=len(samples),
    )
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    initial_parameters = np.zeros(feature_dimension + 1, dtype=np.float64)
    parameters = initial_parameters.copy()
    rounds: list[dict[str, Any]] = []
    converged_round: int | None = None
    previous_loss: float | None = None
    for round_number in range(1, config.max_rounds + 1):
        loss_sum, total_gradient = compute_accumulators(ordered, parameters)
        sample_count = len(ordered)
        loss = loss_sum / sample_count
        parameters = _update_parameters(
            parameters,
            total_gradient,
            sample_count,
            learning_rate=float(config.learning_rate),
            l2=float(config.l2),
        )
        rounds.append(
            {
                "gradient": _serialize_gradient(total_gradient / sample_count),
                "loss": float(loss),
                "loss_sum": float(loss_sum),
                "parameters": _parameters_to_list(parameters),
                "round": round_number,
                "sample_count": sample_count,
            }
        )
        if (
            previous_loss is not None
            and abs(loss - previous_loss) <= float(config.tolerance)
        ):
            converged_round = round_number
            break
        previous_loss = loss

    scores = score_samples(ordered, parameters)
    return {
        "complete": True,
        "converged_round": converged_round,
        "feature_dimension": feature_dimension,
        "initial_parameters": _parameters_to_list(initial_parameters),
        "metrics": compute_metrics(scores),
        "parameters": _parameters_to_list(parameters),
        "rounds": rounds,
        "sample_ids": [sample.sample_id for sample in ordered],
        "scores": scores,
    }


def score_samples(
    samples: Sequence[ExpandedSample],
    parameters: np.ndarray,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda item: item.sample_id):
        logit = float(parameters[-1])
        for index, value in zip(sample.indices, sample.values):
            logit += float(parameters[index]) * value
        details.append(
            {
                "label": sample.label,
                "logit": float(logit),
                "sample_id": sample.sample_id,
                "score": stable_sigmoid(logit),
            }
        )
    return details


def auc_from_details(details: Sequence[dict[str, Any]]) -> float:
    positives = sum(int(detail["label"]) for detail in details)
    negatives = len(details) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    ordered = sorted(
        details,
        key=lambda detail: (float(detail["score"]), str(detail["sample_id"])),
    )
    positive_rank_sum = 0.0
    position = 1
    while position <= len(ordered):
        start = position - 1
        end = start + 1
        score = float(ordered[start]["score"])
        while end < len(ordered) and float(ordered[end]["score"]) == score:
            end += 1
        average_rank = ((position) + (position + (end - start) - 1)) / 2.0
        positive_rank_sum += average_rank * sum(
            int(detail["label"]) for detail in ordered[start:end]
        )
        position += end - start
    return float(
        (positive_rank_sum - positives * (positives + 1) / 2.0)
        / (positives * negatives)
    )


def log_loss_from_details(details: Sequence[dict[str, Any]]) -> float:
    if not details:
        return 0.0
    total = 0.0
    for detail in details:
        score = min(max(float(detail["score"]), 1e-15), 1.0 - 1e-15)
        label = int(detail["label"])
        total -= label * math.log(score) + (1 - label) * math.log(1.0 - score)
    return total / len(details)


def compute_metrics(details: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "auc": auc_from_details(details),
        "log_loss": log_loss_from_details(details),
        "sample_count": len(details),
    }


def compare_chunked_and_full(
    chunked: dict[str, Any],
    full: dict[str, Any],
) -> dict[str, Any]:
    chunk_rounds = {row["round"]: row for row in chunked["rounds"]}
    full_rounds = {row["round"]: row for row in full["rounds"]}
    common_rounds = sorted(set(chunk_rounds) & set(full_rounds))
    round_deltas: list[dict[str, Any]] = []
    for round_number in common_rounds:
        chunk_gradient = np.asarray(
            chunk_rounds[round_number]["gradient"], dtype=np.float64
        )
        full_gradient = np.asarray(
            full_rounds[round_number]["gradient"], dtype=np.float64
        )
        round_deltas.append(
            {
                "max_abs_gradient_delta": float(
                    np.max(np.abs(chunk_gradient - full_gradient))
                ),
                "max_abs_loss_delta": abs(
                    float(chunk_rounds[round_number]["loss"])
                    - float(full_rounds[round_number]["loss"])
                ),
                "round": round_number,
            }
        )

    chunk_parameters = np.asarray(chunked["parameters"], dtype=np.float64)
    full_parameters = np.asarray(full["parameters"], dtype=np.float64)
    deviation = np.abs(chunk_parameters - full_parameters)
    return {
        "chunked_converged_round": chunked["converged_round"],
        "converged_round_equal": (
            chunked["converged_round"] == full["converged_round"]
        ),
        "final_parameter_abs_deviation_spectrum": [
            float(value) for value in deviation.tolist()
        ],
        "full_converged_round": full["converged_round"],
        "max_abs_parameter_deviation": float(np.max(deviation)),
        "mean_abs_parameter_deviation": float(np.mean(deviation)),
        "round_deltas": round_deltas,
    }


def training_fingerprint(
    chunked: dict[str, Any],
    full: dict[str, Any],
    comparison: dict[str, Any],
) -> str:
    stable_payload = {
        "chunked": {
            "chunk_size": chunked["chunk_size"],
            "chunks": chunked["chunks"],
            "complete": chunked["complete"],
            "converged_round": chunked["converged_round"],
            "initial_parameters": chunked["initial_parameters"],
            "metrics": chunked["metrics"],
            "parameters": chunked["parameters"],
            "rounds": chunked["rounds"],
            "sample_ids": chunked["sample_ids"],
            "scores": chunked["scores"],
        },
        "comparison": comparison,
        "full": {
            "converged_round": full["converged_round"],
            "metrics": full["metrics"],
            "parameters": full["parameters"],
            "rounds": full["rounds"],
            "sample_ids": full["sample_ids"],
            "scores": full["scores"],
        },
    }
    return checksum(stable_payload)
