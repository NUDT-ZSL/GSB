"""Deterministic full-batch gradient descent with isolated batch failures."""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .config import ExperimentConfig, RegularizerSpec
from .data import Dataset
from .errors import (
    E_BATCH_FAILURE,
    E_SNAPSHOT_FAILURE,
    E_UPDATE_FAILURE,
    Failure,
    sorted_failures,
)
from .fingerprint import cache_key, config_hash, training_fingerprint
from .objective import evaluate_regularization, mean_squared_error
from .snapshot import SnapshotStore


@dataclass
class BatchComputation:
    round_number: int
    batch_index: int
    stable_key: tuple[int, int]
    loss_sum: float = 0.0
    gradient_sum: np.ndarray | None = None
    intercept_gradient_sum: float = 0.0
    failure: Failure | None = None


def _batch_worker(
    *,
    round_number: int,
    batch_index: int,
    X_batch: np.ndarray,
    y_batch: np.ndarray,
    weights: np.ndarray,
    intercept: float,
) -> BatchComputation:
    result = BatchComputation(
        round_number=round_number,
        batch_index=batch_index,
        stable_key=(round_number, batch_index),
        gradient_sum=np.zeros_like(weights),
    )

    try:
        predictions = np.dot(X_batch, weights) + intercept
    except Exception as exc:
        result.failure = Failure(
            business_code=E_BATCH_FAILURE,
            phase="batch_objective",
            stage="prediction",
            message=f"batch prediction failed: {exc}",
            exception_type=type(exc).__name__,
            round=round_number,
            batch_index=batch_index,
        )
        return result

    try:
        residual = predictions - y_batch
        result.loss_sum = float(np.sum(residual * residual))
    except Exception as exc:
        result.failure = Failure(
            business_code=E_BATCH_FAILURE,
            phase="batch_objective",
            stage="data_loss",
            message=f"batch data-loss calculation failed: {exc}",
            exception_type=type(exc).__name__,
            round=round_number,
            batch_index=batch_index,
        )
        return result

    try:
        result.gradient_sum = np.asarray(2.0 * np.dot(X_batch.T, residual), dtype=np.float64)
        result.intercept_gradient_sum = float(2.0 * np.sum(residual))
    except Exception as exc:
        result.failure = Failure(
            business_code=E_BATCH_FAILURE,
            phase="batch_gradient",
            stage="data_gradient",
            message=f"batch data-gradient calculation failed: {exc}",
            exception_type=type(exc).__name__,
            round=round_number,
            batch_index=batch_index,
        )
    return result


class DeterministicTrainer:
    """Plain gradient descent where batch work is concurrent but aggregation is fixed-order."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        dataset: Dataset,
        scheme: RegularizerSpec,
        snapshot_store: SnapshotStore,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self.dataset = dataset
        self.scheme = scheme
        self.snapshot_store = snapshot_store
        self.stop_event = stop_event or threading.Event()
        self.config_hash = config_hash(config, dataset, scheme)
        self.cache_key = cache_key(scheme, dataset.data_fingerprint)
        self.training_fingerprint = training_fingerprint(config, dataset, scheme)
        self.batch_ranges = tuple(
            (index + 1, start, min(start + config.optimizer.batch_size, dataset.train_size))
            for index, start in enumerate(range(0, dataset.train_size, config.optimizer.batch_size))
        )
        self.failures: list[Failure] = []

    def request_stop(self) -> None:
        self.stop_event.set()

    def _batch_results_for_round(
        self,
        round_number: int,
        weights: np.ndarray,
        intercept: float,
        executor: ThreadPoolExecutor,
    ) -> list[BatchComputation]:
        futures = {}
        for batch_index, start, end in self.batch_ranges:
            future = executor.submit(
                _batch_worker,
                round_number=round_number,
                batch_index=batch_index,
                X_batch=self.dataset.X_train[start:end],
                y_batch=self.dataset.y_train[start:end],
                weights=weights,
                intercept=intercept,
            )
            futures[future] = (round_number, batch_index)

        results: list[BatchComputation] = []
        for future in as_completed(futures):
            key = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    BatchComputation(
                        round_number=key[0],
                        batch_index=key[1],
                        stable_key=key,
                        failure=Failure(
                            business_code=E_BATCH_FAILURE,
                            phase="batch_execution",
                            stage="thread_task",
                            message=f"unexpected batch task failure: {exc}",
                            exception_type=type(exc).__name__,
                            round=key[0],
                            batch_index=key[1],
                        ),
                    )
                )
        results.sort(key=lambda item: item.stable_key)
        return results

    def _snapshot(
        self,
        *,
        round_number: int,
        status: str,
        weights: np.ndarray,
        intercept: float,
        loss_trajectory: list[dict[str, Any]],
        optimizer_state: dict[str, Any],
        commit_pointer: bool,
    ) -> dict[str, Any] | None:
        try:
            payload = self.snapshot_store.write_snapshot(
                config_schema_version=self.config.schema_version,
                config_hash=self.config_hash,
                cache_key=self.cache_key,
                training_fingerprint=self.training_fingerprint,
                scheme_name=self.scheme.name,
                scheme_type=self.scheme.kind,
                round_number=round_number,
                status=status,
                weights=weights,
                intercept=intercept,
                feature_names=self.dataset.feature_names,
                loss_trajectory=loss_trajectory,
                optimizer_state=optimizer_state,
                data_fingerprint=self.dataset.data_fingerprint,
                commit_pointer=commit_pointer,
            )
            return payload
        except Exception as exc:
            self.failures.append(
                Failure(
                    business_code=E_SNAPSHOT_FAILURE,
                    phase="snapshot_write",
                    stage="snapshot_atomic_write",
                    message=f"snapshot write failed; in-memory training continues: {exc}",
                    exception_type=type(exc).__name__,
                    scheme=self.scheme.name,
                    round=round_number,
                    batch_index=None,
                )
            )
            return None

    def _validation_metrics(self, weights: np.ndarray, intercept: float) -> tuple[float, float]:
        train_predictions = np.dot(self.dataset.X_train, weights) + intercept
        validation_predictions = np.dot(self.dataset.X_validation, weights) + intercept
        return (
            mean_squared_error(train_predictions, self.dataset.y_train),
            mean_squared_error(validation_predictions, self.dataset.y_validation),
        )

    def _result(
        self,
        *,
        status: str,
        converged: bool,
        weights: np.ndarray,
        intercept: float,
        loss_trajectory: list[dict[str, Any]],
        snapshot_version: int,
        last_snapshot_round: int,
        training_executed_rounds: list[int],
        resumed_from_round: int | None,
        resumed_snapshot_version: int | None,
    ) -> dict[str, Any]:
        train_mse, validation_mse = self._validation_metrics(weights, intercept)
        return {
            "scheme": self.scheme.name,
            "regularization": self.scheme.to_dict(),
            "status": status,
            "converged": bool(converged),
            "iterations_completed": len(loss_trajectory),
            "last_snapshot_round": int(last_snapshot_round),
            "snapshot_version": int(snapshot_version),
            "config_hash": self.config_hash,
            "cache_key": self.cache_key,
            "training_fingerprint": self.training_fingerprint,
            "data_fingerprint": self.dataset.data_fingerprint,
            "final_coefficients": {
                "intercept": float(intercept),
                "weights": [float(value) for value in weights],
                "named_weights": {
                    name: float(value) for name, value in zip(self.dataset.feature_names, weights)
                },
            },
            "loss_trajectory": loss_trajectory,
            "train_mse": float(train_mse),
            "validation_mse": float(validation_mse),
            "training_executed_rounds": list(training_executed_rounds),
            "resumed_from_round": resumed_from_round,
            "resumed_snapshot_version": resumed_snapshot_version,
            "batch": {
                "batch_size": self.config.optimizer.batch_size,
                "batch_count": len(self.batch_ranges),
                "stable_order": "round_then_batch_index",
            },
            "snapshot_commit_interval": self.config.runtime.snapshot_commit_interval,
            "failures": [],
        }

    @staticmethod
    def _trajectory_from_resume(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        trajectory: list[dict[str, Any]] = []
        for item in snapshot.get("loss_trajectory", []):
            if isinstance(item, dict):
                trajectory.append(dict(item))
        return trajectory

    def resume_or_train(
        self,
        *,
        resume_snapshot: dict[str, Any] | None = None,
        pause_after_round: int | None = None,
    ) -> dict[str, Any]:
        weights = np.zeros(self.dataset.feature_count, dtype=np.float64)
        intercept = 0.0
        trajectory: list[dict[str, Any]] = []
        start_round = 1
        resumed_from_round: int | None = None
        resumed_snapshot_version: int | None = None
        snapshot_version = 0
        last_snapshot_round = 0
        converged = False
        last_snapshot_payload: dict[str, Any] | None = None
        last_snapshot_pointer_committed = False

        if resume_snapshot is not None:
            snapshot_features = resume_snapshot.get("feature_names")
            if list(snapshot_features or []) != list(self.dataset.feature_names):
                self.failures.append(
                    Failure(
                        business_code="E_SNAPSHOT_INVALID",
                        phase="snapshot_restore",
                        stage="feature_schema_check",
                        message="snapshot feature schema does not match regenerated deterministic data",
                        exception_type="SnapshotSchemaError",
                        scheme=self.scheme.name,
                        round=int(resume_snapshot.get("round", 0)),
                        details={"snapshot_version": resume_snapshot.get("snapshot_version")},
                    )
                )
                resume_snapshot = None

        if resume_snapshot is not None:
            coefficients = resume_snapshot["coefficients"]
            weights = np.asarray(coefficients["weights"], dtype=np.float64)
            intercept = float(coefficients["intercept"])
            trajectory = self._trajectory_from_resume(resume_snapshot)
            resumed_from_round = int(resume_snapshot["round"])
            resumed_snapshot_version = int(resume_snapshot["snapshot_version"])
            snapshot_version = resumed_snapshot_version
            last_snapshot_round = resumed_from_round
            start_round = resumed_from_round + 1
            optimizer_state = resume_snapshot.get("optimizer_state", {})
            converged = bool(optimizer_state.get("converged", False))
            if resume_snapshot.get("status") == "complete":
                result = self._result(
                    status="complete",
                    converged=converged,
                    weights=weights,
                    intercept=intercept,
                    loss_trajectory=trajectory,
                    snapshot_version=snapshot_version,
                    last_snapshot_round=last_snapshot_round,
                    training_executed_rounds=[],
                    resumed_from_round=resumed_from_round,
                    resumed_snapshot_version=resumed_snapshot_version,
                )
                result["failures"] = [failure.to_dict() for failure in sorted_failures(self.failures)]
                return result

        training_executed_rounds: list[int] = []
        status = "complete"
        max_iterations = self.config.optimizer.max_iterations
        tolerance = self.config.optimizer.tolerance
        learning_rate = self.config.optimizer.learning_rate
        fit_intercept = self.config.optimizer.fit_intercept

        with ThreadPoolExecutor(
            max_workers=self.config.runtime.concurrency,
            thread_name_prefix="regularization-lab-batch",
        ) as executor:
            for round_number in range(start_round, max_iterations + 1):
                if self.stop_event.is_set():
                    status = "interrupted"
                    break

                batch_results = self._batch_results_for_round(
                    round_number, weights, intercept, executor
                )
                gradient_sum = np.zeros_like(weights)
                data_loss_sum = 0.0
                intercept_gradient_sum = 0.0
                for batch in batch_results:
                    if batch.failure is not None:
                        self.failures.append(batch.failure)
                        continue
                    if batch.gradient_sum is not None:
                        for feature_index in range(weights.size):
                            gradient_sum[feature_index] += float(batch.gradient_sum[feature_index])
                    data_loss_sum += float(batch.loss_sum)
                    intercept_gradient_sum += float(batch.intercept_gradient_sum)

                data_loss = float(data_loss_sum / self.dataset.train_size)
                gradient_data = gradient_sum / self.dataset.train_size
                intercept_gradient = float(intercept_gradient_sum / self.dataset.train_size)

                try:
                    regularization = evaluate_regularization(weights, self.scheme)
                    regularization_loss = regularization.loss
                    regularization_gradient = regularization.gradient
                    regularization_components = regularization.components()
                    total_loss = float(data_loss + regularization_loss)
                    gradient = gradient_data + regularization_gradient
                    if not math.isfinite(total_loss) or not np.all(np.isfinite(gradient)):
                        raise FloatingPointError("non-finite objective or gradient")
                except Exception as exc:
                    self.failures.append(
                        Failure(
                            business_code=E_UPDATE_FAILURE,
                            phase="gradient_update",
                            stage="regularization_objective",
                            message=f"regularization objective failed: {exc}",
                            exception_type=type(exc).__name__,
                            scheme=self.scheme.name,
                            round=round_number,
                            batch_index=None,
                        )
                    )
                    regularization_loss = 0.0
                    regularization_components = {"l1": 0.0, "l2": 0.0}
                    total_loss = data_loss
                    coefficient_delta = np.zeros_like(weights)
                    intercept_delta = 0.0
                    update_norm = 0.0
                else:
                    try:
                        update = learning_rate * gradient
                        intercept_update = (
                            learning_rate * intercept_gradient if fit_intercept else 0.0
                        )
                        coefficient_delta = -update
                        intercept_delta = -intercept_update
                        update_norm = float(
                            np.sqrt(np.sum(coefficient_delta * coefficient_delta) + intercept_delta * intercept_delta)
                        )
                        if not np.all(np.isfinite(coefficient_delta)) or not math.isfinite(intercept_delta):
                            raise FloatingPointError("non-finite parameter update")
                    except Exception as exc:
                        self.failures.append(
                            Failure(
                                business_code=E_UPDATE_FAILURE,
                                phase="gradient_update",
                                stage="parameter_update",
                                message=f"parameter update failed: {exc}",
                                exception_type=type(exc).__name__,
                                scheme=self.scheme.name,
                                round=round_number,
                                batch_index=None,
                            )
                        )
                        coefficient_delta = np.zeros_like(weights)
                        intercept_delta = 0.0
                        update_norm = 0.0

                previous_loss = float(trajectory[-1]["total_loss"]) if trajectory else None
                round_record = {
                    "round": round_number,
                    "data_loss": data_loss,
                    "regularization_loss": float(regularization_loss),
                    "regularization_components": regularization_components,
                    "total_loss": float(total_loss),
                    "update_norm": float(update_norm),
                    "coefficient_delta": [float(value) for value in coefficient_delta],
                    "intercept_delta": float(intercept_delta),
                }
                trajectory.append(round_record)
                weights = weights + coefficient_delta
                intercept = float(intercept + intercept_delta)
                training_executed_rounds.append(round_number)

                converged = bool(
                    previous_loss is not None and abs(previous_loss - total_loss) < tolerance
                )
                final_round = round_number == max_iterations
                stop_requested = self.stop_event.is_set() or (
                    pause_after_round is not None and round_number >= pause_after_round
                )
                if converged or final_round:
                    status = "complete"
                elif stop_requested:
                    status = "interrupted"
                else:
                    status = "running"

                optimizer_state = {
                    "algorithm": "deterministic_gradient_descent",
                    "learning_rate": learning_rate,
                    "batch_size": self.config.optimizer.batch_size,
                    "last_loss": float(total_loss),
                    "last_update_norm": float(update_norm),
                    "converged": converged,
                    "rounds_completed": round_number,
                }
                commit_pointer = bool(
                    round_number == 1
                    or round_number % self.config.runtime.snapshot_commit_interval == 0
                    or status != "running"
                )
                written_payload = self._snapshot(
                    round_number=round_number,
                    status=status,
                    weights=weights,
                    intercept=intercept,
                    loss_trajectory=trajectory,
                    optimizer_state=optimizer_state,
                    commit_pointer=commit_pointer,
                )
                if written_payload is not None:
                    last_snapshot_payload = written_payload
                    last_snapshot_pointer_committed = commit_pointer
                    snapshot_version = int(written_payload["snapshot_version"])
                    last_snapshot_round = round_number

                if status == "complete" or stop_requested:
                    break
                if self.config.runtime.round_delay_seconds > 0.0:
                    self.stop_event.wait(self.config.runtime.round_delay_seconds)

        if status == "running":
            status = "interrupted"
        if (
            status == "interrupted"
            and last_snapshot_payload is not None
            and not last_snapshot_pointer_committed
        ):
            try:
                interrupted_payload = dict(last_snapshot_payload)
                interrupted_payload["status"] = "interrupted"
                self.snapshot_store.commit_pointer(interrupted_payload)
            except Exception as exc:
                self.failures.append(
                    Failure(
                        business_code=E_SNAPSHOT_FAILURE,
                        phase="snapshot_write",
                        stage="progress_commit",
                        message=f"interrupted progress commit failed: {exc}",
                        exception_type=type(exc).__name__,
                        scheme=self.scheme.name,
                        round=last_snapshot_round,
                    )
                )
        result = self._result(
            status=status,
            converged=converged,
            weights=weights,
            intercept=intercept,
            loss_trajectory=trajectory,
            snapshot_version=snapshot_version,
            last_snapshot_round=last_snapshot_round,
            training_executed_rounds=training_executed_rounds,
            resumed_from_round=resumed_from_round,
            resumed_snapshot_version=resumed_snapshot_version,
        )
        result["failures"] = [failure.to_dict() for failure in sorted_failures(self.failures)]
        return result
