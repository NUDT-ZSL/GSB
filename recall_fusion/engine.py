"""Deterministic parallel orchestration for the four-channel recall pipeline.

Dispatch order is fully deterministic:

1. Queries are processed in the order of their query_id.
2. They are sliced into fixed-size batches (the last batch may be shorter).
3. Batches are submitted to the pool in order; results are reassembled in
   query_id order regardless of worker completion timing.

The main process owns the budget and memory ledgers; worker processes are
stateless beyond the one-time DataStore/Config initializer.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from typing import Any

from .budget import BudgetLedger, plan_query
from .config import CHANNEL_ORDER, Config
from .data import DataStore, Query, generate_dataset, write_jsonl
from .memory import MemoryManager
from .recall_stats import (
    accumulate,
    build_fusion_weights_payload,
    build_norm_params_payload,
    build_recall_stats_payload,
    new_channel_accumulator,
)
from .snapshots import SnapshotStore
from .worker import init_worker, process_query


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _dumps_canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Engine:
    def __init__(self, config: Config, output_dir: str | Path):
        self.config = config
        self.output_dir = Path(output_dir)
        self.cache_dir = self.output_dir / "cache"
        self.shard_dir = self.output_dir / "shards"
        self.snapshot_dir = self.output_dir / "snapshots"
        self.sample_dir = self.output_dir / "sample"
        for directory in (
            self.cache_dir,
            self.shard_dir,
            self.snapshot_dir,
            self.sample_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.ledger = BudgetLedger(
            total_candidate_budget=config.total_candidate_budget,
            fusion_compare_budget=config.fusion_compare_budget,
            memory_limit_bytes=config.memory_limit_bytes,
        )
        self.memory = MemoryManager(
            limit_bytes=config.memory_limit_bytes,
            shard_dir=self.shard_dir,
            ledger_path=self.cache_dir / "memory_ledger.jsonl",
        )
        self.snapshots = SnapshotStore(self.snapshot_dir)
        self.plan_ledger: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.channel_stats = new_channel_accumulator()
        self.parallelism_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup
    def prepare_dataset(self) -> tuple[DataStore, list[Query]]:
        docs, queries = generate_dataset(self.config.datagen)
        write_jsonl(
            [doc.__dict__ for doc in docs], self.sample_dir / "documents.jsonl"
        )
        write_jsonl([query.__dict__ for query in queries], self.sample_dir / "queries.jsonl")
        store = DataStore(docs, self.config.datagen, self.config.precision)
        with open(self.sample_dir / "config.json", "w", encoding="utf-8") as handle:
            json.dump(self.config.to_dict(), handle, sort_keys=True, indent=2)
        return store, queries

    # ------------------------------------------------------------- arbitration
    def arbitrate_parallelism(self) -> int:
        """Conflict 3: worker count vs. memory vs. latency."""
        requested = self.config.workers
        order = self.config.policy_priority
        memory_index = order.index("memory")
        latency_index = order.index("latency")
        chosen = requested
        reason = "requested_workers_accepted"
        if memory_index < latency_index:
            # Memory wins over latency: cap concurrent workers so each gets a
            # deterministic slice of the resident-memory budget.
            fair_share = max(1, self.config.memory_limit_bytes // max(1, requested))
            if fair_share < 8 * 1024:
                chosen = max(1, requested // 2)
                reason = "memory_priority_halved_workers"
        event = {
            "requested_workers": requested,
            "effective_workers": chosen,
            "policy_priority": list(order),
            "reason": reason,
        }
        self.parallelism_events.append(event)
        with open(self.cache_dir / "parallelism.json", "w", encoding="utf-8") as handle:
            json.dump(event, handle, sort_keys=True, indent=2)
        return chosen

    # ------------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        store, queries = self.prepare_dataset()
        workers = self.arbitrate_parallelism()

        # Admission/planning is sequential in query order -> deterministic ledger.
        tasks: list[tuple[Query, dict[str, Any]]] = []
        for query in queries:
            plan = plan_query(store, query, self.config, self.ledger)
            self.plan_ledger.append(plan.to_dict())
            if not plan.admitted:
                record = {
                    "query_id": query.query_id,
                    "error_code": plan.error_code,
                    "reason": plan.reason,
                    "plan": plan.to_dict(),
                }
                self.rejected.append(record)
                continue
            tasks.append((query, plan.to_dict()))
            self.ledger.admitted_queries.append(query.query_id)
            self.ledger.used_candidates += plan.estimated_candidates
            self.ledger.used_comparisons += plan.estimated_comparisons

        results_by_id: dict[str, dict[str, Any]] = {}
        batches = _batched(tasks, self.config.batch_size)
        dispatch_log = []
        if workers == 1:
            init_worker(store, self.config)
            for batch_index, batch in enumerate(batches):
                dispatch_log.append(
                    {
                        "batch_index": batch_index,
                        "query_ids": [q.query_id for q, _ in batch],
                        "mode": "in_process",
                    }
                )
                for task in batch:
                    outcome = process_query(task)
                    results_by_id[outcome["query_id"]] = outcome
        else:
            ctx = mp.get_context("spawn")
            with ctx.Pool(
                processes=workers,
                initializer=init_worker,
                initargs=(store, self.config),
            ) as pool:
                ordered_results = []
                for batch_index, batch in enumerate(batches):
                    dispatch_log.append(
                        {
                            "batch_index": batch_index,
                            "query_ids": [q.query_id for q, _ in batch],
                            "mode": "spawn_pool",
                        }
                    )
                    ordered_results.extend(pool.map(process_query, batch))
                for outcome in ordered_results:
                    results_by_id[outcome["query_id"]] = outcome

        # Reassembly in deterministic query_id order and memory eviction.
        ordered_outcomes = [
            results_by_id[query.query_id]
            for query in queries
            if query.query_id in results_by_id
        ]
        actual_candidates = 0
        actual_comparisons = 0
        for outcome in ordered_outcomes:
            if not outcome["ok"]:
                self.failures.append(outcome)
                continue
            actual_candidates += sum(
                len(output["candidates"])
                for output in outcome["channel_outputs"].values()
            )
            actual_comparisons += outcome["comparisons_used"]
            accumulate(
                self.channel_stats,
                outcome["channel_outputs"],
                outcome["plan"]["executed_channels"],
            )
            size_bytes = outcome["plan"]["estimated_bytes"]
            self.ledger.resident_bytes += size_bytes
            self.ledger.peak_resident_bytes = max(
                self.ledger.peak_resident_bytes, self.ledger.resident_bytes
            )
            self.memory.retain(outcome["query_id"], outcome, size_bytes)

        self.memory.drain()
        self.memory.close()

        summary = self._write_artifacts(
            queries=queries,
            ordered_outcomes=ordered_outcomes,
            workers=workers,
            actual_candidates=actual_candidates,
            actual_comparisons=actual_comparisons,
            dispatch_log=dispatch_log,
        )
        return summary

    # -------------------------------------------------------------- artifacts
    def _write_artifacts(
        self,
        queries: list[Query],
        ordered_outcomes: list[dict[str, Any]],
        workers: int,
        actual_candidates: int,
        actual_comparisons: int,
        dispatch_log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        write_jsonl(self.plan_ledger, self.cache_dir / "plan_ledger.jsonl")
        write_jsonl(self.rejected, self.cache_dir / "rejected.jsonl")
        write_jsonl(self.failures, self.cache_dir / "failures.jsonl")
        write_jsonl(dispatch_log, self.cache_dir / "dispatch.jsonl")

        admitted = len(self.ledger.admitted_queries)
        stats_payload = build_recall_stats_payload(
            self.channel_stats,
            query_count=len(queries),
            admitted_count=admitted,
            rejected_count=len(self.rejected),
        )
        norm_payload = build_norm_params_payload(self.config)
        weights_payload = build_fusion_weights_payload(self.config)

        # Whole-snapshot versioned replacement. On reruns into an existing
        # output directory the version must advance monotonically: load the
        # current pointer first, then atomically repoint to the new version.
        snapshot_versions = {}
        for kind, payload in (
            ("recall_stats", stats_payload),
            ("norm_params", norm_payload),
            ("fusion_weights", weights_payload),
        ):
            next_version = (self.snapshots.current_version(kind) or 0) + 1
            self.snapshots.submit(kind, next_version, payload)
            snapshot_versions[kind] = next_version

        results = []
        for outcome in ordered_outcomes:
            results.append(outcome)
        # Include rejected queries in the results bundle as explicit records.
        for record in self.rejected:
            results.append(
                {
                    "query_id": record["query_id"],
                    "ok": False,
                    "status": "rejected",
                    "error": {
                        "code": record["error_code"],
                        "message": record["reason"],
                        "stage": "admission",
                        "field": "",
                    },
                    "plan": record["plan"],
                    "channel_outputs": {
                        channel: {
                            "channel": channel,
                            "requested_limit": record["plan"]["effective_limits"].get(
                                channel, 0
                            ),
                            "truncated": False,
                            "matched_total": 0,
                            "empty_reason": "query_rejected",
                            "candidates": [],
                        }
                        for channel in CHANNEL_ORDER
                    },
                    "fused": [],
                }
            )
        results.sort(key=lambda item: item["query_id"])
        write_jsonl(results, self.output_dir / "results.jsonl")

        # Canonical byte stream for rerun-equality verification.
        canonical = "\n".join(_dumps_canonical(row) for row in results) + "\n"
        results_canonical_path = self.output_dir / "results.canonical.jsonl"
        with open(results_canonical_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical)

        summary = {
            "config": self.config.to_dict(),
            "effective_workers": workers,
            "parallelism_events": self.parallelism_events,
            "queries_total": len(queries),
            "queries_admitted": admitted,
            "queries_rejected": len(self.rejected),
            "queries_failed": len(self.failures),
            "budget": {
                "total_candidate_budget": self.ledger.total_candidate_budget,
                "fusion_compare_budget": self.ledger.fusion_compare_budget,
                "used_candidates_estimated": self.ledger.used_candidates,
                "used_candidates_actual": actual_candidates,
                "used_comparisons_estimated": self.ledger.used_comparisons,
                "used_comparisons_actual": actual_comparisons,
            },
            "memory": self.memory.stats(),
            "snapshots": {
                kind: snapshot_versions[kind]
                for kind in ("recall_stats", "norm_params", "fusion_weights")
            },
            "artifacts": {
                "results": str(self.output_dir / "results.jsonl"),
                "canonical": str(results_canonical_path),
                "plan_ledger": str(self.cache_dir / "plan_ledger.jsonl"),
                "rejected": str(self.cache_dir / "rejected.jsonl"),
                "dispatch": str(self.cache_dir / "dispatch.jsonl"),
                "memory_ledger": str(self.cache_dir / "memory_ledger.jsonl"),
                "snapshot_dir": str(self.snapshot_dir),
                "shard_dir": str(self.shard_dir),
            },
            "recall_stats": stats_payload,
        }
        with open(self.output_dir / "run_summary.json", "w", encoding="utf-8") as handle:
            json.dump(summary, handle, sort_keys=True, indent=2)
        return summary
