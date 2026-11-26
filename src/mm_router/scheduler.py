"""Deterministic process-pool scheduling, resource adjudication, and recovery."""

from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache import ContentAddressedCache, TransactionalCache
from .checkpoint import CheckpointStore, schedule_key
from .errors import (
    E_BUDGET_EXCEEDED,
    E_CHECKPOINT_CORRUPT,
    E_FORMAT_CORRUPT,
    E_RESOURCE_ADJUSTED,
    OK,
    WorkbenchError,
    corrupt,
)
from .models import InferenceRequest, PRECISION_RANK, ThresholdConfig
from .routing import RoutingPipeline


PRECISION_WEIGHT = {
    "fp16": 0.5,
    "fp32": 1.0,
    "fp64": 2.0,
}
PRIORITY_DIMENSIONS = ("precision", "latency", "memory")
RESOURCE_MEMORY_BASE_MB = 16.0
RESOURCE_MEMORY_PER_WORKER_MB = 4.0
RESOURCE_MEMORY_PER_REQUEST_MB = 0.10


@dataclass(frozen=True)
class ResourcePolicy:
    max_float_ops: int | None = None
    max_resident_mb: float | None = None
    priority: tuple[str, ...] = PRIORITY_DIMENSIONS

    def __post_init__(self) -> None:
        if self.max_float_ops is not None and self.max_float_ops <= 0:
            raise corrupt("max_float_ops must be positive")
        if self.max_resident_mb is not None and self.max_resident_mb <= 0:
            raise corrupt("max_resident_mb must be positive")
        if len(self.priority) != 3 or set(self.priority) != set(PRIORITY_DIMENSIONS):
            raise corrupt(
                "resource priority must be a permutation of precision, latency, memory",
                priority=list(self.priority),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_float_ops": self.max_float_ops,
            "max_resident_mb": self.max_resident_mb,
            "priority": list(self.priority),
        }


@dataclass(frozen=True)
class ResourceDecision:
    business_code: str
    original_precision: str
    effective_precision: str
    original_workers: int
    effective_workers: int
    original_batch_size: int
    effective_batch_size: int
    estimated_float_ops: int
    estimated_resident_mb: float
    max_float_ops: int | None
    max_resident_mb: float | None
    latency_units: int
    actions: tuple[str, ...]
    priority: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.business_code,
            "original_precision": self.original_precision,
            "effective_precision": self.effective_precision,
            "original_workers": self.original_workers,
            "effective_workers": self.effective_workers,
            "original_batch_size": self.original_batch_size,
            "effective_batch_size": self.effective_batch_size,
            "estimated_float_ops": self.estimated_float_ops,
            "estimated_resident_mb": self.estimated_resident_mb,
            "max_float_ops": self.max_float_ops,
            "max_resident_mb": self.max_resident_mb,
            "latency_units": self.latency_units,
            "actions": list(self.actions),
            "priority": list(self.priority),
        }


@dataclass(frozen=True)
class ChunkBoundary:
    batch_index: int
    chunk_index: int
    start: int
    end: int
    request_ids: tuple[str, ...]
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "chunk_index": self.chunk_index,
            "start": self.start,
            "end": self.end,
            "request_ids": list(self.request_ids),
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class ScheduleResult:
    schedule_key: str
    business_code: str
    decisions: tuple[dict[str, Any], ...]
    recovered_request_ids: tuple[str, ...]
    recomputed_request_ids: tuple[str, ...]
    merge_order: tuple[str, ...]
    chunk_boundaries: tuple[ChunkBoundary, ...]
    checkpoint_version: int
    checkpoint_checksum: str | None
    resource_decision: ResourceDecision
    effective_config: ThresholdConfig
    actual_weighted_float_ops: int
    cache_statuses: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def decision_bytes(self) -> bytes:
        from .canonical import canonical_bytes

        return canonical_bytes(list(self.decisions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_key": self.schedule_key,
            "business_code": self.business_code,
            "recovered_request_ids": list(self.recovered_request_ids),
            "recomputed_request_ids": list(self.recomputed_request_ids),
            "merge_order": list(self.merge_order),
            "chunk_boundaries": [item.to_dict() for item in self.chunk_boundaries],
            "checkpoint_version": self.checkpoint_version,
            "checkpoint_checksum": self.checkpoint_checksum,
            "resource_decision": self.resource_decision.to_dict(),
            "effective_config": self.effective_config.to_dict(),
            "actual_weighted_float_ops": self.actual_weighted_float_ops,
            "cache_statuses": {
                request_id: dict(status)
                for request_id, status in sorted(self.cache_statuses.items())
            },
            "decision_count": len(self.decisions),
            "business_codes": {
                decision["request_id"]: decision["business_code"]
                for decision in self.decisions
            },
        }


class SimulatedTermination(RuntimeError):
    def __init__(
        self,
        *,
        checkpoint_version: int,
        completed_request_ids: Sequence[str],
        processed_batches: int,
    ):
        self.checkpoint_version = checkpoint_version
        self.completed_request_ids = tuple(completed_request_ids)
        self.processed_batches = processed_batches
        super().__init__(
            "simulated forced termination after committing batch "
            f"{processed_batches} at checkpoint {checkpoint_version}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "SimulatedTermination",
            "checkpoint_version": self.checkpoint_version,
            "completed_request_ids": list(self.completed_request_ids),
            "processed_batches": self.processed_batches,
        }


def _estimate_float_ops(request_count: int, config: ThresholdConfig) -> int:
    # Conservative upper bound; it exceeds the measured contract for the same
    # candidate count, so actual operations are checked again after execution.
    per_request = 1200 + 80 * config.candidate_plan_count
    weighted = per_request * PRECISION_WEIGHT[config.precision]
    return int(math.ceil(request_count * weighted))


def _estimate_latency(request_count: int, workers: int, batch_size: int) -> int:
    if request_count == 0:
        return 0
    batches = math.ceil(request_count / batch_size)
    chunks_per_batch = math.ceil(min(request_count, batch_size) / workers)
    return batches * chunks_per_batch


def _estimate_memory(request_count: int, workers: int, batch_size: int) -> float:
    resident_batch = min(request_count, batch_size)
    return round(
        RESOURCE_MEMORY_BASE_MB
        + RESOURCE_MEMORY_PER_WORKER_MB * workers
        + RESOURCE_MEMORY_PER_REQUEST_MB * resident_batch,
        3,
    )


def adjudicate_resources(
    request_count: int,
    config: ThresholdConfig,
    *,
    requested_workers: int,
    requested_batch_size: int,
    policy: ResourcePolicy,
) -> ResourceDecision:
    if requested_workers < 1 or requested_workers > 64:
        raise corrupt("requested worker count is invalid", workers=requested_workers)
    if requested_batch_size < 1 or requested_batch_size > 256:
        raise corrupt("requested batch size is invalid", batch_size=requested_batch_size)
    original_ops = _estimate_float_ops(request_count, config)
    original_memory = _estimate_memory(request_count, requested_workers, requested_batch_size)
    original_latency = _estimate_latency(
        request_count, requested_workers, requested_batch_size
    )

    def within(ops: int, memory: float) -> bool:
        return (
            (policy.max_float_ops is None or ops <= policy.max_float_ops)
            and (policy.max_resident_mb is None or memory <= policy.max_resident_mb)
        )

    if within(original_ops, original_memory):
        return ResourceDecision(
            business_code=OK,
            original_precision=config.precision,
            effective_precision=config.precision,
            original_workers=requested_workers,
            effective_workers=requested_workers,
            original_batch_size=requested_batch_size,
            effective_batch_size=requested_batch_size,
            estimated_float_ops=original_ops,
            estimated_resident_mb=original_memory,
            max_float_ops=policy.max_float_ops,
            max_resident_mb=policy.max_resident_mb,
            latency_units=original_latency,
            actions=(),
            priority=policy.priority,
        )

    precisions = [
        precision
        for precision in ("fp16", "fp32", "fp64")
        if PRECISION_RANK[precision] <= PRECISION_RANK[config.precision]
    ]
    batch_sizes = sorted(
        {
            1,
            2,
            4,
            8,
            16,
            32,
            64,
            max(1, min(256, requested_batch_size)),
            max(1, min(256, request_count)),
        }
    )
    candidates: list[dict[str, Any]] = []
    for precision in precisions:
        probe_config = config.replace(precision=precision)
        base_ops = _estimate_float_ops(request_count, probe_config)
        for workers in range(1, requested_workers + 1):
            for batch_size in batch_sizes:
                memory = _estimate_memory(request_count, workers, batch_size)
                if not within(base_ops, memory):
                    continue
                candidates.append(
                    {
                        "precision": precision,
                        "precision_rank": PRECISION_RANK[precision],
                        "workers": workers,
                        "batch_size": batch_size,
                        "float_ops": base_ops,
                        "resident_mb": memory,
                        "latency": _estimate_latency(
                            request_count, workers, batch_size
                        ),
                    }
                )
    if not candidates:
        failed = ResourceDecision(
            business_code=E_BUDGET_EXCEEDED,
            original_precision=config.precision,
            effective_precision=config.precision,
            original_workers=requested_workers,
            effective_workers=requested_workers,
            original_batch_size=requested_batch_size,
            effective_batch_size=requested_batch_size,
            estimated_float_ops=original_ops,
            estimated_resident_mb=original_memory,
            max_float_ops=policy.max_float_ops,
            max_resident_mb=policy.max_resident_mb,
            latency_units=original_latency,
            actions=("no_feasible_combination",),
            priority=policy.priority,
        )
        raise WorkbenchError(
            E_BUDGET_EXCEEDED,
            "no precision, worker, and batch combination fits the resource limits",
            {"resource_decision": failed.to_dict()},
        )

    def ranking(candidate: dict[str, Any]) -> tuple[Any, ...]:
        values: list[Any] = []
        for dimension in policy.priority:
            if dimension == "precision":
                values.append(-candidate["precision_rank"])
            elif dimension == "latency":
                values.append(candidate["latency"])
            else:
                values.append(candidate["resident_mb"])
        values.extend(
            [
                candidate["workers"],
                candidate["batch_size"],
                candidate["precision_rank"],
            ]
        )
        return tuple(values)

    selected = sorted(candidates, key=ranking)[0]
    actions: list[str] = []
    if selected["precision"] != config.precision:
        actions.append("reduce_precision")
    if selected["workers"] != requested_workers:
        actions.append("adjust_worker_concurrency")
    if selected["batch_size"] != requested_batch_size:
        actions.append("adjust_batch_boundary")
    return ResourceDecision(
        business_code=E_RESOURCE_ADJUSTED,
        original_precision=config.precision,
        effective_precision=selected["precision"],
        original_workers=requested_workers,
        effective_workers=selected["workers"],
        original_batch_size=requested_batch_size,
        effective_batch_size=selected["batch_size"],
        estimated_float_ops=selected["float_ops"],
        estimated_resident_mb=selected["resident_mb"],
        max_float_ops=policy.max_float_ops,
        max_resident_mb=policy.max_resident_mb,
        latency_units=selected["latency"],
        actions=tuple(actions),
        priority=policy.priority,
    )


def _partition_contiguous(
    requests: Sequence[InferenceRequest],
    parts: int,
) -> list[list[InferenceRequest]]:
    if parts <= 0:
        raise corrupt("partition count must be positive")
    if not requests:
        return []
    parts = min(parts, len(requests))
    quotient, remainder = divmod(len(requests), parts)
    result: list[list[InferenceRequest]] = []
    offset = 0
    for index in range(parts):
        size = quotient + (1 if index < remainder else 0)
        if size:
            result.append(list(requests[offset : offset + size]))
        offset += size
    return result


def _route_worker(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        requests = payload["requests"]
        config = payload["config"]
        cache_root = payload["cache_root"]
        staging_root = payload["staging_root"]
        pipeline = RoutingPipeline(
            TransactionalCache(cache_root, staging_root)
        )
        records = []
        statuses = {}
        for request in requests:
            decision = pipeline.execute(request, config)
            records.append(decision.to_dict())
            statuses[request.request_id] = decision.statuses()
        return {"ok": True, "records": records, "statuses": statuses}
    except WorkbenchError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:  # pragma: no cover - defensive process boundary
        return {
            "ok": False,
            "error": {
                "code": "E_WORKER_FAILURE",
                "message": str(exc),
                "details": {"exception_type": type(exc).__name__},
            },
        }


def _execute_chunks(
    chunks: Sequence[Sequence[InferenceRequest]],
    *,
    config: ThresholdConfig,
    cache_root: Path,
    staging_root: Path,
    workers: int,
    use_processes: bool,
) -> list[dict[str, Any]]:
    payloads = [
        {
            "requests": list(chunk),
            "config": config,
            "cache_root": str(cache_root),
            "staging_root": str(staging_root),
        }
        for chunk in chunks
    ]
    if not payloads:
        return []
    if not use_processes or workers == 1:
        return [_route_worker(payload) for payload in payloads]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_route_worker, payload) for payload in payloads]
        return [future.result() for future in futures]


def run_schedule(
    requests: Sequence[InferenceRequest],
    config: ThresholdConfig,
    cache: ContentAddressedCache,
    checkpoint_store: CheckpointStore,
    *,
    workers: int | None = None,
    batch_size: int = 4,
    resource_policy: ResourcePolicy | None = None,
    use_processes: bool = True,
    stop_after_batches: int | None = None,
) -> ScheduleResult:
    request_ids = [request.request_id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise corrupt("request stream contains duplicate request_id")
    ordered = sorted(requests, key=lambda item: item.request_id)
    requested_workers = workers if workers is not None else config.scheduler_workers
    policy = resource_policy or ResourcePolicy()
    resource = adjudicate_resources(
        len(ordered),
        config,
        requested_workers=requested_workers,
        requested_batch_size=batch_size,
        policy=policy,
    )
    effective_config = config.replace(precision=resource.effective_precision)
    key = schedule_key(ordered, config.fingerprint())
    checkpoint = checkpoint_store.read()
    if checkpoint.schedule_key is not None and checkpoint.schedule_key != key:
        raise WorkbenchError(
            E_CHECKPOINT_CORRUPT,
            "existing checkpoint schedule key does not match the current run",
            {
                "checkpoint_schedule_key": checkpoint.schedule_key,
                "requested_schedule_key": key,
                "checkpoint_version": checkpoint.version,
            },
        )

    initial_completed = set(checkpoint.completed_request_ids)
    completed = set(initial_completed)
    transaction = TransactionalCache(cache.root, cache.root / "uncommitted")
    transaction.discard()
    decisions_by_id: dict[str, dict[str, Any]] = {
        request_id: dict(record)
        for request_id, record in checkpoint.decision_records.items()
    }
    for request_id, record in decisions_by_id.items():
        if record.get("precision") != effective_config.precision:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint decision precision disagrees with effective resource plan",
                {
                    "request_id": request_id,
                    "checkpoint_precision": record.get("precision"),
                    "effective_precision": effective_config.precision,
                },
                first_request_id=request_id,
                first_node="N2_SIMILARITY",
            )

    boundaries: list[ChunkBoundary] = []
    cache_statuses: dict[str, dict[str, str]] = {}
    batch_index = 0
    processed_batches = 0
    offset = 0
    while offset < len(ordered):
        batch = ordered[offset : offset + resource.effective_batch_size]
        batch_ids = {request.request_id for request in batch}
        fully_completed = batch_ids.issubset(completed)
        partially_completed = bool(batch_ids.intersection(completed)) and not fully_completed
        if partially_completed:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint completion splits a deterministic batch",
                {
                    "batch_index": batch_index,
                    "completed": sorted(batch_ids.intersection(completed)),
                    "pending": sorted(batch_ids.difference(completed)),
                },
            )
        if fully_completed:
            for request in batch:
                cache_statuses[request.request_id] = {
                    "N1_FEATURE": "recovered",
                    "N2_SIMILARITY": "recovered",
                    "N3_CANDIDATE": "recovered",
                    "N4_FUSION": "recovered",
                }
            boundaries.append(
                ChunkBoundary(
                    batch_index=batch_index,
                    chunk_index=0,
                    start=offset,
                    end=offset + len(batch),
                    request_ids=tuple(item.request_id for item in batch),
                    skipped=True,
                )
            )
            offset += len(batch)
            batch_index += 1
            continue

        chunks = _partition_contiguous(batch, resource.effective_workers)
        chunk_offset = offset
        for chunk_index, chunk in enumerate(chunks):
            boundaries.append(
                ChunkBoundary(
                    batch_index=batch_index,
                    chunk_index=chunk_index,
                    start=chunk_offset,
                    end=chunk_offset + len(chunk),
                    request_ids=tuple(item.request_id for item in chunk),
                )
            )
            chunk_offset += len(chunk)
        try:
            worker_results = _execute_chunks(
                chunks,
                config=effective_config,
                cache_root=cache.root,
                staging_root=cache.root / "uncommitted",
                workers=resource.effective_workers,
                use_processes=use_processes,
            )
            batch_records: list[dict[str, Any]] = []
            for result in worker_results:
                if not result.get("ok"):
                    error = result.get("error", {})
                    raise WorkbenchError(
                        str(error.get("code", "E_WORKER_FAILURE")),
                        str(error.get("message", "worker failed")),
                        {"worker_error": error},
                    )
                batch_records.extend(result["records"])
                cache_statuses.update(
                    {
                        request_id: dict(status)
                        for request_id, status in result.get("statuses", {}).items()
                    }
                )
            batch_records.sort(key=lambda record: record["request_id"])
            if {record["request_id"] for record in batch_records} != batch_ids:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "worker result set does not match the assigned batch",
                    {
                        "batch_index": batch_index,
                        "expected": sorted(batch_ids),
                        "actual": sorted(
                            record["request_id"] for record in batch_records
                        ),
                    },
                )
            for record in batch_records:
                decisions_by_id[record["request_id"]] = record
                completed.add(record["request_id"])
            weighted_actual = sum(
                int(record["total_operations"])
                * PRECISION_WEIGHT[str(record["precision"])]
                for record in decisions_by_id.values()
            )
            if policy.max_float_ops is not None and weighted_actual > policy.max_float_ops:
                raise WorkbenchError(
                    E_BUDGET_EXCEEDED,
                    "measured weighted float operations exceed the configured budget",
                    {
                        "measured_weighted_float_ops": int(weighted_actual),
                        "max_float_ops": policy.max_float_ops,
                        "batch_index": batch_index,
                    },
                )
            checkpoint_store.commit_batch(
                schedule_key=key,
                completed_request_ids=sorted(completed),
                decision_records=decisions_by_id,
                batch_index=batch_index,
                expected_parent_version=checkpoint.version,
            )
        except Exception:
            transaction.discard()
            raise
        transaction.promote()
        checkpoint = checkpoint_store.read()
        processed_batches += 1
        if stop_after_batches is not None and processed_batches >= stop_after_batches:
            raise SimulatedTermination(
                checkpoint_version=checkpoint.version,
                completed_request_ids=sorted(completed),
                processed_batches=processed_batches,
            )
        offset += len(batch)
        batch_index += 1

    ordered_decisions = tuple(
        decisions_by_id[request_id] for request_id in sorted(decisions_by_id)
    )
    weighted_actual = sum(
        int(record["total_operations"]) * PRECISION_WEIGHT[str(record["precision"])]
        for record in ordered_decisions
    )
    recovered_ids = tuple(sorted(initial_completed.intersection(request_ids)))
    recomputed_ids = tuple(sorted(completed.difference(initial_completed)))
    return ScheduleResult(
        schedule_key=key,
        business_code=resource.business_code,
        decisions=ordered_decisions,
        recovered_request_ids=recovered_ids,
        recomputed_request_ids=recomputed_ids,
        merge_order=tuple(sorted(decisions_by_id)),
        chunk_boundaries=tuple(boundaries),
        checkpoint_version=checkpoint.version,
        checkpoint_checksum=checkpoint.checksum,
        resource_decision=resource,
        effective_config=effective_config,
        actual_weighted_float_ops=int(weighted_actual),
        cache_statuses=cache_statuses,
    )

