from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .config import PipelineConfig
from .errors import error
from .jobs import JobSpec


@dataclass
class StrategyPlan:
    job_id: str
    op_kind: str
    batch_size: int
    dimensions: dict[str, int]
    block_size: dict[str, int]
    loop_order: list[str]
    batch_path: str
    block_count: int
    estimated_flops: int
    estimated_cost: int
    resident_bytes: int
    degraded: bool
    degradation_categories: list[str] = field(default_factory=list)
    decision_basis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op_kind": self.op_kind,
            "batch_size": self.batch_size,
            "dimensions": self.dimensions,
            "block_size": self.block_size,
            "loop_order": self.loop_order,
            "batch_path": self.batch_path,
            "block_count": self.block_count,
            "estimated_flops": self.estimated_flops,
            "estimated_cost": self.estimated_cost,
            "resident_bytes": self.resident_bytes,
            "degraded": self.degraded,
            "degradation_categories": self.degradation_categories,
            "decision_basis": self.decision_basis,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    def checksum(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def covers(self) -> bool:
        return (
            self.block_size["m"] > 0
            and self.block_size["n"] > 0
            and self.block_size["k"] > 0
            and _ceil_div(self.dimensions["m"], self.block_size["m"])
            * _ceil_div(self.dimensions["n"], self.block_size["n"])
            * _ceil_div(self.dimensions["k"], self.block_size["k"])
            * (
                1
                if self.batch_path == "batched"
                else self.dimensions["batch"]
            )
            == self.block_count
        )


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _tile_dims(job: JobSpec, sizes: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = max(1, job.block_size)
    return (
        min(sizes[0], raw),
        min(sizes[1], raw),
        min(sizes[2], raw),
    )


def _estimate(
    job: JobSpec,
    block_m: int,
    block_n: int,
    block_k: int,
    batch_path: str,
) -> tuple[int, int, int, int]:
    batch = job.batch_size
    m_tiles = _ceil_div(job.m, block_m)
    n_tiles = _ceil_div(job.n, block_n)
    k_tiles = _ceil_div(job.k, block_k)
    batch_tiles = 1 if batch_path == "batched" else batch
    block_count = batch_tiles * m_tiles * n_tiles * k_tiles
    flops = 2 * batch * job.m * job.n * job.k
    batch_elements = batch if batch_path == "batched" else 1
    resident_elements = (
        batch_elements * block_m * block_k
        + batch_elements * block_k * block_n
        + batch_elements * block_m * block_n
        + (batch_elements * block_m * block_n if job.accum_mode != "plain" else 0)
    )
    resident_bytes = resident_elements * 8
    overhead = block_count * (block_m * block_n * 4 + 256) + batch * 512
    return block_count, flops, resident_bytes + overhead, resident_bytes


def choose_strategy(job: JobSpec, config: PipelineConfig) -> StrategyPlan:
    dimensions = {
        "batch": job.batch_size,
        "m": job.m,
        "n": job.n,
        "k": job.k,
    }
    requested = max(1, job.block_size)
    block_m, block_n, block_k = _tile_dims(job, (job.m, job.n, job.k))
    categories: list[str] = []

    full_batch_bytes = job.batch_size * job.m * job.n * 8
    if job.batch_size > 1 and full_batch_bytes > config.max_accum_bytes:
        batch_path = "per_batch"
        categories.append("BATCH_AGGREGATION_DISABLED")
    elif job.batch_size > 1:
        batch_path = "batched"
    else:
        batch_path = "scalar_batch"

    while True:
        block_count, flops, cost, resident_bytes = _estimate(
            job, block_m, block_n, block_k, batch_path
        )
        if resident_bytes <= config.max_accum_bytes or (
            block_m == 1 and block_n == 1 and block_k == 1
        ):
            break
        next_m = max(1, block_m // 2)
        next_n = max(1, block_n // 2)
        next_k = max(1, block_k // 2)
        if (next_m, next_n, next_k) == (block_m, block_n, block_k):
            break
        block_m, block_n, block_k = next_m, next_n, next_k
        if "SMALLER_BLOCKS" not in categories:
            categories.append("SMALLER_BLOCKS")

    block_count, flops, cost, resident_bytes = _estimate(
        job, block_m, block_n, block_k, batch_path
    )

    if block_count > config.max_block_count:
        raise error(
            "E_RESOURCE_LIMIT",
            "分块数量超过配置上限",
            job_id=job.job_id,
            field="block_size",
            stage="planning",
            details={
                "block_count": block_count,
                "max_block_count": config.max_block_count,
                "block_size": [block_m, block_n, block_k],
            },
        )
    if resident_bytes > config.max_accum_bytes:
        raise error(
            "E_RESOURCE_LIMIT",
            "驻留分块累加缓冲估算超过配置上限，且无法继续降级",
            job_id=job.job_id,
            field="max_accum_bytes",
            stage="planning",
            details={
                "resident_bytes": resident_bytes,
                "max_accum_bytes": config.max_accum_bytes,
            },
        )

    if batch_path == "batched":
        loop_order = ["batch", "m", "n", "k"]
    elif job.layout == "col":
        loop_order = ["m", "n", "batch", "k"]
    else:
        loop_order = ["batch", "m", "n", "k"]

    plan = StrategyPlan(
        job_id=job.job_id,
        op_kind=job.op_kind,
        batch_size=job.batch_size,
        dimensions=dimensions,
        block_size={"m": block_m, "n": block_n, "k": block_k},
        loop_order=loop_order,
        batch_path=batch_path,
        block_count=block_count,
        estimated_flops=flops,
        estimated_cost=cost,
        resident_bytes=resident_bytes,
        degraded=bool(categories),
        degradation_categories=categories,
        decision_basis={
            "requested_block_size": requested,
            "max_accum_bytes": config.max_accum_bytes,
            "max_block_count": config.max_block_count,
            "dimensions": dimensions,
            "full_batch_bytes": full_batch_bytes,
            "ceil_tiles": {
                "m": _ceil_div(job.m, block_m),
                "n": _ceil_div(job.n, block_n),
                "k": _ceil_div(job.k, block_k),
                "batch": 1 if batch_path == "batched" else job.batch_size,
            },
            "formula": {
                "flops": "2*batch*m*n*k",
                "blocks": "batch_tiles*m_tiles*n_tiles*k_tiles",
                "resident_bytes": "float64 tile buffers including compensation",
            },
        },
    )
    if not plan.covers():
        raise error(
            "E_RESOURCE_LIMIT",
            "策略分块未覆盖完整矩阵",
            job_id=job.job_id,
            field="block_size",
            stage="planning",
            details={"plan": plan.to_dict()},
        )
    return plan


def plan_from_dict(payload: dict[str, Any]) -> StrategyPlan:
    return StrategyPlan(
        job_id=payload["job_id"],
        op_kind=payload["op_kind"],
        batch_size=int(payload["batch_size"]),
        dimensions=dict(payload["dimensions"]),
        block_size=dict(payload["block_size"]),
        loop_order=list(payload["loop_order"]),
        batch_path=payload["batch_path"],
        block_count=int(payload["block_count"]),
        estimated_flops=int(payload["estimated_flops"]),
        estimated_cost=int(payload["estimated_cost"]),
        resident_bytes=int(payload["resident_bytes"]),
        degraded=bool(payload["degraded"]),
        degradation_categories=list(payload["degradation_categories"]),
        decision_basis=dict(payload["decision_basis"]),
    )
