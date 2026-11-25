from __future__ import annotations

import fcntl
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import Codes, WorkbenchError


@dataclass(frozen=True)
class Budget:
    max_jobs_per_stage: int = 64
    max_accum_buffer_bytes: int = 64 * 1024 * 1024
    max_parallel_jobs: int = 4
    resource_policy: str = "degrade"

    def validated(self) -> "Budget":
        if self.max_jobs_per_stage <= 0:
            raise WorkbenchError(Codes.RESOURCE_LIMIT, "max_jobs_per_stage must be positive")
        if self.max_accum_buffer_bytes <= 0:
            raise WorkbenchError(Codes.RESOURCE_LIMIT, "max_accum_buffer_bytes must be positive")
        if self.max_parallel_jobs <= 0:
            raise WorkbenchError(Codes.RESOURCE_LIMIT, "max_parallel_jobs must be positive")
        if self.resource_policy not in {"degrade", "error"}:
            raise WorkbenchError(Codes.SCHEMA_INVALID, "resource_policy must be degrade or error")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommitLock:
    """Exclusive non-blocking lock for snapshots and incremental commits."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = None

    def acquire(self) -> "CommitLock":
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise WorkbenchError(
                Codes.STAGE_BUSY,
                "another stage snapshot or incremental commit is active",
                {"lock_path": str(self.path), "errno": getattr(exc, "errno", None)},
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "CommitLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def estimate_accum_buffer_bytes(
    *,
    op_kind: str,
    matrix_shapes: dict[str, list[int]],
    block_size: int,
    parallel_jobs: int = 1,
) -> dict[str, Any]:
    if op_kind == "gemm":
        m, k = matrix_shapes["A"]
        _, n = matrix_shapes["B"]
        batch = 1
    else:
        batch, m, k = matrix_shapes["A"]
        _, _, n = matrix_shapes["B"]
    tile_m = min(int(block_size), m)
    tile_n = min(int(block_size), n)
    tile_elements = tile_m * tile_n
    output_elements = batch * m * n
    # sum, Neumaier correction, protected subnormal guard and one term tile.
    tile_buffers = 4 * tile_elements * 8
    # Final resident output plus chunk digest/index overhead estimate.
    output_buffers = output_elements * 8
    block_panel_buffers = 2 * min(block_size, m) * min(block_size, n) * 8
    per_job = tile_buffers + output_buffers + block_panel_buffers
    total = per_job * max(1, int(parallel_jobs))
    return {
        "estimated_bytes": int(total),
        "per_job_bytes": int(per_job),
        "parallel_jobs": int(parallel_jobs),
        "tile_elements": int(tile_elements),
        "output_elements": int(output_elements),
    }


def choose_effective_block_size(
    *,
    op_kind: str,
    matrix_shapes: dict[str, list[int]],
    requested_block_size: int,
    budget: Budget,
    parallel_jobs: int,
) -> tuple[int, list[dict[str, Any]]]:
    degradations: list[dict[str, Any]] = []
    effective = int(requested_block_size)
    estimate = estimate_accum_buffer_bytes(
        op_kind=op_kind,
        matrix_shapes=matrix_shapes,
        block_size=effective,
        parallel_jobs=parallel_jobs,
    )
    if estimate["estimated_bytes"] <= budget.max_accum_buffer_bytes:
        return effective, degradations

    if budget.resource_policy == "error":
        raise WorkbenchError(
            Codes.RESOURCE_LIMIT,
            "resident block accumulation buffer estimate exceeds budget",
            {
                "estimate": estimate,
                "budget_bytes": budget.max_accum_buffer_bytes,
                "category": "MEMORY_BUDGET_EXCEEDED",
            },
        )

    while effective > 1:
        effective = max(1, effective // 2)
        estimate = estimate_accum_buffer_bytes(
            op_kind=op_kind,
            matrix_shapes=matrix_shapes,
            block_size=effective,
            parallel_jobs=parallel_jobs,
        )
        if estimate["estimated_bytes"] <= budget.max_accum_buffer_bytes:
            degradations.append(
                {
                    "category": "MEMORY_BUDGET_REDUCED_BLOCK",
                    "requested_block_size": int(requested_block_size),
                    "effective_block_size": effective,
                    "estimated_bytes": estimate["estimated_bytes"],
                    "budget_bytes": budget.max_accum_buffer_bytes,
                }
            )
            return effective, degradations

    raise WorkbenchError(
        Codes.RESOURCE_LIMIT,
        "even 1x1 block accumulation buffer exceeds memory budget",
        {
            "estimate": estimate,
            "budget_bytes": budget.max_accum_buffer_bytes,
            "category": "MEMORY_BUDGET_EXCEEDED",
        },
    )
