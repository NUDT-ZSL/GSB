"""Domain models for tasks, block plans, resources, and execution reports."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .errors import input_error
from .hashing import sha256_json


SUPPORTED_DTYPES = frozenset({"float16", "float32", "float64"})


@dataclass(frozen=True)
class TaskSpec:
    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    q_block_len: int
    kv_block_len: int
    weight_revision: int
    dtype: str
    causal: bool
    seed: int
    q_path: Path
    k_path: Path
    v_path: Path

    @classmethod
    def from_record(cls, record: dict[str, Any], manifest_dir: Path) -> "TaskSpec":
        required = (
            "case_id",
            "seq_len",
            "head_dim",
            "num_heads",
            "q_block_len",
            "kv_block_len",
            "weight_revision",
            "dtype",
            "causal",
            "seed",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise input_error("task is missing required fields", missing=missing)

        case_id = record["case_id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise input_error("case_id must be a non-empty string")
        if any(token in case_id for token in ("/", "\\", "\x00")):
            raise input_error("case_id must not contain path separators", case_id=case_id)

        integer_fields = (
            "seq_len",
            "head_dim",
            "num_heads",
            "q_block_len",
            "kv_block_len",
            "weight_revision",
            "seed",
        )
        values: dict[str, int] = {}
        for key in integer_fields:
            value = record[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise input_error(f"{key} must be an integer", field=key, value=value)
            values[key] = value

        for key in ("seq_len", "head_dim", "num_heads", "q_block_len", "kv_block_len"):
            if values[key] <= 0:
                raise input_error(f"{key} must be positive", field=key, value=values[key])
        if values["weight_revision"] < 1:
            raise input_error("weight_revision must be >= 1")
        if values["seed"] < 0:
            raise input_error("seed must be >= 0")
        if values["q_block_len"] > values["seq_len"]:
            raise input_error("q_block_len must not exceed seq_len")
        if values["kv_block_len"] > values["seq_len"]:
            raise input_error("kv_block_len must not exceed seq_len")

        causal = record["causal"]
        if not isinstance(causal, bool):
            raise input_error("causal must be a boolean", value=causal)

        try:
            dtype = np.dtype(record["dtype"]).name
        except (TypeError, ValueError) as exc:
            raise input_error("dtype is not a NumPy dtype", value=record["dtype"]) from exc
        if dtype not in SUPPORTED_DTYPES:
            raise input_error(
                "dtype must be one of float16, float32, float64",
                value=record["dtype"],
            )

        paths: dict[str, Path] = {}
        for key, letter in (("q_path", "q"), ("k_path", "k"), ("v_path", "v")):
            value = record.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise input_error(f"{key} must be a non-empty path", field=key)
                path = Path(value)
                if not path.is_absolute():
                    path = manifest_dir / path
                paths[key] = path.resolve()
                continue
            candidates = (
                manifest_dir / "arrays" / case_id / f"rev{values['weight_revision']}" / f"{letter}.npy",
                manifest_dir / case_id / f"rev{values['weight_revision']}" / f"{letter}.npy",
                manifest_dir / "arrays" / case_id / f"{letter}.npy",
                manifest_dir / case_id / f"{letter}.npy",
                manifest_dir / f"{case_id}.{letter}.npy",
                manifest_dir / f"{case_id}_{letter}.npy",
            )
            selected = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
            paths[key] = selected.resolve()

        return cls(
            case_id=case_id,
            seq_len=values["seq_len"],
            head_dim=values["head_dim"],
            num_heads=values["num_heads"],
            q_block_len=values["q_block_len"],
            kv_block_len=values["kv_block_len"],
            weight_revision=values["weight_revision"],
            dtype=dtype,
            causal=causal,
            seed=values["seed"],
            q_path=paths["q_path"],
            k_path=paths["k_path"],
            v_path=paths["v_path"],
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "seq_len": self.seq_len,
            "head_dim": self.head_dim,
            "num_heads": self.num_heads,
            "q_block_len": self.q_block_len,
            "kv_block_len": self.kv_block_len,
            "weight_revision": self.weight_revision,
            "dtype": self.dtype,
            "causal": self.causal,
            "seed": self.seed,
            "q_path": str(self.q_path),
            "k_path": str(self.k_path),
            "v_path": str(self.v_path),
        }


@dataclass(frozen=True)
class ArraySet:
    q: np.ndarray = field(repr=False)
    k: np.ndarray = field(repr=False)
    v: np.ndarray = field(repr=False)
    q_sha256: str
    k_sha256: str
    v_sha256: str
    qkv_sha256: str

    def float64(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (
            np.asarray(self.q, dtype=np.float64),
            np.asarray(self.k, dtype=np.float64),
            np.asarray(self.v, dtype=np.float64),
        )


@dataclass(frozen=True)
class BlockRange:
    index: int
    start: int
    length: int

    @property
    def stop(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class BlockPlan:
    head: int
    q_block: int
    q_start: int
    q_len: int
    kv_blocks: tuple[BlockRange, ...]

    @property
    def node_suffix(self) -> str:
        return f"h{self.head}:q{self.q_block}"


@dataclass
class ResourcePolicy:
    max_resident_bytes: int = 256 * 1024 * 1024
    max_parallel_q_blocks: int = 4
    allow_serial_degrade: bool = True

    def __post_init__(self) -> None:
        if self.max_resident_bytes <= 0:
            raise input_error("max_resident_bytes must be positive")
        if self.max_parallel_q_blocks <= 0:
            raise input_error("max_parallel_q_blocks must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_resident_bytes": self.max_resident_bytes,
            "max_parallel_q_blocks": self.max_parallel_q_blocks,
            "allow_serial_degrade": self.allow_serial_degrade,
        }


@dataclass
class ResourceUsage:
    estimated_resident_bytes: int = 0
    parallel_tasks: int = 0
    parallel_workers_used: int = 1
    parallel_limit: int = 1
    degraded: bool = False
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_resident_bytes": self.estimated_resident_bytes,
            "parallel_tasks": self.parallel_tasks,
            "parallel_workers_used": self.parallel_workers_used,
            "parallel_limit": self.parallel_limit,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


@dataclass
class CaseRunResult:
    case_id: str
    business_code: str
    snapshot_version: int
    weight_revision: int
    idempotent_reuse: bool
    input_sha256: str
    output_sha256: str
    output_path: str
    recomputed: list[str]
    reused: list[str]
    changed_heads: list[int]
    affected_q_blocks: list[str]
    resource: ResourceUsage
    verification: dict[str, Any]
    cache_hits: int
    cache_misses: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "business_code": self.business_code,
            "snapshot_version": self.snapshot_version,
            "weight_revision": self.weight_revision,
            "idempotent_reuse": self.idempotent_reuse,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "output_path": self.output_path,
            "recomputed": self.recomputed,
            "reused": self.reused,
            "changed_heads": self.changed_heads,
            "affected_q_blocks": self.affected_q_blocks,
            "resource": self.resource.to_dict(),
            "verification": self.verification,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


@dataclass
class VerificationResult:
    case_id: str
    snapshot_version: int
    ok: bool
    tolerance: float
    tolerance_rtol: float
    output_sha256: str
    node_count: int
    checks: list[str]
    first_difference: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "snapshot_version": self.snapshot_version,
            "ok": self.ok,
            "tolerance": self.tolerance,
            "tolerance_rtol": self.tolerance_rtol,
            "output_sha256": self.output_sha256,
            "node_count": self.node_count,
            "checks": self.checks,
            "first_difference": self.first_difference,
        }


def estimate_q_block_resident_bytes(
    task: TaskSpec,
    plan: BlockPlan,
) -> int:
    """Conservative peak bytes for one Q-block worker, all intermediates float64."""

    dtype_bytes = np.dtype(np.float64).itemsize
    q_len = plan.q_len
    kv_max = max((block.length for block in plan.kv_blocks), default=0)
    head_dim = task.head_dim
    score_elements = q_len * kv_max
    # Q, K, V, scores, exp scores, partial output, accumulator, masks, and stats.
    elements = (
        q_len * head_dim * 4
        + kv_max * head_dim * 2
        + score_elements * 3
        + q_len * 8
    )
    return elements * dtype_bytes * 2


def select_parallel_workers(
    group_count: int,
    per_worker_bytes: int,
    policy: ResourcePolicy,
) -> ResourceUsage:
    if group_count <= 0:
        return ResourceUsage(
            estimated_resident_bytes=0,
            parallel_tasks=0,
            parallel_workers_used=1,
            parallel_limit=policy.max_parallel_q_blocks,
        )

    if per_worker_bytes > policy.max_resident_bytes:
        from .errors import DiagnosticError, E_RESOURCE_LIMIT

        raise DiagnosticError(
            E_RESOURCE_LIMIT,
            "one Q block exceeds max_resident_bytes",
            required_bytes=per_worker_bytes,
            limit_bytes=policy.max_resident_bytes,
        )

    memory_workers = policy.max_resident_bytes // max(per_worker_bytes, 1)
    limited = min(
        group_count,
        policy.max_parallel_q_blocks,
        max(1, int(memory_workers)),
    )
    reasons: list[str] = []
    if group_count > policy.max_parallel_q_blocks:
        reasons.append("parallel_q_block_limit")
    if memory_workers < min(group_count, policy.max_parallel_q_blocks):
        reasons.append("resident_memory_limit")

    if reasons and not policy.allow_serial_degrade:
        from .errors import DiagnosticError, E_RESOURCE_LIMIT

        raise DiagnosticError(
            E_RESOURCE_LIMIT,
            "resource policy would require parallel degradation",
            parallel_tasks=group_count,
            parallel_limit=policy.max_parallel_q_blocks,
            estimated_resident_bytes=per_worker_bytes * min(group_count, policy.max_parallel_q_blocks),
            limit_bytes=policy.max_resident_bytes,
        )

    if reasons:
        workers = 1
        degraded = True
    else:
        workers = limited
        degraded = False

    return ResourceUsage(
        estimated_resident_bytes=per_worker_bytes * workers,
        parallel_tasks=group_count,
        parallel_workers_used=workers,
        parallel_limit=policy.max_parallel_q_blocks,
        degraded=degraded,
        degraded_reason=",".join(reasons) if reasons else None,
    )


def stable_output_path(root: Path, case_id: str, version: int) -> Path:
    return root / "exports" / case_id / f"v{version}" / "output.npy"


def make_case_cache_scope(task: TaskSpec) -> dict[str, Any]:
    return {
        "case_id": task.case_id,
        "q_block_len": task.q_block_len,
        "kv_block_len": task.kv_block_len,
        "dtype": task.dtype,
        "causal": task.causal,
        "seed": task.seed,
    }


def canonical_task_key(task: TaskSpec) -> str:
    return sha256_json(task.canonical())


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(path.parent, os.W_OK):
        raise input_error("output directory is not writable", path=str(path.parent))
