"""Public batch/query facade with atomic snapshots and exact query caching."""

from __future__ import annotations

import copy
import dataclasses
import math
import multiprocessing
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Sequence

from .cache import CacheEntry, CacheStore
from .config import EvaluatorConfig, validate_precision
from .errors import (
    DomainError,
    E_DUPLICATE_SAMPLE_ID,
    E_INVALID_CONFIG,
    E_INVALID_LATENCY_BUDGET,
    E_INVALID_SAMPLE,
    E_LATENCY_BUDGET,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_UNATTRIBUTED,
)
from .invalidation import InvalidationEvent
from .pipeline import (
    STAGE_ORDER,
    analyze_text,
    compute_cache_key,
    estimate_latency,
    evaluate_worker,
    run_pipeline,
)
from .samples import EvaluationSample
from .snapshot import EvaluationSnapshot, SnapshotStore, make_snapshot
from .util import deep_copy_json, quantize
from .validation import validate_sample_record


def _error_response(
    error: DomainError,
    *,
    sample_id: str | None = None,
    snapshot: EvaluationSnapshot | None = None,
    precision: str | None = None,
    estimate: dict[str, Any] | None = None,
    budget_ms: float | None = None,
    measured_ns: int = 0,
    completed_stages: Iterable[str] = (),
    cache_state: str = "not_checked",
) -> dict[str, Any]:
    completed = list(completed_stages or error.completed_stages)
    measured_ms = round(measured_ns / 1_000_000.0, 6)
    latency = {
        "observed_by": "local_evaluation_loop",
        "measured_ms": measured_ms,
        "budget_ms": budget_ms,
        "within_budget": (
            measured_ns <= budget_ms * 1_000_000.0 if budget_ms is not None else None
        ),
        "completed_stages": completed,
        "estimate": estimate,
        "degradation_category": error.degradation_category,
    }
    return {
        "schema_version": "igeval.query/1",
        "status": "error",
        "code": error.code,
        "sample_id": sample_id if sample_id is not None else error.sample_id,
        "snapshot": snapshot.summary() if snapshot is not None else None,
        "precision": precision,
        "cache": {
            "status": cache_state,
            "key": None,
            "feature_digest": None,
            "score_digest": None,
        },
        "latency": latency,
        "stages": {},
        "final_score": None,
        "errors": [error.to_dict()],
        "degradation": (
            [
                {
                    "category": error.degradation_category,
                    "code": error.code,
                }
            ]
            if error.degradation_category
            else []
        ),
    }


class QualityEvaluator:
    """Integrated batch and point-query evaluator.

    A query captures one immutable sample revision and one immutable snapshot,
    then uses exactly the same ``run_pipeline`` implementation as a batch
    worker. Timing and cache metadata never enter score digests.
    """

    def __init__(
        self,
        samples: Sequence[EvaluationSample],
        *,
        config: EvaluatorConfig | None = None,
        snapshot: EvaluationSnapshot | dict[str, Any] | None = None,
        runtime_dir: str | Path = "artifacts/runtime",
        use_persisted_snapshot: bool = True,
    ) -> None:
        self.config = config or EvaluatorConfig()
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_store = SnapshotStore(self.runtime_dir)
        self.cache = CacheStore(
            self.runtime_dir,
            max_entries=self.config.max_cache_entries,
            max_memory_bytes=self.config.max_feature_memory_bytes,
        )
        self._state_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._events_lock = threading.RLock()
        self._samples: dict[str, EvaluationSample] = {}
        self._sample_revisions: dict[str, int] = {}
        self._sample_cache_keys: dict[str, set[str]] = {}
        self._duplicate_ids: set[str] = set()
        self._events: list[InvalidationEvent] = []

        counts: dict[str, int] = {}
        for sample in samples:
            sample_id = sample.sample_id
            counts[sample_id] = counts.get(sample_id, 0) + 1
            if sample_id not in self._samples:
                self._samples[sample_id] = sample
                self._sample_revisions[sample_id] = 1
        self._duplicate_ids = {sample_id for sample_id, count in counts.items() if count > 1}

        persisted_snapshot = self.snapshot_store.load_current()
        if isinstance(snapshot, dict):
            initial_snapshot = EvaluationSnapshot.from_dict(snapshot)
        elif isinstance(snapshot, EvaluationSnapshot):
            initial_snapshot = EvaluationSnapshot.from_dict(snapshot.to_dict())
        elif use_persisted_snapshot and persisted_snapshot is not None:
            initial_snapshot = persisted_snapshot
        else:
            initial_snapshot = make_snapshot()
        self._snapshot = initial_snapshot
        if persisted_snapshot is None:
            self.snapshot_store.set_current(initial_snapshot)
        elif (
            initial_snapshot.snapshot_version <= persisted_snapshot.snapshot_version
            and initial_snapshot.snapshot_id != persisted_snapshot.snapshot_id
        ):
            raise DomainError(
                E_SNAPSHOT_ROLLBACK,
                "initial snapshot would replace a different persisted snapshot at the same or older version",
                field="snapshot_version",
                category="snapshot",
                details={
                    "persisted": persisted_snapshot.snapshot_version,
                    "candidate": initial_snapshot.snapshot_version,
                },
            )
        elif initial_snapshot.snapshot_version > persisted_snapshot.snapshot_version:
            self.snapshot_store.set_current(initial_snapshot)

    def _duplicate_errors(self, ids: Iterable[str]) -> list[DomainError]:
        return [
            DomainError(
                E_DUPLICATE_SAMPLE_ID,
                f"duplicate sample id: {sample_id}",
                field="sample_id",
                sample_id=sample_id,
                stage="validation",
            )
            for sample_id in sorted(set(ids))
        ]

    def _capture(self, sample_id: str) -> tuple[EvaluationSample | None, int, EvaluationSnapshot]:
        with self._state_lock:
            sample = self._samples.get(sample_id)
            revision = self._sample_revisions.get(sample_id, 0)
            snapshot = self._snapshot
            return sample, revision, snapshot

    def _all_sample_ids(self) -> list[str]:
        with self._state_lock:
            return sorted(self._samples)

    def _record_event(
        self,
        kind: str,
        affected_sample_ids: Iterable[str],
        invalidated_cache_keys: Iterable[str],
        invalidated_sample_ids: Iterable[str],
        details: dict[str, Any] | None = None,
    ) -> InvalidationEvent:
        with self._events_lock:
            event = InvalidationEvent(
                event_id=len(self._events) + 1,
                kind=kind,
                affected_sample_ids=tuple(sorted(set(affected_sample_ids))),
                invalidated_cache_keys=tuple(sorted(set(invalidated_cache_keys))),
                invalidated_sample_ids=tuple(sorted(set(invalidated_sample_ids))),
                details=copy.deepcopy(details or {}),
            )
            self._events.append(event)
            return event

    def _mark_recomputed(self, sample_ids: Iterable[str]) -> None:
        ids = list(sample_ids)
        with self._events_lock:
            for event in self._events:
                event.mark_recomputed(ids)

    def _mark_hit(self, sample_id: str) -> None:
        with self._events_lock:
            for event in self._events:
                event.mark_hit([sample_id])

    def _invalidate_sample_ids(
        self,
        sample_ids: Iterable[str],
        *,
        kind: str,
        details: dict[str, Any] | None = None,
        clear_all: bool = False,
    ) -> InvalidationEvent:
        affected = sorted(set(sample_ids))
        with self._cache_lock:
            if clear_all:
                keys = sorted(
                    {
                        key
                        for values in self._sample_cache_keys.values()
                        for key in values
                    }
                    | set(self.cache.all_keys())
                )
                invalidated_keys = self.cache.invalidate_keys(keys)
                self._sample_cache_keys.clear()
            else:
                keys: set[str] = set()
                for sample_id in affected:
                    keys.update(self._sample_cache_keys.pop(sample_id, set()))
                    keys.update(self.cache.keys_for_sample(sample_id))
                invalidated_keys = self.cache.invalidate_keys(sorted(keys))
        return self._record_event(
            kind,
            affected,
            invalidated_keys,
            affected,
            details,
        )

    def _remember_cache_key(self, sample_id: str, key: str) -> None:
        with self._cache_lock:
            self._sample_cache_keys.setdefault(sample_id, set()).add(key)

    def query(
        self,
        sample_id: str,
        *,
        latency_budget_ms: float | None = None,
        precision: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        started_ns = time.perf_counter_ns()
        selected_precision = precision if precision is not None else self.config.precision
        raw_budget = (
            latency_budget_ms
            if latency_budget_ms is not None
            else self.config.latency_budget_ms
        )
        try:
            budget = float(raw_budget)
        except (TypeError, ValueError, OverflowError):
            budget = float("nan")
        snapshot = self._snapshot
        estimate: dict[str, Any] | None = None

        try:
            validate_precision(selected_precision)
        except DomainError as error:
            return _error_response(
                error,
                sample_id=sample_id,
                snapshot=snapshot,
                precision=selected_precision,
                budget_ms=budget if math.isfinite(budget) else None,
                measured_ns=time.perf_counter_ns() - started_ns,
            )
        if (
            isinstance(raw_budget, bool)
            or not math.isfinite(budget)
            or budget <= 0
        ):
            error = DomainError(
                E_INVALID_LATENCY_BUDGET,
                "latency_budget_ms must be a finite positive number",
                field="latency_budget_ms",
                sample_id=sample_id,
                stage="validation",
                category="configuration",
            )
            return _error_response(
                error,
                sample_id=sample_id,
                snapshot=snapshot,
                precision=selected_precision,
                budget_ms=budget if math.isfinite(budget) else None,
                measured_ns=time.perf_counter_ns() - started_ns,
            )
        if not isinstance(sample_id, str) or sample_id in self._duplicate_ids:
            error = (
                self._duplicate_errors([sample_id])[0]
                if isinstance(sample_id, str)
                else DomainError(
                    E_INVALID_SAMPLE,
                    "sample_id must be a string",
                    field="sample_id",
                    stage="validation",
                )
            )
            return _error_response(
                error,
                sample_id=sample_id if isinstance(sample_id, str) else None,
                snapshot=snapshot,
                precision=selected_precision,
                budget_ms=budget,
                measured_ns=time.perf_counter_ns() - started_ns,
            )

        sample, revision, snapshot = self._capture(sample_id)
        if sample is None:
            error = DomainError(
                E_INVALID_SAMPLE,
                f"unknown sample_id: {sample_id}",
                field="sample_id",
                sample_id=sample_id,
                stage="validation",
            )
            return _error_response(
                error,
                sample_id=sample_id,
                snapshot=snapshot,
                precision=selected_precision,
                budget_ms=budget,
                measured_ns=time.perf_counter_ns() - started_ns,
            )
        estimate = estimate_latency(sample, self.config)
        errors, prepared = validate_sample_record(sample, self.config)
        if errors:
            error = errors[0]
            return _error_response(
                error,
                sample_id=sample_id,
                snapshot=snapshot,
                precision=selected_precision,
                estimate=estimate,
                budget_ms=budget,
                measured_ns=time.perf_counter_ns() - started_ns,
            )
        assert prepared is not None
        text = analyze_text(sample.prompt, sample.reference)
        cache_key = compute_cache_key(
            sample.sample_id,
            prepared.image_content_hash,
            text.digest,
            snapshot,
            self.config,
        )

        cache_status = "miss"
        cache_source: str | None = None
        cache_resident: bool | None = None
        cache_entry: CacheEntry | None = None
        resource_error: DomainError | None = None

        if use_cache:
            with self._cache_lock:
                lookup = self.cache.get(cache_key)
            if lookup is not None:
                candidate = lookup.entry
                expected_versions = {
                    name: component["version"]
                    for name, component in snapshot.summary()["components"].items()
                }
                if (
                    candidate.sample_id == sample_id
                    and candidate.snapshot_id == snapshot.snapshot_id
                    and candidate.snapshot_versions == expected_versions
                    and candidate.image_content_hash == prepared.image_content_hash
                    and candidate.text_term_digest == text.digest
                    and candidate.precision == selected_precision
                ):
                    result = deep_copy_json(candidate.result)
                    cache_entry = candidate
                    cache_status = "hit"
                    cache_source = lookup.source
                    cache_resident = lookup.resident
                    self._remember_cache_key(sample_id, cache_key)
                    self._mark_hit(sample_id)

        if cache_entry is None:
            result = run_pipeline(prepared, self.config, snapshot)
            self._mark_recomputed([sample_id])
            if use_cache:
                entry = CacheEntry.from_result(result)
                should_store = False
                with self._state_lock:
                    current_snapshot = self._snapshot
                    current_revision = self._sample_revisions.get(sample_id, 0)
                    should_store = (
                        current_snapshot.snapshot_id == snapshot.snapshot_id
                        and current_revision == revision
                    )
                    if should_store:
                        with self._cache_lock:
                            try:
                                self.cache.put(entry)
                            except DomainError as error:
                                resource_error = error
                            else:
                                self._sample_cache_keys.setdefault(sample_id, set()).add(cache_key)
                                cache_status = "stored"
                                cache_entry = entry
                if not should_store:
                    cache_status = "stale_skipped"

        measured_ns = time.perf_counter_ns() - started_ns
        measured_within_budget = measured_ns <= budget * 1_000_000.0
        completed_stages = [
            stage for stage in STAGE_ORDER if stage in result.get("stages", {})
        ]
        query_errors: list[DomainError] = []
        if not measured_within_budget:
            query_errors.append(
                DomainError(
                    E_LATENCY_BUDGET,
                    "query exceeded the declared latency budget",
                    field="latency_budget_ms",
                    sample_id=sample_id,
                    stage="quality",
                    category="latency",
                    degradation_category="LATENCY_BUDGET_EXCEEDED",
                    completed_stages=completed_stages,
                    details={
                        "measured_ms": round(measured_ns / 1_000_000.0, 6),
                        "budget_ms": budget,
                    },
                )
            )
        if resource_error is not None:
            resource_error.sample_id = sample_id
            resource_error.stage = "quality"
            resource_error.completed_stages = tuple(completed_stages)
            query_errors.append(resource_error)

        response = deep_copy_json(result)
        response["schema_version"] = "igeval.query/1"
        response["status"] = "error" if query_errors else "ok"
        response["code"] = query_errors[0].code if query_errors else None
        response["errors"] = [error.to_dict() for error in query_errors]
        response["degradation"] = [
            {"category": error.degradation_category, "code": error.code}
            for error in query_errors
            if error.degradation_category
        ]
        response["cache"] = {
            "status": cache_status if use_cache else "bypass",
            "key": cache_key,
            "source": cache_source,
            "resident": cache_resident,
            "feature_digest": result["feature_digest"],
            "score_digest": result["score_digest"],
            "entry_checksum": cache_entry.content_checksum if cache_entry else None,
            "stats": self.cache.stats() if use_cache else None,
        }
        response["latency"] = {
            "observed_by": "local_evaluation_loop",
            "measured_ms": round(measured_ns / 1_000_000.0, 6),
            "budget_ms": budget,
            "within_budget": measured_within_budget,
            "completed_stages": completed_stages,
            "estimate": estimate,
            "degradation_category": (
                query_errors[0].degradation_category if query_errors else None
            ),
        }
        return response

    def _batch_error_report(
        self,
        errors: list[DomainError],
        *,
        snapshot: EvaluationSnapshot,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        sorted_errors = sorted(
            errors,
            key=lambda error: (
                error.sample_id or "",
                STAGE_ORDER.index(error.stage) if error.stage in STAGE_ORDER else -1,
                error.code,
            ),
        )
        stage_errors = {stage: [] for stage in STAGE_ORDER}
        validation_errors: list[dict[str, Any]] = []
        unattributed_errors: list[dict[str, Any]] = []
        for error in sorted_errors:
            payload = error.to_dict()
            if error.stage in stage_errors:
                stage_errors[error.stage].append(payload)
            elif error.stage == "validation":
                validation_errors.append(payload)
            else:
                unattributed_errors.append(payload)
        return {
            "schema_version": "igeval.batch-report/1",
            "status": "error",
            "code": sorted_errors[0].code if sorted_errors else None,
            "snapshot": snapshot.summary(),
            "precision": self.config.precision,
            "config_fingerprint": self.config.fingerprint,
            "elapsed_ms": round(elapsed_ms, 6),
            "results": [],
            "ranking": [],
            "stage_sample_ids": {stage: [] for stage in STAGE_ORDER},
            "summary": {
                "evaluated": 0,
                "failed": len(sorted_errors),
                "unattributed": len(unattributed_errors),
            },
            "errors": [error.to_dict() for error in sorted_errors],
            "errors_by_stage": stage_errors,
            "validation_errors": validation_errors,
            "unattributed_errors": unattributed_errors,
        }

    def evaluate_batch(
        self,
        sample_ids: Sequence[str] | None = None,
        *,
        workers: int | None = None,
    ) -> dict[str, Any]:
        started_ns = time.perf_counter_ns()
        with self._state_lock:
            snapshot = self._snapshot
            duplicate_ids = set(self._duplicate_ids)
            if sample_ids is None:
                selected_ids = sorted(self._samples)
            else:
                selected_ids = list(sample_ids)
            selected_samples = [
                self._samples[sample_id]
                for sample_id in selected_ids
                if sample_id in self._samples
            ]
            counts: dict[str, int] = {}
            for sample_id in selected_ids:
                counts[sample_id] = counts.get(sample_id, 0) + 1
            duplicate_ids.update(
                sample_id for sample_id, count in counts.items() if count > 1
            )

        if duplicate_ids:
            elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000.0
            return self._batch_error_report(
                self._duplicate_errors(duplicate_ids),
                snapshot=snapshot,
                elapsed_ms=elapsed,
            )

        missing_errors = [
            DomainError(
                E_INVALID_SAMPLE,
                f"unknown sample_id: {sample_id}",
                field="sample_id",
                sample_id=sample_id if isinstance(sample_id, str) else None,
                stage="validation",
            )
            for sample_id in selected_ids
            if sample_id not in self._samples
        ]
        if missing_errors:
            elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000.0
            return self._batch_error_report(
                missing_errors,
                snapshot=snapshot,
                elapsed_ms=elapsed,
            )

        worker_count = self.config.workers if workers is None else workers
        if (
            isinstance(worker_count, bool)
            or not isinstance(worker_count, int)
            or worker_count <= 0
        ):
            error = DomainError(
                E_INVALID_CONFIG,
                "workers must be a positive integer",
                field="workers",
                category="configuration",
                stage="validation",
            )
            elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000.0
            return self._batch_error_report([error], snapshot=snapshot, elapsed_ms=elapsed)

        errors: list[DomainError] = []
        payloads: list[tuple[str, dict[str, Any]]] = []
        for sample in selected_samples:
            validation_errors, prepared = validate_sample_record(sample, self.config)
            if validation_errors:
                errors.extend(validation_errors)
                continue
            assert prepared is not None
            payloads.append(
                (
                    sample.sample_id,
                    {
                        "config": self.config.to_dict(),
                        "snapshot": snapshot.to_dict(),
                        "sample": sample.to_wire(),
                    },
                )
            )

        outcomes: list[tuple[str, dict[str, Any]]] = []
        if payloads:
            if worker_count == 1 or len(payloads) == 1:
                for sample_id, payload in payloads:
                    outcomes.append((sample_id, evaluate_worker(payload)))
            else:
                context = multiprocessing.get_context("spawn")
                with ProcessPoolExecutor(
                    max_workers=min(worker_count, len(payloads)),
                    mp_context=context,
                ) as executor:
                    futures = {
                        executor.submit(evaluate_worker, payload): sample_id
                        for sample_id, payload in payloads
                    }
                    for future in as_completed(futures):
                        sample_id = futures[future]
                        try:
                            outcomes.append((sample_id, future.result()))
                        except Exception as exc:
                            errors.append(
                                DomainError(
                                    E_UNATTRIBUTED,
                                    "process worker future failed outside a scoring stage",
                                    sample_id=sample_id,
                                    stage=None,
                                    category="unattributed",
                                    details={
                                        "exception_type": type(exc).__name__,
                                        "message": str(exc),
                                    },
                                )
                            )

        results: list[dict[str, Any]] = []
        worker_pids: set[int] = set()
        for sample_id, outcome in outcomes:
            if outcome.get("ok"):
                worker_pid = outcome.get("worker_pid")
                if isinstance(worker_pid, int):
                    worker_pids.add(worker_pid)
                results.append(outcome["result"])
            else:
                error_payload = outcome.get("error", {})
                error = DomainError(
                    error_payload.get("code", E_UNATTRIBUTED),
                    error_payload.get("message", "worker evaluation failed"),
                    field=error_payload.get("field"),
                    sample_id=error_payload.get("sample_id", sample_id),
                    stage=error_payload.get("stage"),
                    category=error_payload.get("category"),
                    degradation_category=error_payload.get("degradation_category"),
                    completed_stages=error_payload.get("completed_stages"),
                    details=error_payload.get("details"),
                )
                errors.append(error)

        results.sort(key=lambda item: (-float(item["final_score"]), item["sample_id"]))
        ranking: list[dict[str, Any]] = []
        for rank, item in enumerate(results, start=1):
            item["rank"] = rank
            ranking.append(
                {
                    "rank": rank,
                    "sample_id": item["sample_id"],
                    "final_score": item["final_score"],
                }
            )

        sorted_errors = sorted(
            errors,
            key=lambda error: (
                error.sample_id or "",
                STAGE_ORDER.index(error.stage) if error.stage in STAGE_ORDER else -1,
                error.code,
            ),
        )
        stage_errors = {stage: [] for stage in STAGE_ORDER}
        validation_errors: list[dict[str, Any]] = []
        unattributed_errors: list[dict[str, Any]] = []
        for error in sorted_errors:
            payload = error.to_dict()
            if error.stage in stage_errors:
                stage_errors[error.stage].append(payload)
            elif error.stage == "validation":
                validation_errors.append(payload)
            else:
                unattributed_errors.append(payload)

        stage_sample_ids = {
            stage: sorted(
                item["sample_id"]
                for item in results
                if item.get("stages", {}).get(stage, {}).get("status") == "completed"
            )
            for stage in STAGE_ORDER
        }
        successful_ids = [item["sample_id"] for item in results]
        self._mark_recomputed(successful_ids)
        elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
        status = "ok"
        if sorted_errors:
            status = "partial" if results else "error"
        mean_score = (
            quantize(
                sum(float(item["final_score"]) for item in results) / len(results),
                self.config.score_decimals,
            )
            if results
            else None
        )
        grade_exact = sum(
            1 for item in results if item["predicted_grade"] == item["truth_grade"]
        )
        grade_within_one = sum(
            1 for item in results if abs(item["predicted_grade"] - item["truth_grade"]) <= 1
        )
        return {
            "schema_version": "igeval.batch-report/1",
            "status": status,
            "code": sorted_errors[0].code if sorted_errors else None,
            "snapshot": snapshot.summary(),
            "precision": self.config.precision,
            "config_fingerprint": self.config.fingerprint,
            "elapsed_ms": round(elapsed_ms, 6),
            "execution": {
                "process_pool": worker_count > 1 and len(payloads) > 1,
                "worker_pids": sorted(worker_pids),
            },
            "results": results,
            "ranking": ranking,
            "stage_sample_ids": stage_sample_ids,
            "summary": {
                "evaluated": len(results),
                "failed": len(sorted_errors),
                "unattributed": len(unattributed_errors),
                "mean_score": mean_score,
                "truth_grade_exact": grade_exact,
                "truth_grade_within_one": grade_within_one,
                "successful_sample_ids": sorted(successful_ids),
            },
            "errors": [error.to_dict() for error in sorted_errors],
            "errors_by_stage": stage_errors,
            "validation_errors": validation_errors,
            "unattributed_errors": unattributed_errors,
        }

    def get_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = self._snapshot
            events = len(self._events)
        with self._cache_lock:
            cache_stats = self.cache.stats()
        return {
            "schema_version": "igeval.snapshot-query/1",
            "status": "ok",
            "snapshot": snapshot.to_dict(),
            "summary": snapshot.summary(),
            "sample_count": len(self._all_sample_ids()),
            "invalidation_event_count": events,
            "cache": cache_stats,
            "files": self.snapshot_store.list_files(),
        }

    def switch_snapshot(
        self,
        snapshot: EvaluationSnapshot | dict[str, Any],
    ) -> dict[str, Any]:
        try:
            candidate = (
                EvaluationSnapshot.from_dict(snapshot)
                if isinstance(snapshot, dict)
                else (
                    EvaluationSnapshot.from_dict(snapshot.to_dict())
                    if isinstance(snapshot, EvaluationSnapshot)
                    else snapshot
                )
            )
            if not isinstance(candidate, EvaluationSnapshot):
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot must be an EvaluationSnapshot or mapping",
                    field="snapshot",
                )
        except DomainError as error:
            return {
                "schema_version": "igeval.snapshot-switch/1",
                "status": "error",
                "code": error.code,
                "errors": [error.to_dict()],
            }

        with self._state_lock:
            current = self._snapshot
            disk_current = self.snapshot_store.load_current()
            baseline = disk_current if disk_current is not None else current
            reasons: list[dict[str, Any]] = []
            if candidate.snapshot_version <= max(
                current.snapshot_version, baseline.snapshot_version
            ):
                reasons.append(
                    {
                        "field": "snapshot_version",
                        "current": max(current.snapshot_version, baseline.snapshot_version),
                        "candidate": candidate.snapshot_version,
                    }
                )
            for name in ("normalization", "alignment", "structure"):
                current_component = current.to_dict()["components"][name]
                baseline_component = baseline.to_dict()["components"][name]
                candidate_component = candidate.to_dict()["components"][name]
                baseline_version = max(
                    current_component["version"], baseline_component["version"]
                )
                if candidate_component["version"] < baseline_version:
                    reasons.append(
                        {
                            "field": f"components.{name}.version",
                            "current": baseline_version,
                            "candidate": candidate_component["version"],
                        }
                    )
                elif (
                    candidate_component["version"] == baseline_version
                    and candidate_component["checksum"]
                    != current_component["checksum"]
                    and candidate_component["checksum"]
                    != baseline_component["checksum"]
                ):
                    reasons.append(
                        {
                            "field": f"components.{name}.checksum",
                            "reason": "same version cannot replace different content",
                        }
                    )
            if reasons:
                error = DomainError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot switch would roll back or replace a version",
                    field=reasons[0]["field"],
                    category="snapshot",
                    details={"reasons": reasons},
                )
                return {
                    "schema_version": "igeval.snapshot-switch/1",
                    "status": "error",
                    "code": error.code,
                    "errors": [error.to_dict()],
                }
            try:
                self.snapshot_store.set_current(candidate)
            except DomainError as error:
                return {
                    "schema_version": "igeval.snapshot-switch/1",
                    "status": "error",
                    "code": error.code,
                    "errors": [error.to_dict()],
                }
            self._snapshot = candidate
            affected = sorted(self._samples)
            event = self._invalidate_sample_ids(
                affected,
                kind="snapshot_switch",
                details={
                    "from_snapshot_id": current.snapshot_id,
                    "to_snapshot_id": candidate.snapshot_id,
                    "from_snapshot_version": current.snapshot_version,
                    "to_snapshot_version": candidate.snapshot_version,
                },
                clear_all=True,
            )
        return {
            "schema_version": "igeval.snapshot-switch/1",
            "status": "ok",
            "code": None,
            "snapshot": candidate.summary(),
            "invalidation": event.to_dict(),
        }

    def append_samples(self, samples: Sequence[EvaluationSample]) -> dict[str, Any]:
        candidate_errors: list[DomainError] = []
        incoming_ids: list[str] = []
        with self._state_lock:
            existing_ids = set(self._samples) | self._duplicate_ids
        counts: dict[str, int] = {}
        for sample in samples:
            counts[sample.sample_id] = counts.get(sample.sample_id, 0) + 1
            incoming_ids.append(sample.sample_id)
            validation_errors, _ = validate_sample_record(sample, self.config)
            candidate_errors.extend(validation_errors)
        duplicates = {
            sample_id
            for sample_id, count in counts.items()
            if count > 1 or sample_id in existing_ids
        }
        candidate_errors.extend(self._duplicate_errors(duplicates))
        if candidate_errors:
            return {
                "schema_version": "igeval.sample-change/1",
                "status": "error",
                "code": candidate_errors[0].code,
                "errors": [error.to_dict() for error in candidate_errors],
            }
        with self._state_lock:
            for sample in samples:
                self._samples[sample.sample_id] = sample
                self._sample_revisions[sample.sample_id] = 1
            affected = sorted(set(incoming_ids))
            event = self._invalidate_sample_ids(
                affected,
                kind="sample_append",
                details={"appended_sample_ids": affected},
            )
        return {
            "schema_version": "igeval.sample-change/1",
            "status": "ok",
            "code": None,
            "sample_ids": affected,
            "invalidation": event.to_dict(),
        }

    def update_prompt(self, sample_id: str, prompt: str) -> dict[str, Any]:
        with self._state_lock:
            current = self._samples.get(sample_id)
            if current is None:
                error = DomainError(
                    E_INVALID_SAMPLE,
                    f"unknown sample_id: {sample_id}",
                    field="sample_id",
                    sample_id=sample_id,
                    stage="validation",
                )
                return {
                    "schema_version": "igeval.sample-change/1",
                    "status": "error",
                    "code": error.code,
                    "errors": [error.to_dict()],
                }
            updated = dataclasses.replace(current, prompt=prompt)
            validation_errors, _ = validate_sample_record(updated, self.config)
            if validation_errors:
                return {
                    "schema_version": "igeval.sample-change/1",
                    "status": "error",
                    "code": validation_errors[0].code,
                    "errors": [error.to_dict() for error in validation_errors],
                }
            self._samples[sample_id] = updated
            self._sample_revisions[sample_id] += 1
            event = self._invalidate_sample_ids(
                [sample_id],
                kind="prompt_update",
                details={
                    "sample_id": sample_id,
                    "old_text_digest": analyze_text(
                        current.prompt, current.reference
                    ).digest,
                    "new_text_digest": analyze_text(
                        updated.prompt, updated.reference
                    ).digest,
                },
            )
        return {
            "schema_version": "igeval.sample-change/1",
            "status": "ok",
            "code": None,
            "sample": updated.to_wire(),
            "invalidation": event.to_dict(),
        }

    def invalidation_report(self, event_id: int | None = None) -> dict[str, Any]:
        with self._events_lock:
            events = list(self._events)
        if event_id is not None:
            events = [event for event in events if event.event_id == event_id]
            if not events:
                error = DomainError(
                    E_INVALID_SAMPLE,
                    f"unknown invalidation event_id: {event_id}",
                    field="event_id",
                    category="invalidation",
                )
                return {
                    "schema_version": "igeval.invalidation-report/1",
                    "status": "error",
                    "code": error.code,
                    "errors": [error.to_dict()],
                    "events": [],
                }
        payloads = [event.to_dict() for event in events]
        all_ok = all(event.coverage_ok for event in events)
        with self._cache_lock:
            cache_stats = self.cache.stats()
        return {
            "schema_version": "igeval.invalidation-report/1",
            "status": "ok",
            "code": None,
            "coverage_ok": all_ok,
            "events": payloads,
            "cache": cache_stats,
        }

    def cache_entry_report(self, sample_id: str) -> dict[str, Any]:
        with self._state_lock:
            revisions = self._sample_revisions.get(sample_id)
            snapshot = self._snapshot
            sample = self._samples.get(sample_id)
        if sample is None or revisions is None:
            return {
                "status": "error",
                "code": E_INVALID_SAMPLE,
                "sample_id": sample_id,
            }
        errors, prepared = validate_sample_record(sample, self.config)
        if errors:
            return {"status": "error", "code": errors[0].code, "errors": [e.to_dict() for e in errors]}
        assert prepared is not None
        text = analyze_text(sample.prompt, sample.reference)
        key = compute_cache_key(
            sample.sample_id,
            prepared.image_content_hash,
            text.digest,
            snapshot,
            self.config,
        )
        with self._cache_lock:
            entry = self.cache.describe_entry(key)
        return {
            "status": "ok" if entry is not None else "miss",
            "code": None if entry is not None else "E_CACHE_MISS",
            "sample_id": sample_id,
            "cache_key": key,
            "entry": entry,
        }

    def self_check(self, batch_report: dict[str, Any] | None = None) -> dict[str, Any]:
        from .selfcheck import run_self_check

        return run_self_check(self, batch_report)

