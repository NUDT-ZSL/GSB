"""Deterministic local process-pool execution of block gradient tasks."""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from typing import Any

import numpy as np

from .config import PrecisionConfig
from .errors import E_WORKER, EngineError
from .planner import ChunkPlan
from .precision import PrecisionOps


_WORKER_CONTEXT: dict[str, Any] | None = None


def _fault_matches(fault_spec: str | None, strength_index: int, block_index: int) -> bool:
    if not fault_spec:
        return False
    try:
        strength_part, block_part = fault_spec.split(":", 1)
        strength_match = strength_part == "*" or int(strength_part) == strength_index
        block_match = block_part == "*" or int(block_part) == block_index
        return strength_match and block_match
    except (ValueError, AttributeError):
        return False


def _initialize_worker_context(
    matrix: np.ndarray,
    targets: np.ndarray,
    precision: dict[str, Any],
    boundaries: tuple[tuple[int, int], ...],
    fault_spec: str | None,
) -> None:
    global _WORKER_CONTEXT
    matrix.setflags(write=False)
    targets.setflags(write=False)
    _WORKER_CONTEXT = {
        "matrix": matrix,
        "targets": targets,
        "ops": PrecisionOps(
            mode=str(precision["mode"]),
            fixed_scale=int(precision["fixed_scale"]),
            fixed_rounding=str(precision["fixed_rounding"]),
        ),
        "boundaries": boundaries,
        "fault_spec": fault_spec,
        "total_samples": int(matrix.shape[0]),
    }


def _worker_task(
    block_index: int,
    weights: np.ndarray,
    intercept: Any,
    strength_index: int,
    strength: float,
    operation: str,
) -> dict[str, Any]:
    context = _WORKER_CONTEXT
    if context is None:
        return {
            "ok": False,
            "error": {
                "code": E_WORKER,
                "message": "worker context was not initialized",
                "stage": "PATH_SOLVE",
                "strength_index": strength_index,
                "strength": strength,
                "block_index": block_index,
                "worker_pid": os.getpid(),
            },
        }
    boundaries = context["boundaries"]
    if block_index < 0 or block_index >= len(boundaries):
        return {
            "ok": False,
            "error": {
                "code": E_WORKER,
                "message": f"invalid block index {block_index}",
                "stage": "PATH_SOLVE",
                "strength_index": strength_index,
                "strength": strength,
                "block_index": block_index,
                "worker_pid": os.getpid(),
            },
        }
    block_start, block_end = boundaries[block_index]
    try:
        if _fault_matches(context["fault_spec"], strength_index, block_index):
            raise RuntimeError("injected worker failure for verification")
        matrix = context["matrix"][block_start:block_end]
        targets = context["targets"][block_start:block_end]
        ops: PrecisionOps = context["ops"]
        rows = block_end - block_start
        if operation == "gradient":
            gradient, intercept_gradient, sse = ops.gradient(
                matrix,
                targets,
                weights,
                intercept,
                context["total_samples"],
            )
            comparisons = rows * (matrix.shape[1] + 2)
            return {
                "ok": True,
                "block_index": block_index,
                "block_start": block_start,
                "block_end": block_end,
                "gradient": gradient,
                "intercept_gradient": intercept_gradient,
                "sse": sse,
                "rows": rows,
                "comparisons": comparisons,
                "worker_pid": os.getpid(),
            }
        if operation == "objective":
            sse, abs_residual_sum = ops.prediction_sse(matrix, targets, weights, intercept)
            comparisons = rows * (matrix.shape[1] + 2)
            return {
                "ok": True,
                "block_index": block_index,
                "block_start": block_start,
                "block_end": block_end,
                "sse": sse,
                "abs_residual_sum": abs_residual_sum,
                "rows": rows,
                "comparisons": comparisons,
                "worker_pid": os.getpid(),
            }
        raise ValueError(f"unknown block operation {operation!r}")
    except BaseException as exc:
        return {
            "ok": False,
            "error": {
                "code": E_WORKER,
                "message": f"{type(exc).__name__}: {exc}",
                "stage": "PATH_SOLVE",
                "strength_index": strength_index,
                "strength": strength,
                "block_index": block_index,
                "block_start": block_start,
                "block_end": block_end,
                "worker_pid": os.getpid(),
                "exception_type": type(exc).__name__,
            },
        }


def _rethrow_worker_error(error: dict[str, Any]) -> None:
    raise EngineError(
        E_WORKER,
        str(error.get("message", "worker failed")),
        stage=str(error.get("stage", "PATH_SOLVE")),
        strength=float(error["strength"]) if "strength" in error else None,
        strength_index=(
            int(error["strength_index"]) if "strength_index" in error else None
        ),
        block_index=int(error["block_index"]) if "block_index" in error else None,
        details={"worker_error": error},
    )


class BlockExecutor:
    """A persistent worker pool with deterministic task and reduction order."""

    def __init__(
        self,
        *,
        matrix: np.ndarray,
        targets: np.ndarray,
        precision: PrecisionConfig,
        plan: ChunkPlan,
        fault_spec: str | None = None,
    ) -> None:
        self._matrix = matrix
        self._targets = targets
        self._precision = asdict(precision)
        self._boundaries = plan.boundaries()
        self._workers = plan.workers
        self._fault_spec = fault_spec
        self._pool: ProcessPoolExecutor | None = None
        self._inline_context_active = False

    def __enter__(self) -> "BlockExecutor":
        if self._workers > 1:
            context = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=context,
                initializer=_initialize_worker_context,
                initargs=(
                    self._matrix,
                    self._targets,
                    self._precision,
                    self._boundaries,
                    self._fault_spec,
                ),
            )
        else:
            _initialize_worker_context(
                self._matrix,
                self._targets,
                self._precision,
                self._boundaries,
                self._fault_spec,
            )
            self._inline_context_active = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        global _WORKER_CONTEXT
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        if self._inline_context_active:
            _WORKER_CONTEXT = None
            self._inline_context_active = False

    def run(
        self,
        *,
        weights: np.ndarray,
        intercept: Any,
        strength_index: int,
        strength: float,
        operation: str,
    ) -> list[dict[str, Any]]:
        if self._pool is None and not self._inline_context_active:
            raise EngineError(E_WORKER, "block executor is not active", stage="PATH_SOLVE")
        if self._pool is None:
            results = [
                _worker_task(
                    block_index,
                    weights,
                    intercept,
                    strength_index,
                    strength,
                    operation,
                )
                for block_index in range(len(self._boundaries))
            ]
        else:
            futures = [
                self._pool.submit(
                    _worker_task,
                    block_index,
                    weights,
                    intercept,
                    strength_index,
                    strength,
                    operation,
                )
                for block_index in range(len(self._boundaries))
            ]
            results = []
            for block_index, future in enumerate(futures):
                try:
                    result = future.result()
                except BaseException as exc:
                    for pending in futures[block_index + 1 :]:
                        pending.cancel()
                    start, end = self._boundaries[block_index]
                    raise EngineError(
                        E_WORKER,
                        f"block worker terminated unexpectedly: {type(exc).__name__}: {exc}",
                        stage="PATH_SOLVE",
                        strength=strength,
                        strength_index=strength_index,
                        block_index=block_index,
                        details={
                            "block_start": start,
                            "block_end": end,
                            "exception_type": type(exc).__name__,
                        },
                    ) from exc
                results.append(result)
        for result in results:
            if not result.get("ok"):
                _rethrow_worker_error(result.get("error", {}))
        return results

