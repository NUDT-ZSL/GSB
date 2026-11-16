"""Interactive application state machine and deterministic reporting."""

from __future__ import annotations

import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .cache import CacheBundle
from .config import (
    ExperimentConfig,
    PlanConfig,
    apply_plan_param,
    load_experiment_config,
    plan_from_dict,
)
from .data import Dataset, load_dataset
from .errors import (
    E_BUDGET_EXHAUSTED,
    E_COMMAND_INVALID,
    E_EVALUATION_FAILED,
    FeatureLabError,
    config_error,
)
from .pipeline import (
    EvaluationPipeline,
    FoldOutcome,
    Preparation,
    aggregate_folds,
    rank_key,
)
from .snapshots import SnapshotManager
from .utils import atomic_write_json, fingerprint, parse_cli_value


@dataclass
class PlanRecord:
    plan: PlanConfig
    created_sequence: int
    status: str = "PENDING"
    code: Optional[str] = None
    preparation: Optional[Mapping[str, Any]] = None
    fold_results: dict[int, FoldOutcome] = field(default_factory=dict)
    metrics: Optional[Mapping[str, Any]] = None
    fold_metrics: list[Mapping[str, Any]] = field(default_factory=list)
    failure: Optional[Mapping[str, Any]] = None
    completed_sequence: Optional[int] = None

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED" and self.metrics is not None

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "plan_id": self.plan_id,
            "plan": self.plan.to_dict(),
            "status": self.status,
            "code": self.code,
            "metrics": dict(self.metrics) if self.metrics is not None else None,
            "fold_metrics": list(self.fold_metrics),
            "progress": {
                "completed_folds": len(self.fold_results),
                "total_folds": None,
            },
            "basis": (
                "CONFIRMED_CROSS_VALIDATION"
                if self.confirmed
                else "NO_AGGREGATE_UNTIL_ALL_FOLDS_CONFIRMED"
            ),
            "failure": dict(self.failure) if self.failure else None,
        }
        if self.preparation:
            selection = self.preparation.get("selection", {})
            result["selected_features"] = list(selection.get("selected_features", []))
            result["removed_features"] = list(selection.get("removed_features", []))
            result["elimination_rounds"] = list(selection.get("elimination_rounds", []))
            result["encodings"] = list(self.preparation.get("resolved", []))
            result["feature_names"] = list(self.preparation.get("feature_names", []))
        else:
            result["selected_features"] = None
            result["removed_features"] = None
            result["elimination_rounds"] = None
            result["encodings"] = [
                {"column": column, "requested_method": method, "resolved_method": None}
                for column, method in self.plan.encodings
            ]
            result["feature_names"] = None
        return result


class _EvaluationJob:
    def __init__(self, app: "FeatureEngineeringApp", record: PlanRecord) -> None:
        self.app = app
        self.record = record
        self.event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"featurelab-job-{record.plan_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.event.wait(timeout)

    def _set_progress(self, fold_results: Mapping[int, FoldOutcome]) -> None:
        with self.app._state_lock:
            self.record.fold_results = dict(fold_results)

    def _run(self) -> None:
        try:
            preparation = self.app.pipeline.prepare(self.record.plan)
            with self.app._state_lock:
                self.record.preparation = preparation.to_dict()
            futures = {
                fold: self.app._executor.submit(
                    self.app.pipeline.fold_task,
                    self.record.plan,
                    preparation,
                    fold,
                )
                for fold in range(self.app.experiment.cv_folds)
            }
            outcomes: dict[int, FoldOutcome] = {}
            for future in as_completed(futures.values()):
                outcome = future.result()
                outcomes[outcome.fold] = outcome
                self._set_progress(outcomes)
            ordered = [outcomes[fold] for fold in sorted(outcomes)]
            aggregate = aggregate_folds(self.record.plan_id, ordered)
            with self.app._state_lock:
                self.record.status = "CONFIRMED"
                self.record.code = None
                self.record.failure = None
                self.record.metrics = aggregate["metrics"]
                self.record.fold_metrics = aggregate["fold_metrics"]
                self.record.fold_results = {outcome.fold: outcome for outcome in ordered}
                self.record.completed_sequence = self.app._sequence
        except FeatureLabError as exc:
            with self.app._state_lock:
                self.record.status = "FAILED"
                self.record.code = exc.code
                self.record.failure = exc.to_dict()
                self.record.metrics = None
                self.app._failures.append(
                    {
                        **exc.to_dict(),
                        "scope": "plan_evaluation",
                        "plan_id": self.record.plan_id,
                    }
                )
        except Exception as exc:  # defensive boundary for a background thread
            wrapped = FeatureLabError(
                E_EVALUATION_FAILED,
                "unexpected evaluation failure",
                details={"error": repr(exc), "plan_id": self.record.plan_id},
            )
            with self.app._state_lock:
                self.record.status = "FAILED"
                self.record.code = wrapped.code
                self.record.failure = wrapped.to_dict()
                self.record.metrics = None
                self.app._failures.append(
                    {**wrapped.to_dict(), "scope": "plan_evaluation", "plan_id": self.record.plan_id}
                )
        finally:
            self.event.set()


class FeatureEngineeringApp:
    def __init__(
        self,
        config: str | Path | ExperimentConfig,
        *,
        serial: bool = False,
        cache_enabled: bool = True,
        state_dir: Optional[str | Path] = None,
        resume: bool = True,
    ) -> None:
        self.experiment = (
            config if isinstance(config, ExperimentConfig) else load_experiment_config(config)
        )
        self.dataset: Dataset = load_dataset(
            self.experiment.data_description, self.experiment.target_column
        )
        self.serial = bool(serial)
        self.cache = CacheBundle(enabled=cache_enabled)
        self.pipeline = EvaluationPipeline(self.dataset, self.experiment, self.cache)
        manager_dir = Path(state_dir) if state_dir is not None else self.experiment.snapshot_dir
        self.snapshot_manager = SnapshotManager(manager_dir)
        self._state_lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1 if self.serial else self.experiment.max_workers,
            thread_name_prefix="featurelab-fold",
        )
        self._snapshot_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="featurelab-snapshot"
        )
        self._active_plan: PlanConfig = self.experiment.initial_plan
        self._records: dict[str, PlanRecord] = {}
        self._sequence = 0
        self._commands: list[dict[str, Any]] = []
        self._failures: list[dict[str, Any]] = []
        self._jobs: dict[str, _EvaluationJob] = {}
        self._closed = False
        self.snapshot_version = 0
        self.recovered = False
        self.recovered_path: Optional[str] = None
        if resume:
            self._restore()
        self._resume_pending_jobs()

    @property
    def active_plan(self) -> PlanConfig:
        return self._active_plan

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def failures(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [dict(item) for item in self._failures]

    def _restore(self) -> None:
        recovery = self.snapshot_manager.recover(
            expected_experiment_fingerprint=self.experiment.fingerprint,
            expected_dataset_fingerprint=self.dataset.fingerprint,
        )
        self._failures.extend(dict(item) for item in recovery.failures)
        if recovery.payload is None:
            return
        payload = recovery.payload
        try:
            active_raw = payload.get("active_plan")
            if not isinstance(active_raw, Mapping):
                raise ValueError("active_plan missing")
            self._active_plan = plan_from_dict(
                active_raw,
                self.experiment.data_description,
                self.experiment.target_column,
            )
            self._sequence = int(payload.get("sequence", 0))
            self._commands = list(payload.get("commands", []))
            self._failures.extend(dict(item) for item in payload.get("failures", []))
            cache_raw = payload.get("caches")
            if isinstance(cache_raw, Mapping):
                self.cache.load_dict(cache_raw)
            records_raw = payload.get("records", [])
            for item in records_raw:
                plan = plan_from_dict(
                    item["plan"],
                    self.experiment.data_description,
                    self.experiment.target_column,
                )
                outcomes: dict[int, FoldOutcome] = {}
                for raw_outcome in item.get("fold_results", []):
                    outcome = FoldOutcome(
                        fold=int(raw_outcome["fold"]),
                        stable_key=str(raw_outcome["stable_key"]),
                        metrics=dict(raw_outcome["metrics"]),
                        n_train=int(raw_outcome["n_train"]),
                        n_valid=int(raw_outcome["n_valid"]),
                        predictions_fingerprint=str(raw_outcome["predictions_fingerprint"]),
                    )
                    outcomes[outcome.fold] = outcome
                record = PlanRecord(
                    plan=plan,
                    created_sequence=int(item.get("created_sequence", 0)),
                    status=str(item.get("status", "PENDING")),
                    code=item.get("code"),
                    preparation=item.get("preparation"),
                    fold_results=outcomes,
                    metrics=item.get("metrics"),
                    fold_metrics=list(item.get("fold_metrics", [])),
                    failure=item.get("failure"),
                    completed_sequence=item.get("completed_sequence"),
                )
                self._records[record.plan_id] = record
            self.snapshot_version = recovery.version
            self.recovered = True
            self.recovered_path = recovery.recovered_path
        except Exception as exc:
            self._failures.append(
                {
                    "code": "E_SNAPSHOT_CORRUPT",
                    "message": "snapshot payload could not be reconstructed",
                    "details": {"snapshot_version": recovery.version, "error": str(exc)},
                }
            )
            self._active_plan = self.experiment.initial_plan
            self._records = {}
            self._sequence = 0
            self._commands = []
            self.cache = CacheBundle(enabled=True)
            self.pipeline = EvaluationPipeline(self.dataset, self.experiment, self.cache)
            self.snapshot_version = 0
            self.recovered = False

    def _resume_pending_jobs(self) -> None:
        pending = [
            record
            for record in self._records.values()
            if record.status not in {"CONFIRMED", "FAILED"}
        ]
        pending.sort(key=lambda record: (record.created_sequence, record.plan_id))
        for record in pending:
            self._ensure_job(record)

    def _ensure_record(self, plan: PlanConfig) -> PlanRecord:
        record = self._records.get(plan.plan_id)
        if record is None:
            record = PlanRecord(plan=plan, created_sequence=self._sequence)
            self._records[plan.plan_id] = record
        return record

    def _ensure_job(self, record: PlanRecord) -> Optional[_EvaluationJob]:
        if self._closed or record.status in {"CONFIRMED", "FAILED"}:
            return None
        existing = self._jobs.get(record.plan_id)
        if existing is not None and existing.thread.is_alive():
            return existing
        job = _EvaluationJob(self, record)
        self._jobs[record.plan_id] = job
        job.start()
        return job

    def _cache_summary(self) -> dict[str, Any]:
        return self.cache.summary()

    @staticmethod
    def _invalidated_columns_delta(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> list[str]:
        before_counts = dict(before.get("feature_invalidation_counts", {}))
        after_counts = dict(after.get("feature_invalidation_counts", {}))
        affected: set[str] = set()
        for namespace, count in after_counts.items():
            if count <= int(before_counts.get(namespace, 0)):
                continue
            parts = namespace.split(":", 2)
            if len(parts) >= 2 and parts[0] == "feature":
                affected.add(parts[1])
        return sorted(affected)

    def _complete_record(self, record: PlanRecord) -> dict[str, Any]:
        summary = record.summary()
        summary["total_folds"] = self.experiment.cv_folds
        summary["progress"]["total_folds"] = self.experiment.cv_folds
        return summary

    def _best_record(self) -> Optional[PlanRecord]:
        confirmed = [record for record in self._records.values() if record.confirmed]
        if not confirmed:
            return None
        return min(
            confirmed,
            key=lambda record: rank_key(record.metrics or {}, record.plan_id),
        )

    def _ranked_records(self) -> list[PlanRecord]:
        confirmed = [record for record in self._records.values() if record.confirmed]
        confirmed.sort(key=lambda record: rank_key(record.metrics or {}, record.plan_id))
        return confirmed

    def _current_summary(self) -> dict[str, Any]:
        record = self._records.get(self._active_plan.plan_id)
        if record is None:
            return {
                "plan_id": self._active_plan.plan_id,
                "plan": self._active_plan.to_dict(),
                "status": "NOT_EVALUATED",
                "code": None,
                "metrics": None,
                "basis": "NOT_EVALUATED",
            }
        return self._complete_record(record)

    def _pending_summaries(self) -> list[dict[str, Any]]:
        pending = [
            record
            for record in self._records.values()
            if record.status == "PENDING"
        ]
        pending.sort(key=lambda record: (record.created_sequence, record.plan_id))
        result: list[dict[str, Any]] = []
        for record in pending:
            summary = self._complete_record(record)
            summary["code"] = record.code or E_BUDGET_EXHAUSTED
            result.append(summary)
        return result

    def _response(self, *, sequence: int, command: str, started: float) -> dict[str, Any]:
        current = self._current_summary()
        best = self._best_record()
        pending = self._pending_summaries()
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        result: dict[str, Any] = {
            "sequence": sequence,
            "command": command,
            "ok": True,
            "code": None,
            "status": current.get("status", "UNKNOWN"),
            "duration_ms": duration_ms,
            "budget_ms": self.experiment.latency_budget_ms,
            "within_budget": duration_ms <= self.experiment.latency_budget_ms,
            "current_plan": current,
            "best_plan": self._complete_record(best) if best is not None else None,
            "best_basis": (
                "CONFIRMED_CROSS_VALIDATION"
                if best is not None
                else "NO_CONFIRMED_RESULT"
            ),
            "confirmed_plan_ids": [record.plan_id for record in self._ranked_records()],
            "pending_plans": pending,
            "snapshot_version": self.snapshot_version,
        }
        if current.get("status") == "FAILED":
            result["code"] = current.get("code")
            result["status"] = "FAILED"
        elif current.get("status") == "PENDING":
            result["code"] = E_BUDGET_EXHAUSTED
            result["status"] = "PARTIAL"
        return result

    def _mutate_plan(self, new_plan: PlanConfig) -> PlanRecord:
        self._active_plan = new_plan
        return self._ensure_record(new_plan)

    def _parse_set(self, tokens: Sequence[str]) -> PlanRecord:
        if len(tokens) != 2:
            raise FeatureLabError(E_COMMAND_INVALID, "usage: set <column> <method>")
        column, method = tokens
        if column not in self.active_plan.encoding_map():
            raise config_error("unknown encoding column", column=column)
        snapshot = self.dataset.column(column)
        if snapshot.kind == "numeric":
            if method != "numeric":
                raise config_error(
                    "numeric column must use numeric encoding", column=column
                )
        elif method not in self.experiment.allowed_methods:
            raise config_error(
                "encoding method is not enabled in the experiment",
                column=column,
                method=method,
                methods=list(self.experiment.allowed_methods),
            )
        return self._mutate_plan(self.active_plan.with_method(column, method))

    def _parse_set_param(self, tokens: Sequence[str]) -> PlanRecord:
        if len(tokens) < 2:
            raise FeatureLabError(
                E_COMMAND_INVALID, "usage: set-param <path> <value>"
            )
        path = tokens[0]
        raw_value = " ".join(tokens[1:])
        value = parse_cli_value(raw_value)
        return self._mutate_plan(apply_plan_param(self.active_plan, path, value))

    def _record_command(self, response: Mapping[str, Any]) -> None:
        stable = {
            key: value
            for key, value in response.items()
            if key not in {"current_plan", "best_plan", "pending_plans", "report"}
        }
        stable["current_plan_id"] = response.get("current_plan", {}).get("plan_id")
        stable["best_plan_id"] = (
            response.get("best_plan", {}).get("plan_id")
            if response.get("best_plan") is not None
            else None
        )
        stable["pending_plan_ids"] = [
            item.get("plan_id") for item in response.get("pending_plans", [])
        ]
        self._commands.append(stable)

    def handle_command(self, command: str) -> dict[str, Any]:
        started = time.monotonic()
        with self._state_lock:
            self._sequence += 1
            sequence = self._sequence
        cache_before = self._cache_summary()
        try:
            tokens = shlex.split(command)
            if not tokens:
                raise FeatureLabError(E_COMMAND_INVALID, "empty command")
            verb = tokens[0].lower()
            schedule = False
            output: dict[str, Any] = {}
            if verb in {"set", "set-encoding"}:
                record = self._parse_set(tokens[1:])
                schedule = True
            elif verb in {"set-param", "set-selector"}:
                if verb == "set-selector" and len(tokens) >= 2:
                    tokens = [tokens[0], "selector." + tokens[1], *tokens[2:]]
                record = self._parse_set_param(tokens[1:])
                schedule = True
            elif verb in {"evaluate", "eval", "status"}:
                record = self._ensure_record(self.active_plan)
                schedule = True
            elif verb == "snapshot":
                record = self._ensure_record(self.active_plan)
            elif verb == "cache":
                record = self._ensure_record(self.active_plan)
                output["cache"] = self._cache_summary()
            elif verb in {"quit", "exit"}:
                record = self._ensure_record(self.active_plan)
                output["quitting"] = True
            elif verb == "help":
                record = self._ensure_record(self.active_plan)
                output["help"] = [
                    "set <column> <one_hot|target|frequency>",
                    "set-param <path> <value>",
                    "set-selector <path> <value>",
                    "evaluate | status | cache | report | snapshot | quit",
                ]
            elif verb == "report":
                record = self._ensure_record(self.active_plan)
            else:
                raise FeatureLabError(E_COMMAND_INVALID, "unknown command", details={"verb": verb})

            if schedule and record.status not in {"CONFIRMED", "FAILED"}:
                job = self._ensure_job(record)
                if job is not None:
                    budget_seconds = self.experiment.latency_budget_ms / 1000.0
                    response_reserve = min(0.005, budget_seconds * 0.35)
                    remaining = (
                        budget_seconds
                        - response_reserve
                        - (time.monotonic() - started)
                    )
                    completed = job.wait(max(0.0, remaining))
                    if not completed and record.status == "PENDING":
                        with self._state_lock:
                            record.code = E_BUDGET_EXHAUSTED

            response = self._response(sequence=sequence, command=command, started=started)
            response.update(output)
            if verb == "report":
                response["report"] = self.build_report()
            if verb == "snapshot":
                response["snapshot_requested"] = True
            cache_summary = self._cache_summary()
            response["cache_delta"] = {
                "hits": cache_summary["hits"] - cache_before["hits"],
                "misses": cache_summary["misses"] - cache_before["misses"],
                "invalidations": cache_summary["invalidations"]
                - cache_before["invalidations"],
                "computes": cache_summary["computes"] - cache_before["computes"],
                "invalidated_feature_columns": self._invalidated_columns_delta(
                    cache_before, cache_summary
                ),
            }
            self._record_command(response)
            # Persistence runs on a dedicated single-writer thread. Use any
            # remaining interaction budget to confirm durability; otherwise
            # return promptly and drain the writer during close().
            self._persist_command_snapshot(
                response, started, force_wait=(verb == "snapshot")
            )
            return response
        except FeatureLabError as exc:
            duration_ms = round((time.monotonic() - started) * 1000.0, 3)
            failure = {
                **exc.to_dict(),
                "scope": "command",
                "command": command,
            }
            with self._state_lock:
                self._failures.append(failure)
            response = {
                "sequence": sequence,
                "command": command,
                "ok": False,
                "code": exc.code,
                "status": "REJECTED",
                "duration_ms": duration_ms,
                "budget_ms": self.experiment.latency_budget_ms,
                "within_budget": duration_ms <= self.experiment.latency_budget_ms,
                "failure": exc.to_dict(),
                "current_plan": self._current_summary(),
                "best_plan": (
                    self._complete_record(self._best_record())
                    if self._best_record() is not None
                    else None
                ),
                "best_basis": "CONFIRMED_CROSS_VALIDATION",
                "confirmed_plan_ids": [record.plan_id for record in self._ranked_records()],
                "pending_plans": self._pending_summaries(),
                "cache_delta": {"hits": 0, "misses": 0, "invalidations": 0, "computes": 0},
                "snapshot_version": 0,
            }
            self._record_command(response)
            self._persist_command_snapshot(response, started)
            return response

    def _record_to_snapshot(self, record: PlanRecord) -> dict[str, Any]:
        return {
            "plan": record.plan.to_dict(),
            "created_sequence": record.created_sequence,
            "status": record.status,
            "code": record.code,
            "preparation": record.preparation,
            "fold_results": [
                record.fold_results[fold].to_dict()
                for fold in sorted(record.fold_results)
            ],
            "metrics": dict(record.metrics) if record.metrics is not None else None,
            "fold_metrics": [dict(item) for item in record.fold_metrics],
            "failure": dict(record.failure) if record.failure else None,
            "completed_sequence": record.completed_sequence,
        }

    def _snapshot_payload(self) -> dict[str, Any]:
        with self._state_lock:
            records = [
                self._record_to_snapshot(record)
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (item.created_sequence, item.plan_id),
                )
            ]
            return {
                "schema_version": 1,
                "experiment_fingerprint": self.experiment.fingerprint,
                "dataset_fingerprint": self.dataset.fingerprint,
                "active_plan": self._active_plan.to_dict(),
                "sequence": self._sequence,
                "commands": [dict(item) for item in self._commands],
                "records": records,
                "caches": self.cache.to_dict(),
                "failures": [dict(item) for item in self._failures],
            }

    def _write_snapshot(self) -> int:
        with self._snapshot_lock:
            payload = self._snapshot_payload()
            version = self.snapshot_manager.write(payload)
            self.snapshot_version = version
            return version

    def _persist_command_snapshot(
        self,
        response: dict[str, Any],
        started: float,
        *,
        force_wait: bool = False,
    ) -> None:
        future = self._snapshot_executor.submit(self._write_snapshot)
        response["snapshot_pending"] = True
        if force_wait:
            try:
                version = future.result()
            except FutureTimeoutError:
                version = None
            except Exception as exc:
                with self._state_lock:
                    self._failures.append(
                        {
                            "code": "E_SNAPSHOT_CORRUPT",
                            "message": "snapshot persistence failed",
                            "details": {"error": repr(exc)},
                            "scope": "snapshot_write",
                        }
                    )
                version = self.snapshot_version
            if version is not None:
                response["snapshot_version"] = version
                response["snapshot_pending"] = False
                if self._commands:
                    self._commands[-1]["snapshot_version"] = version
        response["duration_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        response["within_budget"] = (
            response["duration_ms"] <= self.experiment.latency_budget_ms
        )
        if self._commands:
            self._commands[-1]["duration_ms"] = response["duration_ms"]
            self._commands[-1]["within_budget"] = response["within_budget"]
            self._commands[-1]["snapshot_pending"] = response["snapshot_pending"]
            self._commands[-1]["snapshot_version"] = response["snapshot_version"]

    def write_report(self, path: Optional[str | Path] = None) -> Path:
        target = Path(path) if path is not None else self.experiment.report_path
        atomic_write_json(target, self.build_report())
        return target

    def build_report(self) -> dict[str, Any]:
        with self._state_lock:
            ranked = self._ranked_records()
            rank_by_id = {record.plan_id: index + 1 for index, record in enumerate(ranked)}
            plans: list[dict[str, Any]] = []
            records = sorted(
                self._records.values(),
                key=lambda record: (record.created_sequence, record.plan_id),
            )
            for record in records:
                item = self._complete_record(record)
                item["rank"] = rank_by_id.get(record.plan_id)
                item["active"] = record.plan_id == self._active_plan.plan_id
                plans.append(item)
            best = self._best_record()
            ranking = [record.plan_id for record in ranked]
            report = {
                "schema_version": 1,
                "dataset": {
                    "fingerprint": self.dataset.fingerprint,
                    "description_fingerprint": self.dataset.description.fingerprint,
                    "target_column": self.experiment.target_column,
                    "target_classes": list(self.dataset.target_classes),
                    "rows": self.dataset.n_rows,
                    "columns": [
                        {
                            "name": name,
                            "kind": self.dataset.columns[name].kind,
                            "fingerprint": self.dataset.columns[name].content_fingerprint,
                        }
                        for name in self.dataset.column_order
                    ],
                },
                "experiment": {
                    "fingerprint": self.experiment.fingerprint,
                    "latency_budget_ms": self.experiment.latency_budget_ms,
                    "max_workers": 1 if self.serial else self.experiment.max_workers,
                    "serial": self.serial,
                    "cv_folds": self.experiment.cv_folds,
                    "cv_seed": self.experiment.cv_seed,
                    "task_delay_ms": self.experiment.task_delay_ms,
                },
                "active_plan_id": self._active_plan.plan_id,
                "plans": plans,
                "ranking": ranking,
                "best_plan": self._complete_record(best) if best is not None else None,
                "commands": [dict(item) for item in self._commands],
                "cache": self._cache_summary(),
                "failures": [dict(item) for item in self._failures],
                "snapshot_version": self.snapshot_version,
                "snapshot_dir": str(self.snapshot_manager.directory),
                "recovered": self.recovered,
                "recovered_path": self.recovered_path,
            }
            semantic = self._semantic_projection(report)
            report["semantic_fingerprint"] = fingerprint(semantic)
            return report

    def _semantic_projection(self, report: Mapping[str, Any]) -> dict[str, Any]:
        plans: list[dict[str, Any]] = []
        for item in report.get("plans", []):
            plans.append(
                {
                    "plan_id": item.get("plan_id"),
                    "plan": item.get("plan"),
                    "status": item.get("status"),
                    "code": item.get("code"),
                    "metrics": item.get("metrics"),
                    "fold_metrics": item.get("fold_metrics"),
                    "selected_features": item.get("selected_features"),
                    "removed_features": item.get("removed_features"),
                    "elimination_rounds": item.get("elimination_rounds"),
                    "encodings": item.get("encodings"),
                    "feature_names": item.get("feature_names"),
                    "rank": item.get("rank"),
                }
            )
        commands = [
            {
                "sequence": item.get("sequence"),
                "command": item.get("command"),
                "ok": item.get("ok"),
                "code": item.get("code"),
                "current_plan_id": item.get("current_plan_id"),
                "best_plan_id": item.get("best_plan_id"),
            }
            for item in report.get("commands", [])
        ]
        failure_codes = [
            {
                "code": item.get("code"),
                "message": item.get("message"),
                "details": {
                    key: value
                    for key, value in dict(item.get("details", {})).items()
                    if key not in {"path", "snapshot_version"}
                },
            }
            for item in report.get("failures", [])
            if item.get("code") != "E_SNAPSHOT_CORRUPT"
        ]
        return {
            "dataset_fingerprint": report.get("dataset", {}).get("fingerprint"),
            "experiment": {
                key: value
                for key, value in report.get("experiment", {}).items()
                if key not in {"latency_budget_ms", "max_workers", "serial", "task_delay_ms"}
            },
            "active_plan_id": report.get("active_plan_id"),
            "plans": plans,
            "ranking": report.get("ranking"),
            "best_plan_id": (report.get("best_plan") or {}).get("plan_id"),
            "commands": commands,
            "failures": failure_codes,
        }

    def wait_for_all(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            jobs = [job for job in self._jobs.values() if job.thread.is_alive()]
            if not jobs:
                return True
            for job in jobs:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0.0 and not job.event.is_set():
                    return False
                job.wait(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return all(job.event.is_set() for job in jobs)

    def close(self, *, snapshot: bool = True) -> int:
        if self._closed:
            return self.snapshot_version
        self.wait_for_all()
        self._closed = True
        self._executor.shutdown(wait=True)
        self._snapshot_executor.shutdown(wait=True)
        if snapshot:
            self._write_snapshot()
        return self.snapshot_version

    def close_with_exit_code(self) -> int:
        self.close(snapshot=True)
        return 0
