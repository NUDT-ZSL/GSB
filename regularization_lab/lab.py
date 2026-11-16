"""Experiment orchestration: cache reuse, recovery, decisions and reports."""

from __future__ import annotations

import copy
import math
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .atomic import atomic_write_json, canonical_json
from .cache import ResultCache
from .config import ExperimentConfig, RegularizerSpec, load_experiment
from .data import DataSpec, Dataset, build_dataset, load_data_spec
from .errors import (
    E_CACHE_INVALID,
    E_COMMAND_INVALID,
    E_CONFIG_INVALID,
    Failure,
    business_codes as failure_business_codes,
)
from .fingerprint import cache_key, config_hash, optimizer_fingerprint, training_fingerprint
from .numeric import NumericReferenceStore, compare_numeric_results
from .snapshot import SnapshotStore
from .trainer import DeterministicTrainer

REPORT_SCHEMA_VERSION = 1


def _failure_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    round_value = item.get("round")
    batch_value = item.get("batch_index")
    return (
        round_value if isinstance(round_value, int) else -1,
        batch_value if isinstance(batch_value, int) else -1,
        str(item.get("phase", "")),
        str(item.get("stage", "")),
        str(item.get("business_code", "")),
        str(item.get("scheme") or ""),
        str(item.get("column") or ""),
        str(item.get("message", "")),
        str(item.get("exception_type", "")),
    )


def _round_list(length: int) -> list[int]:
    return list(range(1, length + 1))


class ExperimentSession:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        data_spec: DataSpec | None = None,
        state_dir: str | Path | None = None,
        report_path: str | Path | None = None,
        stop_event: threading.Event | None = None,
    ):
        if state_dir is not None or report_path is not None:
            runtime = replace(
                config.runtime,
                state_dir=Path(state_dir).expanduser().resolve()
                if state_dir is not None
                else config.runtime.state_dir,
                report_path=Path(report_path).expanduser().resolve()
                if report_path is not None
                else config.runtime.report_path,
            )
            config = replace(config, runtime=runtime)
        self.config = config
        self.dataset: Dataset | None = None
        self.data_spec = data_spec or load_data_spec(config.data.spec_path)
        self.dataset, data_failures = build_dataset(config, self.data_spec)
        self.failures: list[dict[str, Any]] = []
        self._failure_keys: set[str] = set()
        for failure in [*config.failures, *data_failures]:
            self.add_failure(failure)

        self.schemes: dict[str, RegularizerSpec] = {
            scheme.name: scheme for scheme in config.regularizers
        }
        self.initial_scheme_names: list[str] = list(self.schemes)
        self.active_scheme_name: str | None = next(iter(self.schemes), None)
        self.results: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.cache_decisions: list[dict[str, Any]] = []
        self._decision_sequence = 0
        self.stop_event = stop_event or threading.Event()
        self.snapshot_store = SnapshotStore(config.runtime.state_dir)
        self.cache = ResultCache(config.runtime.state_dir)
        self.numeric_references = NumericReferenceStore(config.runtime.state_dir)

        self._record_decision(
            "session_initialized",
            schema_version=config.schema_version,
            requested_schemes=self.initial_scheme_names,
            data_fingerprint=self.dataset.data_fingerprint if self.dataset is not None else None,
        )
        if self.dataset is not None:
            for failure in self.snapshot_store.audit_pointer(self.known_config_hashes()):
                self.add_failure(failure)

    @classmethod
    def from_config_path(
        cls,
        config_path: str | Path,
        *,
        state_dir: str | Path | None = None,
        report_path: str | Path | None = None,
        stop_event: threading.Event | None = None,
    ) -> "ExperimentSession":
        return cls(
            load_experiment(config_path),
            state_dir=state_dir,
            report_path=report_path,
            stop_event=stop_event,
        )

    @property
    def report_path(self) -> Path:
        return self.config.runtime.report_path

    def _record_decision(self, action: str, **payload: Any) -> None:
        self._decision_sequence += 1
        self.decisions.append(
            {"sequence": self._decision_sequence, "action": action, **payload}
        )

    def add_failure(self, failure: Failure | dict[str, Any]) -> None:
        item = failure.to_dict() if isinstance(failure, Failure) else dict(failure)
        item.setdefault("details", {})
        if item.get("business_code") == "E_CONFIG_MISMATCH":
            key = canonical_json(
                {
                    "business_code": item.get("business_code"),
                    "phase": item.get("phase"),
                    "stage": item.get("stage"),
                    "snapshot_config_hash": item.get("details", {}).get("snapshot_config_hash"),
                }
            )
        else:
            key = canonical_json(item)
        if key not in self._failure_keys:
            self._failure_keys.add(key)
            self.failures.append(item)

    def record_command_error(self, *, command: str, message: str) -> None:
        self.add_failure(
            Failure(
                business_code=E_COMMAND_INVALID,
                phase="command",
                stage="parse_command",
                message=message,
                exception_type="CommandValidationError",
                details={"command": command},
            )
        )
        self._record_decision("command_rejected", command=command, message=message)

    def _all_failure_objects(self) -> list[Failure]:
        objects: list[Failure] = []
        for item in self.failures:
            objects.append(
                Failure(
                    business_code=str(item.get("business_code", "")),
                    phase=str(item.get("phase", "")),
                    message=str(item.get("message", "")),
                    exception_type=str(item.get("exception_type", "")),
                    scheme=item.get("scheme"),
                    round=item.get("round"),
                    batch_index=item.get("batch_index"),
                    stage=item.get("stage"),
                    column=item.get("column"),
                )
            )
        return objects

    def sorted_failure_dicts(self) -> list[dict[str, Any]]:
        return sorted(self.failures, key=_failure_sort_key)

    def known_config_hashes(self) -> list[str]:
        if self.dataset is None:
            return []
        return [
            config_hash(self.config, self.dataset, scheme)
            for scheme in self.schemes.values()
        ]

    def _identity(self, scheme: RegularizerSpec) -> tuple[str, str, str]:
        assert self.dataset is not None
        return (
            config_hash(self.config, self.dataset, scheme),
            cache_key(scheme, self.dataset.data_fingerprint),
            training_fingerprint(self.config, self.dataset, scheme),
        )

    def _cache_probe(self, scheme: RegularizerSpec) -> dict[str, Any]:
        if self.dataset is None:
            return {
                "cache_key": None,
                "hit": False,
                "reason": "no_dataset",
                "reusable_rounds": [],
                "recompute_rounds": "pending_training",
            }
        _, key, fingerprint = self._identity(scheme)
        lookup = self.cache.lookup(cache_key=key, training_fingerprint=fingerprint)
        for failure in lookup.failures or []:
            self.add_failure(failure)
        if lookup.hit and lookup.entry is not None:
            trajectory = lookup.entry["result"]["loss_trajectory"]
            return {
                "cache_key": key,
                "hit": True,
                "reason": lookup.reason,
                "reusable_rounds": [int(item["round"]) for item in trajectory],
                "recompute_rounds": [],
            }
        return {
            "cache_key": key,
            "hit": False,
            "reason": lookup.reason,
            "reusable_rounds": [],
            "recompute_rounds": "pending_training",
        }

    def switch_scheme(self, name: str) -> bool:
        if name not in self.schemes:
            failure = Failure(
                business_code=E_COMMAND_INVALID,
                phase="command",
                stage="switch_scheme",
                message=f"unknown regularization scheme {name!r}",
                exception_type="CommandValidationError",
                scheme=name,
            )
            self.add_failure(failure)
            self._record_decision("switch_scheme_rejected", requested=name)
            return False
        previous = self.active_scheme_name
        self.active_scheme_name = name
        probe = self._cache_probe(self.schemes[name])
        self._record_decision(
            "switch_scheme",
            previous_scheme=previous,
            scheme=name,
            cache_probe=probe,
        )
        return True

    def probe_active_cache(self) -> dict[str, Any] | None:
        if self.active_scheme_name is None:
            return None
        return self._cache_probe(self.schemes[self.active_scheme_name])

    def set_penalty(self, value: Any) -> bool:
        if self.active_scheme_name is None:
            return False
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0.0:
            failure = Failure(
                business_code=E_CONFIG_INVALID,
                phase="command",
                stage="set_penalty",
                message="penalty must be a non-negative finite number",
                exception_type="CommandValidationError",
                scheme=self.active_scheme_name,
            )
            self.add_failure(failure)
            self._record_decision("set_penalty_rejected", scheme=self.active_scheme_name, requested=value)
            return False
        scheme = self.schemes[self.active_scheme_name]
        updated = replace(scheme, alpha=float(value))
        self.schemes[self.active_scheme_name] = updated
        probe = self._cache_probe(updated)
        self._record_decision(
            "set_penalty",
            scheme=updated.name,
            previous_alpha=scheme.alpha,
            alpha=updated.alpha,
            cache_probe=probe,
        )
        return True

    def set_mix(self, value: Any) -> bool:
        if self.active_scheme_name is None:
            return False
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            failure = Failure(
                business_code=E_CONFIG_INVALID,
                phase="command",
                stage="set_mix",
                message="l1 mixture ratio must be a finite number in [0, 1]",
                exception_type="CommandValidationError",
                scheme=self.active_scheme_name,
            )
            self.add_failure(failure)
            self._record_decision("set_mix_rejected", scheme=self.active_scheme_name, requested=value)
            return False
        scheme = self.schemes[self.active_scheme_name]
        updated = replace(scheme, l1_ratio=float(value))
        self.schemes[self.active_scheme_name] = updated
        probe = self._cache_probe(updated)
        self._record_decision(
            "set_mix",
            scheme=updated.name,
            previous_l1_ratio=scheme.l1_ratio,
            l1_ratio=updated.l1_ratio,
            cache_probe=probe,
        )
        return True

    def _numeric_check(self, result: dict[str, Any]) -> bool:
        fingerprint = str(result["training_fingerprint"])
        reference = self.numeric_references.load(fingerprint)
        if reference is None:
            try:
                self.numeric_references.store(fingerprint, result)
                result["numeric_check"] = {
                    "reference_found": False,
                    "tolerance": self.config.runtime.numeric_tolerance,
                    "within_tolerance": True,
                    "max_abs_loss_difference": 0.0,
                    "max_abs_final_coefficient_difference": 0.0,
                    "structural_mismatch": False,
                }
            except Exception as exc:
                self.add_failure(
                    Failure(
                        business_code=E_CACHE_INVALID,
                        phase="numeric_verification",
                        stage="reference_write",
                        message=f"numeric reference write failed: {exc}",
                        exception_type=type(exc).__name__,
                        scheme=result.get("scheme"),
                    )
                )
            return False
        try:
            audit, failure = compare_numeric_results(
                reference, result, self.config.runtime.numeric_tolerance
            )
        except Exception as exc:
            audit = {
                "reference_found": True,
                "tolerance": self.config.runtime.numeric_tolerance,
                "max_abs_loss_difference": None,
                "max_abs_final_coefficient_difference": None,
                "structural_mismatch": True,
                "within_tolerance": False,
            }
            failure = Failure(
                business_code=E_NUMERIC_DRIFT,
                phase="numeric_verification",
                stage="reference_compare",
                message=f"numeric reference could not be compared: {exc}",
                exception_type=type(exc).__name__,
                scheme=result.get("scheme"),
            )
        result["numeric_check"] = audit
        if failure is not None:
            self.add_failure(failure)
            return True
        return False

    def run_scheme(
        self,
        name: str,
        *,
        pause_after_round: int | None = None,
    ) -> dict[str, Any] | None:
        if name not in self.schemes:
            self.add_failure(
                Failure(
                    business_code=E_COMMAND_INVALID,
                    phase="command",
                    stage="run_scheme",
                    message=f"unknown regularization scheme {name!r}",
                    exception_type="CommandValidationError",
                    scheme=name,
                )
            )
            return None
        if self.dataset is None:
            return None

        scheme = self.schemes[name]
        current_config_hash, current_cache_key, current_training_fingerprint = self._identity(scheme)
        lookup = self.cache.lookup(
            cache_key=current_cache_key,
            training_fingerprint=current_training_fingerprint,
        )
        for failure in lookup.failures or []:
            self.add_failure(failure)

        if lookup.hit and lookup.entry is not None:
            result = copy.deepcopy(lookup.entry["result"])
            trajectory = result.get("loss_trajectory", [])
            origin_rounds = list(result.get("training_executed_rounds", []))
            result["origin_training_executed_rounds"] = origin_rounds
            result["training_executed_rounds"] = []
            result["cache_reused"] = True
            decision = {
                "scheme": name,
                "cache_key": current_cache_key,
                "cache_hit": True,
                "reason": lookup.reason,
                "training_executed": False,
                "reused_rounds": [int(item["round"]) for item in trajectory],
                "recomputed_rounds": [],
                "restored_rounds": [],
                "cache_written": False,
                "resumed": False,
                "snapshot_version": int(result.get("snapshot_version", 0)),
            }
            result["cache_decision"] = decision
            result["cache_reused"] = True
            self.cache_decisions.append(decision)
            self.results[name] = result
            self._record_decision(
                "run_scheme",
                scheme=name,
                cache_key=current_cache_key,
                cache_hit=True,
                training_executed=False,
                reused_rounds=decision["reused_rounds"],
                recomputed_rounds=[],
            )
            return result

        resume_snapshot, snapshot_failures = self.snapshot_store.load_latest(
            config_hash=current_config_hash,
            config_schema_version=self.config.schema_version,
            known_config_hashes=self.known_config_hashes(),
        )
        for failure in snapshot_failures:
            self.add_failure(failure)

        trainer = DeterministicTrainer(
            config=self.config,
            dataset=self.dataset,
            scheme=scheme,
            snapshot_store=self.snapshot_store,
            stop_event=self.stop_event,
        )
        result = trainer.resume_or_train(
            resume_snapshot=resume_snapshot,
            pause_after_round=pause_after_round,
        )
        for item in result.get("failures", []):
            self.add_failure(item)

        resumed_round = result.get("resumed_from_round")
        restored_rounds = (
            _round_list(int(resumed_round)) if isinstance(resumed_round, int) else []
        )
        training_executed_rounds = list(result.get("training_executed_rounds", []))
        trajectory_rounds = [int(item["round"]) for item in result.get("loss_trajectory", [])]
        snapshot_complete_reuse = bool(
            resume_snapshot is not None
            and resume_snapshot.get("status") == "complete"
            and not training_executed_rounds
        )
        if snapshot_complete_reuse:
            reused_rounds = trajectory_rounds
            recomputed_rounds: list[int] = []
        else:
            reused_rounds = []
            recomputed_rounds = training_executed_rounds

        numeric_drift = False
        if result.get("status") == "complete":
            numeric_drift = self._numeric_check(result)

        cache_written = False
        if (
            result.get("status") == "complete"
            and bool(result.get("converged", False))
            and not numeric_drift
        ):
            try:
                self.cache.store(
                    cache_key=current_cache_key,
                    training_fingerprint=current_training_fingerprint,
                    optimizer_fingerprint=optimizer_fingerprint(self.config.optimizer),
                    regularization=scheme.to_dict(),
                    data_fingerprint=self.dataset.data_fingerprint,
                    result=result,
                )
                cache_written = True
            except Exception as exc:
                self.add_failure(
                    Failure(
                        business_code=E_CACHE_INVALID,
                        phase="cache_write",
                        stage="cache_atomic_write",
                        message=f"verified result cache write failed: {exc}",
                        exception_type=type(exc).__name__,
                        scheme=name,
                        round=int(result.get("last_snapshot_round", 0)),
                    )
                )

        decision = {
            "scheme": name,
            "cache_key": current_cache_key,
            "cache_hit": False,
            "reason": lookup.reason,
            "training_executed": bool(training_executed_rounds),
            "reused_rounds": reused_rounds,
            "recomputed_rounds": recomputed_rounds,
            "restored_rounds": restored_rounds,
            "cache_written": cache_written,
            "resumed": bool(restored_rounds),
            "resumed_from_round": resumed_round,
            "resumed_snapshot_version": result.get("resumed_snapshot_version"),
            "snapshot_version": int(result.get("snapshot_version", 0)),
            "snapshot_complete_reuse": snapshot_complete_reuse,
        }
        result["cache_decision"] = decision
        result["cache_reused"] = bool(snapshot_complete_reuse)
        self.cache_decisions.append(decision)
        self.results[name] = result
        self._record_decision(
            "run_scheme",
            scheme=name,
            cache_key=current_cache_key,
            cache_hit=False,
            cache_miss_reason=lookup.reason,
            training_executed=decision["training_executed"],
            restored_rounds=restored_rounds,
            recomputed_rounds=recomputed_rounds,
            snapshot_version=decision["snapshot_version"],
            cache_written=cache_written,
        )
        return result

    def train_active(self, *, pause_after_round: int | None = None) -> dict[str, Any] | None:
        if self.active_scheme_name is None:
            return None
        return self.run_scheme(
            self.active_scheme_name,
            pause_after_round=pause_after_round,
        )

    def run_all(self, *, pause_after_round: int | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in self.initial_scheme_names:
            if self.stop_event.is_set():
                break
            result = self.run_scheme(name, pause_after_round=pause_after_round)
            if result is not None:
                results.append(result)
            if (
                result is not None
                and result.get("status") == "interrupted"
                and pause_after_round is not None
            ):
                break
        return results

    def _invalid_scheme_names(self) -> list[str]:
        names: list[str] = []
        for item in self.failures:
            name = item.get("scheme")
            if isinstance(name, str) and name and name not in self.schemes and name not in names:
                names.append(name)
        return names

    def _overall_status(self) -> str:
        if self.dataset is None:
            return "failed"
        if not self.results:
            return "interrupted" if self.stop_event.is_set() else "no_training"
        if any(result.get("status") == "interrupted" for result in self.results.values()):
            return "interrupted"
        return "complete"

    def _reuse_and_recompute(self) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        for decision in self.cache_decisions:
            latest[str(decision["scheme"])] = {
                "cache_key": decision["cache_key"],
                "cache_hit": decision["cache_hit"],
                "reason": decision["reason"],
                "reused_rounds": decision["reused_rounds"],
                "recomputed_rounds": decision["recomputed_rounds"],
                "restored_rounds": decision["restored_rounds"],
                "resumed_from_round": decision.get("resumed_from_round"),
            }
        return latest

    def build_report(self) -> dict[str, Any]:
        scheme_reports: dict[str, Any] = {}
        for name in self.initial_scheme_names:
            if name in self.results:
                scheme_reports[name] = self.results[name]
            else:
                scheme_reports[name] = {
                    "scheme": name,
                    "regularization": self.schemes[name].to_dict(),
                    "status": "pending",
                    "final_coefficients": None,
                    "loss_trajectory": [],
                    "cache_decision": None,
                }
        for name in self._invalid_scheme_names():
            scheme_reports[name] = {
                "scheme": name,
                "regularization": None,
                "status": "invalid",
                "final_coefficients": None,
                "loss_trajectory": [],
                "cache_decision": None,
            }

        failures = self.sorted_failure_dicts()
        failure_objects = self._all_failure_objects()
        snapshot_versions = {
            name: int(result.get("snapshot_version", 0))
            for name, result in sorted(self.results.items())
        }
        pointer, pointer_failures = self.snapshot_store.read_pointer()
        # Pointer read failures are normally already present; add only new ones.
        for failure in pointer_failures:
            self.add_failure(failure)
        if pointer_failures:
            failures = self.sorted_failure_dicts()
            failure_objects = self._all_failure_objects()

        report = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "status": self._overall_status(),
            "exit_semantics": {
                "graceful_signal_exit_code": 0,
                "forced_termination_report_guaranteed": False,
                "thread_timeout_used": False,
            },
            "requested_schemes": list(self.initial_scheme_names),
            "active_scheme": self.active_scheme_name,
            "regularization_schemes": {
                name: scheme.to_dict() for name, scheme in self.schemes.items()
            },
            "optimizer": {
                "learning_rate": self.config.optimizer.learning_rate,
                "max_iterations": self.config.optimizer.max_iterations,
                "tolerance": self.config.optimizer.tolerance,
                "batch_size": self.config.optimizer.batch_size,
                "fit_intercept": self.config.optimizer.fit_intercept,
            },
            "split": {
                "train_ratio": self.config.split.train_ratio,
                "seed": self.config.split.seed,
            },
            "runtime_policy": {
                "snapshot_commit_interval": self.config.runtime.snapshot_commit_interval,
                "snapshot_policy": "every_completed_round_atomic_replace",
                "numeric_tolerance": self.config.runtime.numeric_tolerance,
                "batch_execution": "thread_pool_fixed_order_aggregation",
            },
            "data": self.dataset.audit if self.dataset is not None else None,
            "data_fingerprint": self.dataset.data_fingerprint if self.dataset is not None else None,
            "config_hashes": {
                name: config_hash(self.config, self.dataset, scheme)
                for name, scheme in self.schemes.items()
            }
            if self.dataset is not None
            else {},
            "schemes": scheme_reports,
            "cache_decisions": self.cache_decisions,
            "reuse_and_recompute": self._reuse_and_recompute(),
            "decision_log": self.decisions,
            "snapshot_versions": snapshot_versions,
            "snapshot_version": max(snapshot_versions.values(), default=0),
            "latest_snapshot_pointer": pointer,
            "failures": failures,
            "business_codes": failure_business_codes(failure_objects),
            "business_code_counts": {
                code: sum(1 for item in failures if item["business_code"] == code)
                for code in failure_business_codes(failure_objects)
            },
        }
        return report

    def write_report(self, path: str | Path | None = None) -> dict[str, Any]:
        report = self.build_report()
        destination = Path(path).expanduser().resolve() if path is not None else self.report_path
        atomic_write_json(destination, report, indent=2)
        return report

    def summary(self) -> dict[str, Any]:
        return {
            "status": self._overall_status(),
            "report_path": str(self.report_path),
            "results": {
                name: {
                    "status": result.get("status"),
                    "converged": result.get("converged"),
                    "iterations_completed": result.get("iterations_completed"),
                    "snapshot_version": result.get("snapshot_version"),
                    "validation_mse": result.get("validation_mse"),
                    "cache_reused": result.get("cache_reused", False),
                }
                for name, result in sorted(self.results.items())
            },
            "business_codes": sorted({item["business_code"] for item in self.failures}),
            "failure_count": len(self.failures),
        }
