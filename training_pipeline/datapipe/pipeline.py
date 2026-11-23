"""End-to-end orchestration: cache warm-up, round planning, run, resume.

Round planning is deterministic: the fixed shard list of every dataset in the
job is walked in order and sliced into rounds of ``prefetch_depth`` shards per
worker (``data_parallel * prefetch_depth`` candidates per round).  This makes
prefetch scheduling fully reproducible while still exercising multiple rounds,
budget rejection, hits and recovery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .assembler import Assembler, Batch
from .cache import ContentCache, evaluate_invalidation
from .clock import LogicalClock
from .injection import InjectionEngine
from .model import Scenario
from .scheduler import (
    OUTCOME_COMPLETED,
    OUTCOME_HIT,
    OUTCOME_SKIPPED_BUDGET,
    RoundReport,
    Scheduler,
)
from .sharding import build_shard_manifest, cache_algebra
from .store import FileLock, Store


def plan_rounds(scenario: Scenario) -> List[Dict[str, List[int]]]:
    per_round = scenario.job.data_parallel * scenario.job.prefetch_depth
    flat: List[Tuple[str, int]] = []
    for dataset in scenario.job.datasets:
        spec = next(item for item in scenario.datasets if item.name == dataset)
        for shard_index in range(spec.shard_count):
            flat.append((dataset, shard_index))
    rounds: List[Dict[str, List[int]]] = []
    for start in range(0, len(flat), per_round):
        needed: Dict[str, List[int]] = {}
        for dataset, shard_index in flat[start:start + per_round]:
            needed.setdefault(dataset, []).append(shard_index)
        rounds.append(needed)
    return rounds


@dataclass
class PipelineResult:
    invalidation: dict
    round_reports: List[RoundReport] = field(default_factory=list)
    batches: List[Batch] = field(default_factory=list)
    clock: Optional[dict] = None
    recovered: bool = False
    recovery_detail: Optional[dict] = None


class Pipeline:
    def __init__(self, work_dir: str, scenario: Scenario):
        self.work_dir = work_dir
        self.scenario = scenario
        os.makedirs(work_dir, exist_ok=True)
        self.cache_dir = os.path.join(work_dir, "cache")
        self.db_path = os.path.join(work_dir, "pipeline.sqlite3")
        self.lock_path = os.path.join(work_dir, ".pipeline.lock")
        self.store = Store(self.db_path)
        self.content_cache = ContentCache(self.cache_dir)
        self.clock = LogicalClock(scenario.clock_steps, scenario.clock_rollback_tolerance_ms)
        self.injector = InjectionEngine(
            scenario.injections, marker_path=os.path.join(work_dir, ".crash-markers.json")
        )
        self.scheduler = Scheduler(
            scenario, self.store, self.content_cache, self.clock, self.injector, self.lock_path
        )

    def close(self) -> None:
        self.store.close()

    def round_offset(self) -> int:
        owned = ":".join(self.scenario.job.datasets)
        isolated = self.store.get_kv(f"round_namespace:{owned}")
        if isolated is None:
            existing = self.store.get_kv("round_namespace_counter", 0)
            isolated = existing + 1
            self.store.set_kv("round_namespace_counter", isolated)
            self.store.set_kv(f"round_namespace:{owned}", isolated)
        return isolated * 100000

    # ------------------------------------------------------------------
    def warm_cache_from_view(self) -> dict:
        """Materialise the supplied cache view and compute precise invalidation."""
        specs = {spec.name: spec for spec in self.scenario.datasets}
        report = evaluate_invalidation(specs, self.scenario.cache_view)
        warmed = 0
        for entry in self.scenario.cache_view:
            if (f"{entry.dataset}#s{entry.shard_index:06d}") in report.invalidated:
                continue
            spec = specs[entry.dataset]
            manifest = build_shard_manifest(spec, entry.shard_index)
            if not self.content_cache.payload_exists(manifest):
                self.content_cache.write_atomic(manifest)
            self.store.upsert_cache_entry(
                entry.dataset,
                entry.shard_index,
                entry.algebra,
                manifest.cache_identity,
                manifest.digest,
                manifest.byte_size,
                entry.last_used_clock,
            )
            self.store.commit_shard(
                round_index=-1,
                dataset=entry.dataset,
                shard_index=entry.shard_index,
                algebra=entry.algebra,
                cache_identity=manifest.cache_identity,
                digest=manifest.digest,
                byte_size=manifest.byte_size,
                outcome=OUTCOME_HIT,
            )
            warmed += 1
        return {
            "trigger": report.trigger,
            "invalidated": report.invalidated,
            "retained": report.retained,
            "warmed_entries": warmed,
        }

    # ------------------------------------------------------------------
    def _resume_state(self, needed: Dict[str, List[int]], round_index: int) -> dict:
        """Reconcile an open round's committed vs half-committed shards."""
        detail = {"open_round": round_index, "replayed_missing": [], "already_committed": []}
        for dataset, shard_indexes in needed.items():
            spec = next(item for item in self.scenario.datasets if item.name == dataset)
            algebra = cache_algebra(spec)
            for shard_index in shard_indexes:
                key = f"{dataset}#s{shard_index:06d}"
                if self.store.shard_committed(dataset, shard_index, algebra) is not None:
                    detail["already_committed"].append(key)
                else:
                    detail["replayed_missing"].append(key)
        detail["already_committed"].sort()
        detail["replayed_missing"].sort()
        return detail

    def run(self, assemble: bool = True) -> PipelineResult:
        with FileLock(self.lock_path):
            result = PipelineResult(invalidation=self.warm_cache_from_view())
            rounds = plan_rounds(self.scenario)
            # Isolated, deterministic round index space per owned dataset set so
            # parallel instances never collide on the shared round table while
            # still coordinating on individual shards via their primary keys.
            offset = self.round_offset()

            start_round = 0
            open_row = self.store.get_open_round()
            if open_row is not None and self._round_owned_here(open_row["round_index"], offset, len(rounds)):
                start_round = open_row["round_index"] - offset
                result.recovered = True
                result.recovery_detail = self._resume_state(rounds[start_round], open_row["round_index"])

            for local_index in range(start_round, len(rounds)):
                round_index = offset + local_index
                report = self.scheduler.run_round(round_index, rounds[local_index])
                result.round_reports.append(report)
                hard = [r for r in report.results if r.outcome in ("failed", "timeout")]
                if hard:
                    # Do not assemble; leave durable state for resume / diagnosis.
                    result.clock = self.clock.snapshot()
                    return result
                abandoned = [r for r in report.results if r.outcome == OUTCOME_SKIPPED_BUDGET]
                if abandoned and not assemble:
                    # schedule-only run: surface budget abandons without assembly.
                    result.clock = self.clock.snapshot()
                    return result

            if assemble:
                assembler = Assembler(self.scenario, self.store, self.injector)
                result.batches = assembler.assemble()
            self.store.set_watermark("logical_clock", self.clock.snapshot())
            result.clock = self.clock.snapshot()
            return result

    def _round_owned_here(self, round_index: int, offset: int, round_count: int) -> bool:
        return offset <= round_index < offset + round_count

    # ------------------------------------------------------------------
    def status(self) -> dict:
        offset = self.round_offset()
        total_local = len(plan_rounds(self.scenario))
        all_rounds = self.store.committed_rounds()
        rounds = [row for row in all_rounds if offset <= row["round_index"] < offset + total_local]
        shard_rows = self.store.committed_shard_rows()
        cache_rows = self.store.cache_rows()
        memory = self.store.total_memory_charged()
        faults = self.store.fault_rows()
        abandoned = 0
        for round_index in range(len(plan_rounds(self.scenario))):
            snapshot = self.store.get_kv(f"snapshot:round:{round_index}")
            if snapshot:
                abandoned += len(snapshot.get("abandoned", []))
        return {
            "job": self.scenario.job.name,
            "prefetch_watermark": {
                "committed_rounds": len(rounds),
                "total_rounds": total_local,
                "open_round": self._open_round_index(),
            },
            "cache_hits": sum(1 for row in shard_rows if row["outcome"] == OUTCOME_HIT),
            "committed_shards": len(shard_rows),
            "cache_entries": len(cache_rows),
            "abandoned_shards": abandoned,
            "budget": {
                "limit_bytes": self.scenario.job.memory_budget_bytes,
                "used_bytes": memory,
                "used_fraction": round(memory / self.scenario.job.memory_budget_bytes, 6),
            },
            "recorded_batches": len(self.store.batch_rows()),
            "faults": [
                {
                    "stage": row["stage"],
                    "target": row["target"],
                    "kind": row["kind"],
                    "code": row["code"],
                }
                for row in faults
            ],
            "clock": self.store.get_watermark("logical_clock", self.clock.snapshot()),
        }

    def _open_round_index(self):
        row = self.store.get_open_round()
        return None if row is None else row["round_index"]
