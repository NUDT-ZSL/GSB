"""Parallel prefetch scheduling under memory and logical-time budgets.

Within one round, candidate shards are grouped by dataset and processed with a
per-dataset concurrency cap using a thread pool.  Each candidate first reserves
its estimated bytes against the global memory budget.  Three distinct endings
are recorded:

* ``completed`` / ``hit``      - started and finished successfully,
* ``failed`` / ``timeout``     - started but did not finish successfully,
* ``skipped_budget``           - never started because the budget was exhausted.

Once the budget can no longer cover a candidate, every later candidate in the
same round is abandoned with an explicit reason; already started work always
runs to completion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cache import ContentCache
from .clock import LogicalClock
from .errors import E_PREFETCH_TIMEOUT, PipelineError
from .injection import ForcedCrash, InjectionEngine
from .model import DatasetSpec, Scenario
from .sharding import ShardManifest, build_shard_manifest, cache_algebra, shard_key
from .store import Store

OUTCOME_HIT = "hit"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_SKIPPED_BUDGET = "skipped_budget"


@dataclass
class Candidate:
    dataset: str
    shard_index: int
    estimated_bytes: int
    algebra: int

    @property
    def key(self) -> str:
        return shard_key(self.dataset, self.shard_index)


@dataclass
class CandidateResult:
    candidate: Candidate
    outcome: str
    started: bool
    reason: str
    manifest: Optional[ShardManifest] = None
    actual_bytes: int = 0
    cache_hit: bool = False
    error_code: Optional[str] = None


@dataclass
class RoundReport:
    round_index: int
    started_clock_ms: int
    ended_clock_ms: int
    results: List[CandidateResult] = field(default_factory=list)
    budget_bytes: int = 0
    budget_used_bytes: int = 0
    timed_out: bool = False

    def counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts


class Scheduler:
    def __init__(
        self,
        scenario: Scenario,
        store: Store,
        content_cache: ContentCache,
        clock: LogicalClock,
        injector: InjectionEngine,
        lock_path: str,
    ):
        self.scenario = scenario
        self.store = store
        self.content_cache = content_cache
        self.clock = clock
        self.injector = injector
        self.lock_path = lock_path
        self.specs: Dict[str, DatasetSpec] = {spec.name: spec for spec in scenario.datasets}

    # ------------------------------------------------------------------
    def _build_candidates(self, round_index: int, needed: Dict[str, List[int]]) -> List[Candidate]:
        """Flatten needed shards into a deterministic, dataset-grouped order."""
        candidates: List[Candidate] = []
        for dataset in self.scenario.job.datasets:
            for shard_index in sorted(needed.get(dataset, [])):
                spec = self.specs[dataset]
                manifest = build_shard_manifest(spec, shard_index)
                candidates.append(
                    Candidate(
                        dataset=dataset,
                        shard_index=shard_index,
                        estimated_bytes=manifest.byte_size,
                        algebra=cache_algebra(spec),
                    )
                )
        return candidates

    def _reserve_budget(self, candidates: List[Candidate], already_used: int) -> Tuple[List[Candidate], List[CandidateResult]]:
        """Single-threaded deterministic reservation against the global budget."""
        accepted: List[Candidate] = []
        abandoned: List[CandidateResult] = []
        used = already_used
        budget = self.scenario.job.memory_budget_bytes
        budget_open = True
        for candidate in candidates:
            if budget_open and used + candidate.estimated_bytes <= budget:
                accepted.append(candidate)
                used += candidate.estimated_bytes
            else:
                budget_open = False
                abandoned.append(
                    CandidateResult(
                        candidate=candidate,
                        outcome=OUTCOME_SKIPPED_BUDGET,
                        started=False,
                        reason=(
                            "global memory budget exhausted"
                            if used + candidate.estimated_bytes > budget
                            else "abandoned after prior budget exhaustion"
                        ),
                    )
                )
        return accepted, abandoned

    # ------------------------------------------------------------------
    def _run_one(self, candidate: Candidate, round_index: int, round_start_ms: int) -> CandidateResult:
        spec = self.specs[candidate.dataset]
        target = candidate.key
        # Prefetch-stage faults (failure / timeout / clock rollback) are armed
        # before the clock step so they can interrupt scheduling deterministically.
        self.injector.check("prefetch", target)
        # Every started shard consumes an injected clock step (with real wait).
        self.clock.advance(reason=f"prefetch:{target}")
        elapsed = self.clock.now_ms - round_start_ms
        if elapsed > self.scenario.job.round_time_budget_ms:
            return CandidateResult(
                candidate=candidate,
                outcome=OUTCOME_TIMEOUT,
                started=True,
                reason="logical round time budget exceeded",
                error_code=E_PREFETCH_TIMEOUT,
            )

        # Cache hit path: authoritative SQLite entry + payload file present.
        existing = self.store.shard_committed(candidate.dataset, candidate.shard_index, candidate.algebra)
        if existing is not None:
            manifest = build_shard_manifest(spec, candidate.shard_index)
            if self.content_cache.payload_exists(manifest):
                self.store.upsert_cache_entry(
                    candidate.dataset,
                    candidate.shard_index,
                    candidate.algebra,
                    manifest.cache_identity,
                    manifest.digest,
                    manifest.byte_size,
                    self.clock.now_ms,
                )
                return CandidateResult(
                    candidate=candidate,
                    outcome=OUTCOME_HIT,
                    started=True,
                    reason="cache hit",
                    manifest=manifest,
                    actual_bytes=manifest.byte_size,
                    cache_hit=True,
                )

        # Miss path: injected generation fault, deterministic generation, then
        # atomic cache write and durable registration.
        self.injector.check("generate", target)
        manifest = build_shard_manifest(spec, candidate.shard_index)

        self.injector.check("cache", target)
        inserted_cache = self.store.upsert_cache_entry(
            candidate.dataset,
            candidate.shard_index,
            candidate.algebra,
            manifest.cache_identity,
            manifest.digest,
            manifest.byte_size,
            self.clock.now_ms,
        )
        if inserted_cache:
            self.content_cache.write_atomic(manifest)

        inserted_shard = self.store.commit_shard(
            round_index,
            candidate.dataset,
            candidate.shard_index,
            candidate.algebra,
            manifest.cache_identity,
            manifest.digest,
            manifest.byte_size,
            OUTCOME_COMPLETED,
        )
        if inserted_shard:
            self.store.add_memory_charge(
                round_index,
                candidate.dataset,
                candidate.shard_index,
                candidate.algebra,
                candidate.estimated_bytes,
            )
        return CandidateResult(
            candidate=candidate,
            outcome=OUTCOME_COMPLETED,
            started=True,
            reason="generated and committed" if inserted_shard else "idempotent replay",
            manifest=manifest,
            actual_bytes=manifest.byte_size,
        )

    def _run_group(
        self, dataset: str, group: List[Candidate], round_index: int, round_start_ms: int
    ) -> List[CandidateResult]:
        spec = self.specs[dataset]
        results: List[CandidateResult] = []
        max_workers = max(1, min(spec.max_concurrent_prefetch, len(group)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"pf-{dataset}") as pool:
            futures = {pool.submit(self._safe_run, candidate, round_index, round_start_ms): candidate
                       for candidate in group}
            done, _ = wait(futures)
            for future in done:
                results.append(future.result())
        results.sort(key=lambda result: result.candidate.shard_index)
        return results

    def _safe_run(self, candidate: Candidate, round_index: int, round_start_ms: int) -> CandidateResult:
        try:
            return self._run_one(candidate, round_index, round_start_ms)
        except ForcedCrash:
            raise
        except PipelineError as exc:
            self.store.log_fault(
                round_index,
                "prefetch",
                candidate.key,
                exc.diagnostics.get("kind", "prefetch_failure"),
                exc.code,
                exc.diagnostics,
            )
            return CandidateResult(
                candidate=candidate,
                outcome=OUTCOME_FAILED,
                started=True,
                reason=exc.message,
                error_code=exc.code,
            )

    # ------------------------------------------------------------------
    def run_round(self, round_index: int, needed: Dict[str, List[int]]) -> RoundReport:
        start_clock = self.clock.now_ms
        self.store.open_round(round_index, start_clock)
        self.store.set_watermark("prefetch_round", round_index)

        candidates = self._build_candidates(round_index, needed)
        already_used = self.store.total_memory_charged()
        accepted, abandoned = self._reserve_budget(candidates, already_used)

        grouped: Dict[str, List[Candidate]] = {}
        for candidate in accepted:
            grouped.setdefault(candidate.dataset, []).append(candidate)

        results: List[CandidateResult] = list(abandoned)
        # Dataset groups run concurrently; each group additionally honours its
        # own per-dataset prefetch concurrency cap.
        group_items = [(dataset, group) for dataset, group in grouped.items()]
        if group_items:
            max_group_workers = min(len(group_items), max(1, self.scenario.job.data_parallel))
            with ThreadPoolExecutor(max_workers=max_group_workers, thread_name_prefix="pf-group") as pool:
                futures = {
                    pool.submit(self._run_group, dataset, group, round_index, start_clock): dataset
                    for dataset, group in group_items
                }
                done, _ = wait(futures)
                for future in done:
                    results.extend(future.result())

        results.sort(key=lambda result: (result.candidate.dataset, result.candidate.shard_index))
        timed_out = any(result.outcome == OUTCOME_TIMEOUT for result in results)
        used_after = self.store.total_memory_charged()
        report = RoundReport(
            round_index=round_index,
            started_clock_ms=start_clock,
            ended_clock_ms=self.clock.now_ms,
            results=results,
            budget_bytes=self.scenario.job.memory_budget_bytes,
            budget_used_bytes=used_after,
            timed_out=timed_out,
        )

        # Round commit only happens when no started shard failed/timed out; the
        # snapshot pointer is replaced atomically inside the same transaction.
        hard_failures = [r for r in results if r.outcome in (OUTCOME_FAILED, OUTCOME_TIMEOUT)]
        if not hard_failures:
            snapshot = {
                "round_index": round_index,
                "clock_now_ms": self.clock.now_ms,
                "committed": [r.candidate.key for r in results if r.outcome in (OUTCOME_HIT, OUTCOME_COMPLETED)],
                "abandoned": [r.candidate.key for r in results if r.outcome == OUTCOME_SKIPPED_BUDGET],
                "memory_used_bytes": used_after,
            }
            self.store.commit_round(round_index, self.clock.now_ms, f"snapshot-round-{round_index}.json")
            self.store.set_kv(f"snapshot:round:{round_index}", snapshot)
        return report
