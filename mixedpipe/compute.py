"""Deterministic NumPy forward/backward simulation and result summaries."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .canonical import canonical_json_bytes, content_checksum, derive_seed
from .errors import BusinessError, E_INJECTED_FAULT, E_WORKER_INTERNAL
from .models import PipelineConfig, Task


class InjectedFault(BusinessError):
    def __init__(self, task: Task, stage: str) -> None:
        super().__init__(
            E_INJECTED_FAULT,
            f"injected deterministic fault at {stage}",
            stage=stage,
            details={"job_id": task.job_id, "requested_stage": stage},
        )


def _maybe_fault(task: Task, stage: str) -> None:
    if task.fault == stage:
        raise InjectedFault(task, stage)


def generate_features(task: Task) -> np.ndarray:
    rng = np.random.default_rng(
        derive_seed(
            "features",
            task.seed,
            task.batch_size,
            task.seq_len,
            task.feature_dim,
        )
    )
    return rng.standard_normal(
        (task.batch_size, task.seq_len, task.feature_dim),
        dtype=np.float64,
    )


def generate_targets(task: Task) -> np.ndarray:
    rng = np.random.default_rng(
        derive_seed(
            "targets",
            task.seed,
            task.batch_size,
            task.seq_len,
            task.feature_dim,
        )
    )
    return rng.uniform(
        -1.0,
        1.0,
        (task.batch_size, task.seq_len, task.feature_dim),
    ).astype(np.float64, copy=False)


def generate_model(model_tag: str, feature_dim: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(derive_seed("model", model_tag, feature_dim))
    scale = 1.0 / np.sqrt(float(feature_dim))
    weights = rng.standard_normal((feature_dim, feature_dim), dtype=np.float64) * scale
    bias = rng.standard_normal((feature_dim,), dtype=np.float64) * 0.01
    return weights, bias


def _forward(
    features: np.ndarray,
    weights: np.ndarray,
    bias: np.ndarray,
    targets: np.ndarray,
) -> tuple[np.ndarray, float]:
    # Explicit broadcast + reduction avoids platform-specific BLAS matmul paths.
    logits = np.sum(
        features[:, :, :, None] * weights[None, None, :, :],
        axis=2,
        dtype=np.float64,
    )
    logits = logits + bias[None, None, :]
    scores = np.tanh(logits)
    loss = float(np.mean((scores - targets) ** 2, dtype=np.float64))
    return scores, loss


def _backward(
    features: np.ndarray,
    targets: np.ndarray,
    scores: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    denominator = float(scores.size)
    d_scores = (2.0 / denominator) * (scores - targets)
    d_logits = d_scores * (1.0 - scores * scores)
    grad_weights = np.sum(
        features[:, :, :, None] * d_logits[:, :, None, :],
        axis=(0, 1),
        dtype=np.float64,
    )
    grad_bias = np.sum(d_logits, axis=(0, 1), dtype=np.float64)
    grad_norm = float(
        np.sqrt(
            np.sum(grad_weights * grad_weights, dtype=np.float64)
            + np.sum(grad_bias * grad_bias, dtype=np.float64)
        )
    )
    return grad_weights, grad_bias, grad_norm


def _prediction_checksum(scores: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"mixedpipe-scores-v1\x00")
    digest.update(str(scores.shape).encode("ascii"))
    digest.update(b"\x00")
    digest.update(np.ascontiguousarray(scores, dtype="<f8").tobytes(order="C"))
    return digest.hexdigest()


def execute_task(task: Task, config: PipelineConfig) -> dict[str, Any]:
    """Execute one task and return a canonical, checksummed result record."""

    _maybe_fault(task, "load")
    features = generate_features(task)
    targets = generate_targets(task)
    weights, bias = generate_model(task.model_tag, task.feature_dim)

    loss_values: list[float] = []
    grad_norm = 0.0
    scores: np.ndarray

    if task.job_kind == "train":
        for _ in range(task.iterations):
            _maybe_fault(task, "forward")
            scores, loss = _forward(features, weights, bias, targets)
            loss_values.append(loss)
            _maybe_fault(task, "backward")
            grad_weights, grad_bias, grad_norm = _backward(
                features, targets, scores
            )
            _maybe_fault(task, "update")
            weights -= config.learning_rate * grad_weights
            bias -= config.learning_rate * grad_bias
        _maybe_fault(task, "score")
        scores, _ = _forward(features, weights, bias, targets)
    else:
        for _ in range(task.iterations):
            _maybe_fault(task, "forward")
            scores, loss = _forward(features, weights, bias, targets)
            loss_values.append(loss)
        _maybe_fault(task, "score")
        grad_norm = 0.0

    summary = {
        "job_id": task.job_id,
        "job_kind": task.job_kind,
        "model_tag": task.model_tag,
        "seed": task.seed,
        "feature_dim": task.feature_dim,
        "batch_size": task.batch_size,
        "seq_len": task.seq_len,
        "iterations": task.iterations,
        "loss_first": loss_values[0],
        "loss_last": loss_values[-1],
        "loss_sequence": loss_values,
        "grad_norm": grad_norm,
        "prediction_checksum": _prediction_checksum(scores),
        "prediction_mean": float(np.mean(scores, dtype=np.float64)),
    }
    summary["summary_checksum"] = content_checksum(summary)

    result = {
        "schema_version": 1,
        "job_id": task.job_id,
        "status": "ok",
        "effective_task": task.to_dict(),
        "summary": summary,
    }
    result["record_checksum"] = content_checksum(result)
    return result


def error_result(
    task: Task,
    code: str,
    stage: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": 1,
        "job_id": task.job_id,
        "status": "error",
        "effective_task": task.to_dict(),
        "business_code": code,
        "stage": stage,
        "message": message,
        "details": details or {},
    }
    result["record_checksum"] = content_checksum(result)
    return result


def run_job(task: Task, config: PipelineConfig) -> dict[str, Any]:
    try:
        return execute_task(task, config)
    except BusinessError as exc:
        return error_result(task, exc.code, exc.stage, exc.message, exc.details)
    except Exception as exc:  # Defensive boundary: always return per-job failure.
        return error_result(
            task,
            E_WORKER_INTERNAL,
            "internal",
            f"{type(exc).__name__}: {exc}",
            {"job_id": task.job_id},
        )


def result_bytes(result: dict[str, Any]) -> bytes:
    return canonical_json_bytes(result)
