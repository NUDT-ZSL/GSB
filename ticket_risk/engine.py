"""Interactive engine: atomic commits, budgeted responses, and recovery."""

from __future__ import annotations

import copy
import math
import threading
import time
from pathlib import Path
from typing import Any

from .commands import ParsedCommand
from .errors import (
    E_COMMAND_INVALID,
    E_INCONSISTENT,
    E_INTERACT_BUDGET_PENDING,
    E_STAGE_EXCEPTION,
    E_WAIT_TIMEOUT,
    Failure,
    LoadBundle,
)
from .loader import RISK_LEVELS, validate_config
from .pipeline import (
    ChangePlan,
    SampleComputation,
    compare_canonical,
    run_full_serial,
    run_parallel_sample_set,
)
from .report import build_report, write_report
from .scoring import result_from_artifacts
from .snapshots import clear_snapshot_dir, restore_snapshot, save_snapshot
from .state import STAGES, RuntimeState, build_reference_index
from .util import round_float


class InteractiveEngine:
    def __init__(
        self,
        bundle: LoadBundle,
        snapshot_dir: str | Path,
        report_path: str | Path | None = None,
        *,
        fresh: bool = False,
        restore: bool = True,
    ) -> None:
        if bundle.fatal or not bundle.samples:
            raise ValueError("cannot start engine with a fatal or empty load bundle")
        self.snapshot_dir = Path(snapshot_dir)
        self.report_path = Path(report_path) if report_path else None
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        if fresh:
            clear_snapshot_dir(self.snapshot_dir)

        self._lock = threading.Condition(threading.RLock())
        self._snapshot_io_lock = threading.Lock()
        self._stop_manager = False
        self._accepting_edits = True
        self._manager_failure: str | None = None
        self._last_snapshot_error: str | None = None
        self._pending_effects: list[ChangePlan] = []
        self._command_records: list[dict[str, Any]] = []
        self._job_progress: dict[str, Any] | None = None
        self._verification: dict[str, Any] | None = None
        self._recovery_info: dict[str, Any] | None = None
        self._commits_since_snapshot = 0

        restored = None
        if restore:
            restored = restore_snapshot(
                self.snapshot_dir,
                base_config_hash=bundle.config_hash,
                base_resource_fingerprint=bundle.resource_fingerprint,
                samples_fingerprint=bundle.samples_fingerprint,
                samples=bundle.samples,
            )

        if restored is not None:
            state, recovery_info = restored
            self._committed = state
            self._summary_cache = state.summary()
            self._recovery_info = recovery_info
            self._last_snapshot_version = int(
                recovery_info["recovery_point"]["global_version"]
            )
            self._command_seq = state.command_count
        else:
            state = self._run_initial_full(bundle)
            self._committed = state
            self._summary_cache = state.summary()
            self._last_snapshot_version = state.version
            self._command_seq = 0
            manifest = self._save_snapshot(state)
            self._last_snapshot_version = int(manifest["global_version"])
            self._recovery_info = {
                "recovered": False,
                "recovery_point": {
                    "global_version": state.version,
                    "stage_versions": dict(state.stage_versions),
                    "completed_sample_count": len(state.completed_sample_ids()),
                },
            }

        self._desired_config = copy.deepcopy(self._committed.config)
        self._desired_rules = copy.deepcopy(self._committed.rules)
        self._target_version = self._committed.version
        self._reference_index = build_reference_index(
            self._committed.samples, self._desired_rules
        )
        self._manager_thread = threading.Thread(
            target=self._manager_loop,
            name="determination-recompute-manager",
            daemon=True,
        )
        self._manager_thread.start()
        self._write_current_report()

    def _run_initial_full(self, bundle: LoadBundle) -> RuntimeState:
        computations, _ = run_parallel_sample_set(
            bundle.samples,
            bundle.config,
            bundle.rules,
            None,
            None,
            int(bundle.config["worker_threads"]),
        )
        artifacts: dict[str, dict[str, dict[str, Any]]] = {}
        failures = list(bundle.failures)
        for sample in bundle.samples:
            computation = computations[sample.sample_id]
            artifacts[sample.sample_id] = computation.artifacts
            if computation.failure is not None:
                failures.append(computation.failure)
        failures.sort(key=lambda item: item.sort_key())
        samples = sorted(bundle.samples, key=lambda item: item.sample_id)
        stage_versions = {stage: 1 for stage in STAGES}
        audit = {
            "command_version": 0,
            "reason": "initial_full_build",
            "affected_sample_ids": [sample.sample_id for sample in samples],
            "affected_stage_counts": {
                stage: sum(
                    sample.sample_id in artifacts
                    and stage in artifacts[sample.sample_id]
                    for sample in samples
                )
                for stage in STAGES
            },
            "reused_sample_count": 0,
            "recomputed_sample_count": len(samples),
            "reused_sample_ids": [],
            "recomputed_sample_ids": [sample.sample_id for sample in samples],
            "reused_stage_count": 0,
            "recomputed_stage_count": len(samples) * len(STAGES),
            "computed_stage_counts": {stage: len(samples) for stage in STAGES},
            "reused_stage_counts": {stage: 0 for stage in STAGES},
            "job_duration_ms": 0.0,
        }
        return RuntimeState(
            version=0,
            command_count=0,
            samples=samples,
            rules=copy.deepcopy(bundle.rules),
            config=copy.deepcopy(bundle.config),
            artifacts=artifacts,
            failures=failures,
            base_config_hash=bundle.config_hash,
            base_resource_fingerprint=bundle.resource_fingerprint,
            samples_fingerprint=bundle.samples_fingerprint,
            stage_versions=stage_versions,
            last_audit=audit,
        )

    def _save_snapshot(self, state: RuntimeState) -> dict[str, Any]:
        with self._snapshot_io_lock:
            return save_snapshot(state, self.snapshot_dir)

    @property
    def recovery_info(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._recovery_info)

    @property
    def snapshot_version(self) -> int:
        with self._lock:
            return self._last_snapshot_version

    @property
    def state_snapshot(self) -> RuntimeState:
        """Committed state. Workers never mutate artifacts after commit."""

        with self._lock:
            return self._committed

    def _plan_for_edit(
        self,
        command: ParsedCommand,
        version: int,
    ) -> ChangePlan:
        plan = ChangePlan(command_version=version)
        refs = self._reference_index
        kind = command.kind
        args = command.args
        if kind == "set_weight":
            name = str(args[0])
            if name == "lexicon_weight_scale":
                plan.add(refs["sentiment_samples"], {"sentiment", "fusion"})
                plan.reason = "sentiment_weight_scale_changed"
            elif name == "risk_weight_scale":
                plan.add(refs["risk_samples"], {"risk", "fusion"})
                plan.reason = "risk_weight_scale_changed"
            elif name == "position_decay":
                plan.add(refs["sentiment_samples"], {"sentiment", "fusion"})
                plan.add(refs["risk_samples"], {"risk", "fusion"})
                plan.reason = "position_decay_changed"
            elif name == "fusion_temperature":
                plan.add(refs["all_samples"], {"fusion"})
                plan.reason = "fusion_temperature_changed"
        elif kind == "set_threshold":
            plan.add(refs["all_samples"], {"risk", "fusion"})
            plan.reason = "risk_threshold_changed"
        elif kind == "set_lexicon":
            word = str(args[0])
            plan.add(refs["sentiment_words"].get(word, set()), {"sentiment", "fusion"})
            plan.reason = f"sentiment_term_changed:{word}"
        elif kind == "set_risk_keyword":
            keyword = str(args[0])
            plan.add(refs["risk_keywords"].get(keyword, set()), {"risk", "fusion"})
            plan.reason = f"risk_keyword_changed:{keyword}"
        elif kind == "set_degree":
            word = str(args[0])
            plan.add(refs["degree_words"].get(word, set()), {"sentiment", "fusion"})
            plan.reason = f"degree_term_changed:{word}"
        elif kind == "set_risk_level_weight":
            level = str(args[0])
            plan.add(refs["risk_levels"].get(level, set()), {"risk", "fusion"})
            plan.reason = f"risk_level_weight_changed:{level}"
        elif kind == "set_negation_window":
            plan.add(refs["sentiment_samples"], {"sentiment", "fusion"})
            plan.reason = "negation_window_changed"
        return plan

    def _mutate_desired(self, command: ParsedCommand) -> tuple[dict[str, Any], dict[str, Any]]:
        config = copy.deepcopy(self._desired_config)
        rules = copy.deepcopy(self._desired_rules)
        kind = command.kind
        args = command.args
        if kind == "set_weight":
            config[str(args[0])] = float(args[1])
            config = validate_config(config)
        elif kind == "set_threshold":
            config["risk_level_thresholds"][str(args[0])] = float(args[1])
            config = validate_config(config)
        elif kind == "set_negation_window":
            config["negation_window"] = int(args[0])
            config = validate_config(config)
        elif kind == "set_lexicon":
            word, weight = str(args[0]), float(args[1])
            if word not in rules["sentiment_lexicon"]:
                raise ValueError(f"{E_COMMAND_INVALID}: unknown sentiment word: {word}")
            if not math.isfinite(weight) or not -1 <= weight <= 1:
                raise ValueError(f"{E_COMMAND_INVALID}: sentiment weight must be in [-1,1]")
            rules["sentiment_lexicon"][word]["weight"] = weight
        elif kind == "set_risk_keyword":
            keyword, weight = str(args[0]), float(args[1])
            if keyword not in rules["risk_keywords"]:
                raise ValueError(
                    f"{E_COMMAND_INVALID}: unknown risk keyword: {keyword}"
                )
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(f"{E_COMMAND_INVALID}: risk weight must be non-negative")
            rules["risk_keywords"][keyword]["weight"] = weight
        elif kind == "set_degree":
            word, factor = str(args[0]), float(args[1])
            if word not in rules["degree_adverbs"]:
                raise ValueError(f"{E_COMMAND_INVALID}: unknown degree word: {word}")
            if not math.isfinite(factor) or factor < 0:
                raise ValueError(f"{E_COMMAND_INVALID}: degree factor must be non-negative")
            rules["degree_adverbs"][word]["factor"] = factor
        elif kind == "set_risk_level_weight":
            level, weight = str(args[0]), float(args[1])
            if level not in rules["risk_level_map"]:
                raise ValueError(f"{E_COMMAND_INVALID}: unknown risk level: {level}")
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(f"{E_COMMAND_INVALID}: level weight must be non-negative")
            rules["risk_level_map"][level]["weight"] = weight
        else:
            raise ValueError(f"{E_COMMAND_INVALID}: command is not an edit: {kind}")
        return config, rules

    def apply_edit(self, command: ParsedCommand) -> dict[str, Any]:
        started = time.monotonic()
        with self._lock:
            if not self._accepting_edits:
                return self._error_response(
                    command.raw, E_COMMAND_INVALID, "engine is shutting down", started
                )
            try:
                new_config, new_rules = self._mutate_desired(command)
            except Exception as exc:
                response = self._error_response(command.raw, E_COMMAND_INVALID, str(exc), started)
                self._record_command(response)
                return response

            self._desired_config = new_config
            self._desired_rules = new_rules
            self._command_seq += 1
            version = self._command_seq
            plan = self._plan_for_edit(command, version)
            self._pending_effects.append(plan)
            self._target_version = version
            self._verification = None
            self._lock.notify_all()
            budget_ms = float(self._desired_config["interaction_budget_ms"])
            # Reserve scheduling headroom so the response can actually be
            # emitted before the configured deadline even while worker threads
            # are competing for the GIL.
            safety_margin = min(0.060, budget_ms / 1000.0 * 0.30)
            deadline = started + max(0.0, budget_ms / 1000.0 - safety_margin)
            while self._committed.version < version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            pending = self._committed.version < version
            summary = self._summary_cache
            pending_progress = self._pending_progress_snapshot(version)
            latency_ms = round_float((time.monotonic() - started) * 1000.0, 6)
            codes = [E_INTERACT_BUDGET_PENDING] if pending else []
            response = {
                "ok": True,
                "event": "edit_response",
                "command": command.raw,
                "command_seq": version,
                "target_version": version,
                "latest_completed_version": self._committed.version,
                "snapshot_version": self._last_snapshot_version,
                "business_codes": codes,
                "budget_ms": budget_ms,
                "latency_ms": latency_ms,
                "over_budget": latency_ms > budget_ms,
                "monotonic_clock": True,
                "pending": pending,
                "summary": summary,
                "pending_progress": pending_progress,
                "affected_sample_ids": plan.affected_sample_ids,
            }
            response["command_record"] = self._record_command(response)
            return response

    def _pending_progress_snapshot(self, target_version: int) -> dict[str, Any] | None:
        if self._committed.version >= target_version:
            return None
        if (
            self._job_progress is not None
            and self._job_progress.get("target_version") == target_version
        ):
            return copy.deepcopy(self._job_progress)
        effects = [effect for effect in self._pending_effects if effect.command_version <= target_version]
        union: dict[str, set[str]] = {}
        reasons: list[str] = []
        for effect in effects:
            for sample_id, stages in effect.affected_stages.items():
                union.setdefault(sample_id, set()).update(stages)
            if effect.reason:
                reasons.append(effect.reason)
        total = len(self._committed.samples)
        affected = sorted(union)
        return {
            "target_version": target_version,
            "started": False,
            "affected_sample_ids": affected,
            "affected_stage_counts": {
                stage: sum(stage in stages for stages in union.values()) for stage in STAGES
            },
            "completed_sample_count": total - len(affected),
            "total_sample_count": total,
            "recomputed_sample_count": 0,
            "reuse_sample_count": total - len(affected),
            "progress": {
                "completed": total - len(affected),
                "total": total,
                "percent": round(100.0 * (total - len(affected)) / total, 4) if total else 100.0,
                "partial": True,
            },
            "reasons": reasons,
        }

    def _error_response(
        self,
        command: str,
        code: str,
        detail: str,
        started: float,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "command": command,
            "business_codes": [code],
            "detail": detail,
            "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
            "over_budget": False,
            "pending": False,
        }

    def _record_command(self, response: dict[str, Any]) -> dict[str, Any]:
        record = {
            "command": response.get("command"),
            "command_seq": response.get("command_seq"),
            "target_version": response.get("target_version"),
            "ok": response.get("ok", True),
            "business_codes": list(response.get("business_codes", [])),
            "latency_ms": response.get("latency_ms"),
            "budget_ms": response.get("budget_ms"),
            "over_budget": response.get("over_budget"),
            "pending_at_response": response.get("pending"),
            "affected_sample_ids": list(response.get("affected_sample_ids", [])),
            "committed_version_at_response": response.get("latest_completed_version"),
            "budget_excluded": bool(response.get("budget_excluded", False)),
        }
        target = response.get("target_version")
        latest = response.get("latest_completed_version")
        if isinstance(target, int) and isinstance(latest, int) and latest >= target:
            record["committed_version"] = latest
        self._command_records.append(record)
        return record

    def status(self) -> dict[str, Any]:
        started = time.monotonic()
        with self._lock:
            response = {
                "ok": True,
                "event": "status",
                "latest_completed_version": self._committed.version,
                "target_version": self._target_version,
                "snapshot_version": self._last_snapshot_version,
                "pending": self._committed.version < self._target_version,
                "summary": self._summary_cache,
                "pending_progress": self._pending_progress_snapshot(self._target_version),
                "last_snapshot_error": self._last_snapshot_error,
                "manager_failure": self._manager_failure,
                "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
                "budget_excluded": True,
            }
            self._record_command(response)
            return response

    def wait_idle(self, timeout_seconds: float | None = None) -> bool:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._lock:
            while self._committed.version < self._target_version or self._pending_effects:
                if self._manager_failure:
                    raise RuntimeError(self._manager_failure)
                if deadline is None:
                    self._lock.wait(0.05)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(min(remaining, 0.05))
            return True

    def wait_command(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        started = time.monotonic()
        complete = self.wait_idle(timeout_seconds)
        with self._lock:
            codes = [] if complete else [E_WAIT_TIMEOUT]
            response = {
                "ok": complete,
                "event": "wait",
                "business_codes": codes,
                "latest_completed_version": self._committed.version,
                "target_version": self._target_version,
                "pending": self._committed.version < self._target_version,
                "summary": self._summary_cache,
                "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
                "budget_excluded": True,
            }
            self._record_command(response)
            self._write_current_report()
            return response

    def force_snapshot(self, wait: bool = True, timeout_seconds: float = 30.0) -> dict[str, Any]:
        started = time.monotonic()
        complete = self.wait_idle(timeout_seconds) if wait else True
        if not complete:
            return {
                "ok": False,
                "event": "snapshot",
                "business_codes": [E_WAIT_TIMEOUT],
                "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
                "budget_excluded": True,
            }
        with self._lock:
            state = self._committed
        manifest = self._save_snapshot(state)
        with self._lock:
            self._last_snapshot_version = int(manifest["global_version"])
            self._commits_since_snapshot = 0
        self._write_current_report()
        return {
            "ok": True,
            "event": "snapshot",
            "global_version": manifest["global_version"],
            "config_hash": manifest["config_hash"],
            "resource_fingerprint": manifest["resource_fingerprint"],
            "completed_sample_count": len(manifest["completed_sample_ids"]),
            "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
            "budget_excluded": True,
        }

    def verify_consistency(self, timeout_seconds: float = 120.0) -> dict[str, Any]:
        started = time.monotonic()
        if not self.wait_idle(timeout_seconds):
            return {
                "ok": False,
                "event": "verify",
                "business_codes": [E_WAIT_TIMEOUT],
                "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
                "budget_excluded": True,
            }
        with self._lock:
            state = self._committed
            loader_failures = [
                failure for failure in state.failures if failure.stage is None
            ]
            samples = list(state.samples)
            config = copy.deepcopy(state.config)
            rules = copy.deepcopy(state.rules)
            incremental_payload = state.canonical_payload()
        full_state = run_full_serial(samples, config, rules, loader_failures)
        full_payload = full_state.canonical_payload()
        differences = compare_canonical(incremental_payload, full_payload)
        consistent = not differences
        result = {
            "ok": consistent,
            "event": "verify",
            "business_codes": [] if consistent else [E_INCONSISTENT],
            "consistent": consistent,
            "differences": differences,
            "verified_state_version": state.version,
            "sample_count": len(incremental_payload["samples"]),
            "failure_count": len(incremental_payload["failures"]),
            "latency_ms": round_float((time.monotonic() - started) * 1000.0, 6),
            "budget_excluded": True,
        }
        with self._lock:
            self._verification = copy.deepcopy(result)
            self._record_command(result)
            self._write_current_report()
        return result

    def write_report_now(self) -> str | None:
        with self._lock:
            self._write_current_report()
            return str(self.report_path) if self.report_path else None

    def _write_current_report(self) -> None:
        if self.report_path is None:
            return
        with self._lock:
            state = self._committed
            command_records = copy.deepcopy(self._command_records)
            last_snapshot_version = self._last_snapshot_version
            pending = self._pending_progress_snapshot(self._target_version)
            recovery_info = copy.deepcopy(self._recovery_info)
            verification = copy.deepcopy(self._verification)
            snapshot_error = self._last_snapshot_error
        report = build_report(
            state,
            command_records=command_records,
            last_snapshot_version=last_snapshot_version,
            pending=pending,
            recovery_info=recovery_info,
            verification=verification,
            snapshot_error=snapshot_error,
        )
        write_report(self.report_path, report)

    def _manager_loop(self) -> None:
        while True:
            with self._lock:
                while not self._stop_manager and self._committed.version >= self._target_version:
                    self._lock.wait(0.05)
                if self._stop_manager:
                    return
                job_version = self._target_version
                job_config = copy.deepcopy(self._desired_config)
                job_rules = copy.deepcopy(self._desired_rules)
                base_state = self._committed
                effects = [
                    effect
                    for effect in self._pending_effects
                    if effect.command_version <= job_version
                ]
                union = ChangePlan(job_version)
                for effect in effects:
                    union.merge(effect)
                stage_plan = union.affected_stages
                affected_ids = sorted(stage_plan)
                affected_set = set(affected_ids)
                samples_by_id = {sample.sample_id: sample for sample in base_state.samples}
                selected = [samples_by_id[sample_id] for sample_id in affected_ids]
                total_samples = len(base_state.samples)
            base_distribution = {level: 0 for level in RISK_LEVELS}
            for sample in base_state.samples:
                if sample.sample_id in affected_set:
                    continue
                fusion = base_state.artifacts.get(sample.sample_id, {}).get("fusion")
                if fusion is not None:
                    base_distribution[fusion["final_risk_level"]] += 1
            progress: dict[str, Any] = {
                "target_version": job_version,
                "started": True,
                "affected_sample_ids": affected_ids,
                "affected_stage_counts": {
                    stage: sum(stage in stages for stages in stage_plan.values())
                    for stage in STAGES
                },
                "completed_sample_count": total_samples - len(affected_ids),
                "total_sample_count": total_samples,
                "recomputed_sample_count": 0,
                "reuse_sample_count": total_samples - len(affected_ids),
                "risk_distribution": base_distribution,
                "progress": {
                    "completed": total_samples - len(affected_ids),
                    "total": total_samples,
                    "percent": round(
                        100.0 * (total_samples - len(affected_ids)) / total_samples, 4
                    )
                    if total_samples
                    else 100.0,
                    "partial": True,
                },
                "reasons": [union.reason] if union.reason else [],
            }
            with self._lock:
                if self._target_version != job_version:
                    continue
                self._job_progress = progress

            cancel_event = threading.Event()
            started = time.monotonic()
            progress_counter = 0
            progress_batch_every = max(1, len(selected) // 40)
            batch_completed = 0
            batch_distribution = {level: 0 for level in RISK_LEVELS}

            def on_progress(computation: SampleComputation) -> None:
                nonlocal progress_counter, batch_completed
                if self._target_version != job_version:
                    cancel_event.set()
                    return
                progress_counter += 1
                batch_completed += 1
                if computation.failure is None and all(
                    stage in computation.artifacts for stage in STAGES
                ):
                    sample = samples_by_id[computation.sample_id]
                    result = result_from_artifacts(sample, computation.artifacts)
                    batch_distribution[result["risk_level"]] += 1
                if (
                    progress_counter % progress_batch_every != 0
                    and progress_counter != len(selected)
                ):
                    return
                with self._lock:
                    if self._target_version != job_version:
                        cancel_event.set()
                        return
                    progress["completed_sample_count"] += batch_completed
                    progress["progress"]["completed"] = progress["completed_sample_count"]
                    progress["progress"]["percent"] = round(
                        100.0
                        * progress["completed_sample_count"]
                        / max(1, progress["total_sample_count"]),
                        4,
                    )
                    for level in RISK_LEVELS:
                        progress["risk_distribution"][level] += batch_distribution[level]
                        batch_distribution[level] = 0
                    batch_completed = 0

            try:
                computations, cancelled = run_parallel_sample_set(
                    selected,
                    job_config,
                    job_rules,
                    base_state.artifacts,
                    stage_plan,
                    int(job_config["worker_threads"]),
                    on_progress,
                    cancel_event,
                )
            except Exception as exc:
                with self._lock:
                    self._manager_failure = f"recompute manager failed: {type(exc).__name__}: {exc}"
                    self._job_progress = None
                    self._lock.notify_all()
                return

            with self._lock:
                if self._target_version != job_version or cancelled:
                    continue

            artifacts: dict[str, dict[str, dict[str, Any]]] = {}
            task_failures: list[Failure] = []
            computed_stage_names: set[str] = set()
            computed_stage_counts = {stage: 0 for stage in STAGES}
            reused_stage_counts = {stage: 0 for stage in STAGES}
            recomputed_sample_ids: list[str] = []
            reused_sample_ids: list[str] = []
            for sample in base_state.samples:
                sample_id = sample.sample_id
                if sample_id not in affected_set:
                    artifacts[sample_id] = base_state.artifacts.get(sample_id, {})
                    reused_sample_ids.append(sample_id)
                    for stage in STAGES:
                        if stage in artifacts[sample_id]:
                            reused_stage_counts[stage] += 1
                    continue
                computation = computations.get(sample_id)
                if computation is None:
                    failure = Failure(
                        business_code=E_STAGE_EXCEPTION,
                        detail="sample task did not produce a result",
                        sample_id=sample_id,
                        line=sample.line,
                        stage="pipeline",
                        exception_type="MissingTaskResult",
                        scope="sample_stage",
                    )
                    artifacts[sample_id] = base_state.artifacts.get(sample_id, {})
                    task_failures.append(failure)
                    continue
                artifacts[sample_id] = computation.artifacts
                if computation.failure is not None:
                    task_failures.append(computation.failure)
                if computation.computed_stages:
                    recomputed_sample_ids.append(sample_id)
                    for stage in computation.computed_stages:
                        computed_stage_names.add(stage)
                        computed_stage_counts[stage] += 1
                else:
                    reused_sample_ids.append(sample_id)
                for stage in computation.reused_stages:
                    reused_stage_counts[stage] += 1

            preserved_failures = [
                failure
                for failure in base_state.failures
                if failure.scope != "sample_stage"
                or (failure.sample_id not in affected_set)
            ]
            failures = preserved_failures + task_failures
            failures.sort(key=lambda item: item.sort_key())
            stage_versions = dict(base_state.stage_versions)
            for stage in computed_stage_names:
                stage_versions[stage] += 1
            duration_ms = round_float((time.monotonic() - started) * 1000.0, 6)
            audit = {
                "command_version": job_version,
                "reason": union.reason or "coalesced_noop",
                "affected_sample_ids": affected_ids,
                "affected_stage_counts": progress["affected_stage_counts"],
                "reused_sample_count": len(reused_sample_ids),
                "recomputed_sample_count": len(recomputed_sample_ids),
                "reused_sample_ids": sorted(reused_sample_ids),
                "recomputed_sample_ids": sorted(recomputed_sample_ids),
                "reused_stage_count": sum(reused_stage_counts.values()),
                "recomputed_stage_count": sum(computed_stage_counts.values()),
                "computed_stage_counts": computed_stage_counts,
                "reused_stage_counts": reused_stage_counts,
                "job_duration_ms": duration_ms,
            }
            new_state = RuntimeState(
                version=job_version,
                command_count=job_version,
                samples=base_state.samples,
                rules=job_rules,
                config=job_config,
                artifacts=artifacts,
                failures=failures,
                base_config_hash=base_state.base_config_hash,
                base_resource_fingerprint=base_state.base_resource_fingerprint,
                samples_fingerprint=base_state.samples_fingerprint,
                stage_versions=stage_versions,
                last_audit=audit,
            )
            new_summary = new_state.summary()

            with self._lock:
                if self._target_version != job_version:
                    continue
                self._committed = new_state
                self._summary_cache = new_summary
                self._pending_effects = [
                    effect for effect in self._pending_effects if effect.command_version > job_version
                ]
                self._job_progress = None
                for record in self._command_records:
                    target = record.get("target_version")
                    if isinstance(target, int) and target <= job_version and not record.get("committed_version"):
                        record["committed_version"] = job_version
                self._lock.notify_all()

            self._commits_since_snapshot += 1
            with self._lock:
                # During a burst of edits, defer heavy snapshot/report I/O until
                # the command stream becomes quiet.  This keeps the interactive
                # thread inside its budget and lets superseded generations be
                # discarded without writing misleading snapshots.
                quiet_deadline = time.monotonic() + 0.020
                while (
                    self._target_version == job_version
                    and not self._stop_manager
                    and time.monotonic() < quiet_deadline
                ):
                    self._lock.wait(max(0.0, quiet_deadline - time.monotonic()))
                if self._target_version != job_version:
                    continue
            interval = int(job_config["snapshot_commit_interval"])
            if self._commits_since_snapshot >= interval:
                try:
                    manifest = self._save_snapshot(new_state)
                    with self._lock:
                        self._last_snapshot_version = int(manifest["global_version"])
                        self._last_snapshot_error = None
                    self._commits_since_snapshot = 0
                except Exception as exc:
                    with self._lock:
                        self._last_snapshot_error = (
                            f"snapshot failed: {type(exc).__name__}: {exc}"
                        )
            self._write_current_report()

    def graceful_shutdown(self, timeout_seconds: float = 120.0) -> dict[str, Any]:
        with self._lock:
            self._accepting_edits = False
        complete = self.wait_idle(timeout_seconds)
        if not complete:
            raise TimeoutError("timed out waiting for the current command before exit")
        with self._lock:
            state = self._committed
        manifest = self._save_snapshot(state)
        with self._lock:
            self._last_snapshot_version = int(manifest["global_version"])
            self._commits_since_snapshot = 0
        self._write_current_report()
        result = {
            "ok": True,
            "event": "shutdown",
            "exit_code": 0,
            "snapshot_version": manifest["global_version"],
            "config_hash": manifest["config_hash"],
            "resource_fingerprint": manifest["resource_fingerprint"],
            "completed_sample_count": len(manifest["completed_sample_ids"]),
            "report": str(self.report_path) if self.report_path else None,
        }
        with self._lock:
            self._stop_manager = True
            self._lock.notify_all()
        self._manager_thread.join(timeout=2.0)
        return result
