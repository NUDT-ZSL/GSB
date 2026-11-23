"""Deterministic parallel scheduler, budget admission, eviction and reporting."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from .budget import AdmissionController, ArbitrationRecord
from .config import FusionConfig
from .errors import ErrorCode
from .memory import EvictionEvent, ResidentMemoryManager
from .models import Document, Query, QueryResult
from .worker import deserialize_result, process_batch


@dataclass
class SchedulingReport:
    batches: list[list[str]]
    workers: int
    batch_size: int
    admitted_query_ids: list[str]
    rejected_query_ids: list[str]
    rejections: list[dict]
    completed_query_ids: list[str]
    worker_errors: list[dict]
    eviction_events: list[dict]
    arbitrations: list[dict]
    budget_remaining: dict[str, int]
    stats: dict
    resident_peak_bytes: int
    memory_limit_bytes: int
    execution_order: list[str]

    def to_dict(self) -> dict:
        return {
            "batches": self.batches,
            "workers": self.workers,
            "batch_size": self.batch_size,
            "admitted_query_ids": self.admitted_query_ids,
            "rejected_query_ids": self.rejected_query_ids,
            "rejections": self.rejections,
            "completed_query_ids": self.completed_query_ids,
            "worker_errors": self.worker_errors,
            "eviction_events": self.eviction_events,
            "arbitrations": self.arbitrations,
            "budget_remaining": self.budget_remaining,
            "stats": self.stats,
            "resident_peak_bytes": self.resident_peak_bytes,
            "memory_limit_bytes": self.memory_limit_bytes,
            "execution_order": self.execution_order,
        }


class PipelineScheduler:
    def __init__(
        self,
        documents: list[Document],
        config: FusionConfig,
        in_process: bool = False,
    ):
        self.documents = documents
        self.config = config
        self.in_process = in_process
        self.results: dict[str, QueryResult] = {}

    def _chunk(self, queries: list[Query]) -> list[list[Query]]:
        size = self.config.scheduler.batch_size
        return [queries[i : i + size] for i in range(0, len(queries), size)]

    def _preflight_memory_arbitration(
        self,
        batch: list[Query],
        assignments: list[dict],
        batch_id: int,
        remaining_recall: int,
        remaining_fusion: int,
    ) -> tuple[list[ArbitrationRecord], list[dict]]:
        """Rule 7: deterministic arbitration for precision/memory/latency conflicts."""
        records: list[ArbitrationRecord] = []
        assignments = list(assignments)
        priority = self.config.scheduler.conflict_priority

        # Conflict B: a single shard larger than the cap cannot be admitted even
        # after evicting every other shard; shrink such shards out deterministically.
        # Cumulative batch pressure below this threshold is handled at runtime by
        # shard-LRU eviction, so no admitted query is dropped.
        limit = self.config.budgets.resident_memory_bytes
        oversized = [
            item
            for item in assignments
            if self._estimate_shard_bytes(item) > limit
        ]
        if oversized:
            kept = [
                item
                for item in assignments
                if self._estimate_shard_bytes(item) <= limit
            ]
            if kept != assignments:
                records.append(
                    ArbitrationRecord(
                        trigger="batch_memory_conflict",
                        basis=(
                            f"{len(oversized)} shard(s) exceed limit {limit}B; "
                            f"priority={'>'.join(priority)}; drop oversized shards, "
                            "runtime LRU handles cumulative pressure"
                        ),
                        sacrificed_metric="precision",
                        chosen_batch_size=len(kept),
                        detail={
                            "batch_id": batch_id,
                            "original_size": len(assignments),
                            "kept_query_ids": [
                                item["query"]["query_id"] for item in kept
                            ],
                        },
                    )
                )
                assignments = kept

        # Conflict C: parallelism saves latency but multiplies the peak shard
        # footprint across concurrently resident worker shards.
        max_shard = max(
            (self._estimate_shard_bytes(item) for item in assignments),
            default=0,
        )
        parallel_peak = max_shard * self.config.scheduler.workers
        if parallel_peak > limit and self.config.scheduler.workers > 1:
            if priority.index("memory") < priority.index("latency"):
                chosen_workers = 1
                sacrificed = "latency"
            else:
                chosen_workers = self.config.scheduler.workers
                sacrificed = "memory"
            records.append(
                ArbitrationRecord(
                    trigger="memory_parallelism_conflict",
                    basis=(
                        f"parallel peak {parallel_peak}B > limit {limit}B; "
                        f"priority={'>'.join(priority)}"
                    ),
                    sacrificed_metric=sacrificed,
                    chosen_batch_size=len(batch),
                    detail={
                        "batch_id": batch_id,
                        "estimated_peak_bytes": parallel_peak,
                        "limit_bytes": limit,
                        "effective_workers": chosen_workers,
                    },
                )
            )
        return records, assignments

    @staticmethod
    def _estimate_batch_bytes(assignments: list[dict]) -> int:
        # Realistic deterministic estimate: per candidate trace overhead plus
        # fixed per-query shard bookkeeping, calibrated against the object model.
        total_candidates = sum(
            sum(assignment["caps"].values()) for assignment in assignments
        )
        return total_candidates * 260 + len(assignments) * 2048

    @staticmethod
    def _estimate_shard_bytes(assignment: dict) -> int:
        candidates = sum(assignment["caps"].values())
        return candidates * 260 + 2048

    def run(self, queries: list[Query]) -> SchedulingReport:
        # Deterministic global ordering makes chunking and admission reproducible.
        ordered = sorted(queries, key=lambda q: q.query_id)
        chunks = self._chunk(ordered)

        admission = AdmissionController(self.documents, self.config)
        memory = ResidentMemoryManager(
            self.config.budgets.resident_memory_bytes,
            self.config.scheduler.eviction_policy,
        )
        all_arbitrations: list[ArbitrationRecord] = []
        worker_errors: list[dict] = []
        eviction_events: list[EvictionEvent] = []
        batch_layout: list[list[str]] = []
        execution_order: list[str] = []
        effective_workers = self.config.scheduler.workers

        for batch_id, batch in enumerate(chunks):
            assignments = []
            batch_query_ids = []
            two_stage_fallback_ids = set()
            for query in batch:
                decision = admission.admit(query, len(batch))
                if decision.admitted:
                    assignments.append(
                        {
                            "query": query.__dict__.copy(),
                            "caps": dict(decision.caps),
                            "two_stage_fallback": decision.two_stage_fallback,
                        }
                    )
                    batch_query_ids.append(query.query_id)
                    if decision.two_stage_fallback:
                        two_stage_fallback_ids.add(query.query_id)
            batch_layout.append(batch_query_ids)
            if not assignments:
                continue

            preflight, assignments = self._preflight_memory_arbitration(
                batch,
                assignments,
                batch_id,
                admission.remaining_recall,
                admission.remaining_fusion,
            )
            all_arbitrations.extend(preflight)
            kept_ids = {item["query"]["query_id"] for item in assignments}
            admitted_ids = {query_id for query_id in batch_query_ids}
            for dropped_id in admitted_ids - kept_ids:
                decision = next(
                    record
                    for record in admission.records
                    if record.query_id == dropped_id and record.admitted
                )
                admission.remaining_recall += decision.recall_cost
                admission.remaining_fusion += decision.fusion_cost
                decision.admitted = False
                decision.rejection_code = ErrorCode.E_RESOURCE_LIMIT
                decision.rejection_reason = (
                    "deferred by batch-memory arbitration (batch shrink)"
                )
                admission.rejected.append(decision)
            if any(
                arb.trigger == "memory_parallelism_conflict"
                and arb.detail["effective_workers"] == 1
                for arb in all_arbitrations
            ):
                effective_workers = 1
            two_stage_override = bool(two_stage_fallback_ids)

            payloads = self._dispatch(
                batch_id, assignments, effective_workers, two_stage_override
            )
            for payload in payloads:
                worker_errors.extend(payload["errors"])
                for row in payload["results"]:
                    result = deserialize_result(row)
                    execution_order.append(result.query_id)
                    events = memory.register(result)
                    for event in events:
                        eviction_events.append(event)
                        if event.compacted_results:
                            self.results.update(event.compacted_results)
                    self._store_result(result, memory)

        compacted = memory.compact_all()
        for query_id, compact in compacted.items():
            if query_id not in self.results:
                self.results[query_id] = compact

        stats = self._aggregate_stats(admission)
        all_arbitrations = list(admission.arbitrations) + all_arbitrations
        return SchedulingReport(
            batches=batch_layout,
            workers=self.config.scheduler.workers,
            batch_size=self.config.scheduler.batch_size,
            admitted_query_ids=[
                record.query_id
                for record in admission.records
                if record.admitted
            ],
            rejected_query_ids=[record.query_id for record in admission.rejected],
            rejections=admission.rejection_report(),
            completed_query_ids=sorted(self.results),
            worker_errors=worker_errors,
            eviction_events=[
                {
                    key: value
                    for key, value in event.__dict__.items()
                    if key != "compacted_results"
                }
                for event in eviction_events
            ],
            arbitrations=[arb.__dict__ for arb in all_arbitrations],
            budget_remaining={
                "total_candidates": admission.remaining_recall,
                "fusion_comparisons": admission.remaining_fusion,
            },
            stats=stats,
            resident_peak_bytes=memory.peak_bytes,
            memory_limit_bytes=memory.limit_bytes,
            execution_order=execution_order,
        )

    def _store_result(
        self, result: QueryResult, memory: ResidentMemoryManager
    ) -> None:
        # Evicted shards are replaced by their compact form lazily on eviction;
        # here the full result is retained while it remains a live shard.
        self.results[result.query_id] = result

    def _dispatch(
        self,
        batch_id: int,
        assignments: list[dict],
        workers: int,
        two_stage_override: bool = False,
    ) -> list[dict]:
        config_dict = self.config.to_dict()
        if two_stage_override:
            config_dict["scheduler"]["two_stage_enabled"] = False
        if self.in_process or workers <= 1:
            return [
                process_batch(self.documents, config_dict, batch_id, assignments)
            ]
        # Deterministic dispatch: chunks keep their batch_id and are submitted in
        # order; map preserves submission order when gathering results.
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    process_batch,
                    self.documents,
                    config_dict,
                    batch_id,
                    assignments,
                )
            ]
            return [future.result() for future in futures]

    def _aggregate_stats(self, admission: AdmissionController) -> dict:
        per_channel_hits = {channel: 0 for channel in self.config.enabled_channels()}
        total_fused = 0
        per_query_rows = []
        for query_id in sorted(self.results):
            result = self.results[query_id]
            total_fused += len(result.fused)
            for channel in per_channel_hits:
                per_channel_hits[channel] += sum(
                    1
                    for candidate in result.fused
                    if any(hit.channel == channel for hit in candidate.all_hits)
                )
            per_query_rows.append(
                {
                    "query_id": query_id,
                    "fused_count": len(result.fused),
                    "recall_cost": result.cost_recall,
                    "fusion_cost": result.cost_fusion,
                }
            )
        return {
            "completed": len(self.results),
            "rejected": len(admission.rejected),
            "per_channel_kept_hits": per_channel_hits,
            "total_fused_candidates": total_fused,
            "per_query": per_query_rows,
        }
