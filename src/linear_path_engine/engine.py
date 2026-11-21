"""Four-stage training state machine with resumable whole-snapshot commits."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .config import load_config
from .data import load_dataset
from .errors import (
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STATE,
    E_VERSION_MISMATCH,
    EngineError,
)
from .planner import ChunkPlan, plan_chunks
from .snapshot import Snapshot, SnapshotStore
from .solver import ComparisonBudget, PathSolver
from .standardization import (
    StandardizedData,
    fit_standardization,
    stats_content_version,
    transform_with_stats,
)
from .util import atomic_write_json, sha256_value
from .validation import validate_path


PHASE_STANDARDIZATION = "standardization"
PHASE_PATH_SOLVE = "path_solve"
PHASE_PATH_VALIDATION = "path_validation"
PHASE_PATH_SUMMARY = "path_summary"

_PHASE_RANK = {
    PHASE_STANDARDIZATION: 1,
    PHASE_PATH_SOLVE: 2,
    PHASE_PATH_VALIDATION: 3,
    PHASE_PATH_SUMMARY: 4,
}


class TrainingEngine:
    def __init__(self, config_path: str | Path) -> None:
        self.config = load_config(config_path)
        self.dataset = load_dataset(self.config.data)
        self.store = (
            SnapshotStore(self.config.snapshots.directory, self.config.run_id)
            if self.config.snapshots.enabled
            else None
        )
        self.state: dict[str, Any] | None = None
        self.version = 0
        self.checksum: str | None = None

    def _resource_error(self, message: str, **details: Any) -> EngineError:
        return EngineError(
            E_RESOURCE_LIMIT,
            message,
            stage="resource_preflight",
            details=details,
        )

    def preflight_limits(self) -> None:
        config = self.config
        path_size = len(config.model.strengths_ascending)
        if path_size > config.execution.max_path_size:
            raise self._resource_error(
                "regularization path size exceeds max_path_size",
                path_size=path_size,
                max_path_size=config.execution.max_path_size,
            )
        for ascending_index in range(path_size):
            iterations = config.solver.iterations_for(ascending_index)
            if iterations > config.execution.max_iterations_per_strength:
                raise self._resource_error(
                    "per-strength iteration cap exceeds max_iterations_per_strength",
                    strength_index=ascending_index,
                    strength=config.model.strengths_ascending[ascending_index],
                    requested_iterations=iterations,
                    max_iterations_per_strength=config.execution.max_iterations_per_strength,
                )

    def _corrupt_state(
        self, message: str, *, version: int | None = None, details: dict[str, Any] | None = None
    ) -> EngineError:
        return EngineError(
            E_SNAPSHOT_CORRUPT,
            message,
            stage=self.state.get("phase", "snapshot") if self.state else "snapshot",
            version=self.version if version is None else version,
            details=details,
        )

    def _restore_state(self, snapshot: Snapshot) -> None:
        state = snapshot.payload
        self.state = state
        self.version = snapshot.version
        self.checksum = snapshot.checksum
        required = {
            "state_schema_version",
            "run_id",
            "config_fingerprint",
            "data_fingerprint",
            "phase",
            "phase_complete",
        }
        missing = sorted(required - set(state))
        if missing:
            raise self._corrupt_state(
                f"snapshot state is missing fields: {missing}",
                details={"missing_fields": missing},
            )
        if state["state_schema_version"] != 1:
            raise self._corrupt_state(
                "unsupported snapshot state schema version",
                details={"state_schema_version": state["state_schema_version"]},
            )
        if state["phase"] not in _PHASE_RANK:
            raise self._corrupt_state(
                "snapshot phase is unknown",
                details={"phase": state["phase"]},
            )
        if state["run_id"] != self.config.run_id:
            raise EngineError(
                E_VERSION_MISMATCH,
                "snapshot run_id does not match the configuration",
                stage=self.state["phase"],
                version=self.version,
            )
        if state["config_fingerprint"] != self.config.fingerprint:
            raise EngineError(
                E_VERSION_MISMATCH,
                "configuration changed relative to the committed snapshot",
                stage=self.state["phase"],
                version=self.version,
                details={
                    "snapshot_config_fingerprint": state["config_fingerprint"],
                    "current_config_fingerprint": self.config.fingerprint,
                },
            )
        if state["data_fingerprint"] != self.dataset.fingerprint:
            raise EngineError(
                E_VERSION_MISMATCH,
                "dataset changed relative to the committed snapshot",
                stage=self.state["phase"],
                version=self.version,
                details={
                    "snapshot_data_fingerprint": state["data_fingerprint"],
                    "current_data_fingerprint": self.dataset.fingerprint,
                },
            )
        if _PHASE_RANK[state["phase"]] >= _PHASE_RANK[PHASE_STANDARDIZATION]:
            standardization_doc = state.get("standardization")
            if not isinstance(standardization_doc, dict):
                raise self._corrupt_state("standardization state is missing")
            stats = standardization_doc.get("stats")
            stats_version = standardization_doc.get("stats_version")
            if not isinstance(stats, dict) or not isinstance(stats_version, str):
                raise self._corrupt_state("standardization statistics are incomplete")
            actual_version = stats_content_version(stats)
            if actual_version != stats_version:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    "standardization statistics checksum no longer matches its version",
                    stage=self.state["phase"],
                    version=self.version,
                    details={
                        "declared_stats_version": stats_version,
                        "content_stats_version": actual_version,
                    },
                )
            plan_doc = state.get("chunk_plan")
            if not isinstance(plan_doc, dict):
                raise self._corrupt_state("chunk plan state is missing")
            if plan_doc.get("precision_mode") != self.config.precision.mode:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    "committed chunk plan precision mode differs from the configuration",
                    stage=self.state["phase"],
                    version=self.version,
                )
        path_entries = state.get("path", [])
        if not isinstance(path_entries, list) or not all(
            isinstance(entry, dict) for entry in path_entries
        ):
            raise self._corrupt_state("snapshot path is not an array of objects")
        if len(path_entries) > len(self.config.model.strengths_ascending):
            raise self._corrupt_state(
                "snapshot path contains more strengths than configured",
                details={
                    "path_length": len(path_entries),
                    "configured_path_length": len(self.config.model.strengths_ascending),
                },
            )
        solve_order = state.get("solve_order_indices")
        expected_solve_order = list(reversed(range(len(self.config.model.strengths_ascending))))
        if solve_order != expected_solve_order:
            raise self._corrupt_state(
                "snapshot solve order is not the configured descending path",
                details={
                    "snapshot_solve_order": solve_order,
                    "expected_solve_order": expected_solve_order,
                },
            )
        for position, entry in enumerate(path_entries):
            if entry.get("solve_position") != position:
                raise self._corrupt_state(
                    "snapshot path entry has an invalid solve position",
                    details={"position": position, "entry": entry.get("solve_position")},
                )
            expected_strength_index = expected_solve_order[position]
            if entry.get("strength_index") != expected_strength_index:
                raise self._corrupt_state(
                    "snapshot path entry has an invalid strength index",
                    details={
                        "position": position,
                        "entry_strength_index": entry.get("strength_index"),
                        "expected_strength_index": expected_strength_index,
                    },
                )
        if state["phase"] == PHASE_PATH_VALIDATION and not isinstance(
            state.get("validation"), dict
        ):
            raise self._corrupt_state("validation phase snapshot has no validation report")
        if state["phase"] == PHASE_PATH_SUMMARY and not isinstance(
            state.get("result_document"), dict
        ):
            raise self._corrupt_state("summary phase snapshot has no result document")

    def _load_existing(self) -> bool:
        if self.store is None:
            return False
        current = self.store.load_current()
        if current is None:
            return False
        if not self.config.snapshots.resume:
            raise EngineError(
                E_SNAPSHOT_ROLLBACK,
                "an existing committed snapshot cannot be overwritten when resume is disabled",
                stage=current.stage,
                version=current.version,
            )
        self._restore_state(current)
        return True

    def _commit(
        self,
        *,
        stage: str,
        stage_detail: str | None,
        phase_complete: bool = True,
    ) -> None:
        if self.state is None:
            raise EngineError(E_STATE, "cannot commit without state", stage=stage)
        self.state["phase"] = stage
        self.state["phase_complete"] = phase_complete
        if self.store is None:
            self.version += 1
            return
        snapshot = self.store.commit(
            payload=self.state,
            stage=stage,
            stage_detail=stage_detail,
            expected_version=self.version,
            expected_checksum=self.checksum,
        )
        self.version = snapshot.version
        self.checksum = snapshot.checksum

    def _new_state(self, standardized: StandardizedData, plan: ChunkPlan) -> dict[str, Any]:
        solve_order = list(reversed(range(len(self.config.model.strengths_ascending))))
        return {
            "state_schema_version": 1,
            "run_id": self.config.run_id,
            "config_fingerprint": self.config.fingerprint,
            "data_fingerprint": self.dataset.fingerprint,
            "phase": PHASE_STANDARDIZATION,
            "phase_complete": False,
            "standardization": {
                "method": self.config.standardization,
                "stats": standardized.stats,
                "stats_version": standardized.stats_version,
                "feature_names": list(standardized.feature_names),
            },
            "chunk_plan": plan.to_dict(),
            "solve_order_indices": solve_order,
            "path": [],
            "next_solve_position": 0,
            "validation": None,
            "summary": None,
            "resource_usage": {
                "comparison_limit": self.config.execution.comparison_budget,
                "comparisons_used": 0,
            },
        }

    def _standardize(self) -> None:
        standardized = fit_standardization(self.dataset, self.config)
        iteration_counts = tuple(
            self.config.solver.iterations_for(index)
            for index in range(len(self.config.model.strengths_ascending))
        )
        plan = plan_chunks(
            self.config,
            n_samples=self.dataset.n_samples,
            n_features=self.dataset.n_features,
            iteration_counts_ascending=iteration_counts,
        )
        self.state = self._new_state(standardized, plan)
        self._commit(stage=PHASE_STANDARDIZATION, stage_detail="features")

    def _plan_from_state(self) -> ChunkPlan:
        if self.state is None:
            raise EngineError(E_STATE, "state is not initialized", stage=PHASE_PATH_SOLVE)
        plan_doc = self.state.get("chunk_plan")
        if not isinstance(plan_doc, dict):
            raise self._corrupt_state("chunk plan is missing")
        try:
            plan = ChunkPlan.from_dict(plan_doc)
        except (KeyError, TypeError, ValueError) as exc:
            raise self._corrupt_state(f"chunk plan is invalid: {exc}") from exc
        if plan.n_samples != self.dataset.n_samples or plan.n_features != self.dataset.n_features:
            raise EngineError(
                E_VERSION_MISMATCH,
                "committed chunk plan input scale differs from the dataset",
                stage=PHASE_PATH_SOLVE,
                version=self.version,
            )
        return plan

    def _hard_exit(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(97)

    def _solve_path(
        self,
        *,
        crash_after_committed_strengths: int | None,
        crash_during_strength: int | None,
        crash_after_iterations: int | None,
        fault_worker_at: str | None,
    ) -> None:
        if self.state is None:
            raise EngineError(E_STATE, "state is not initialized", stage=PHASE_PATH_SOLVE)
        standardization_doc = self.state["standardization"]
        stats = standardization_doc["stats"]
        stats_version = standardization_doc["stats_version"]
        matrix = transform_with_stats(self.dataset, stats)
        plan = self._plan_from_state()
        usage = self.state.get("resource_usage", {})
        comparisons_used = int(usage.get("comparisons_used", 0))
        if comparisons_used > self.config.execution.comparison_budget:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "committed comparisons already exceed the configured budget",
                stage=PHASE_PATH_SOLVE,
                version=self.version,
            )
        budget = ComparisonBudget(
            limit=self.config.execution.comparison_budget,
            used=comparisons_used,
        )
        fault_spec = fault_worker_at
        if fault_spec is None:
            raw_fault = self.config.testing.get("fault_worker_at")
            if raw_fault is not None:
                if not isinstance(raw_fault, str):
                    raise EngineError(
                        E_STATE,
                        "testing.fault_worker_at must be a string",
                        stage="config",
                    )
                fault_spec = raw_fault

        def iteration_hook(ascending_index: int, iteration: int) -> None:
            if (
                crash_during_strength is not None
                and crash_after_iterations is not None
                and ascending_index == crash_during_strength
                and iteration >= crash_after_iterations
            ):
                self._hard_exit()

        solve_order = list(self.state["solve_order_indices"])
        path = self.state["path"]
        with PathSolver(
            config=self.config,
            standardized_matrix=matrix,
            targets=self.dataset.targets,
            plan=plan,
            budget=budget,
            stats_version=stats_version,
            fault_spec=fault_spec,
            iteration_hook=iteration_hook,
        ) as solver:
            while len(path) < len(solve_order):
                solve_position = len(path)
                ascending_index = int(solve_order[solve_position])
                if path:
                    last_entry = path[-1]
                    initial_weights = np.asarray(last_entry["coefficients"], dtype=np.float64)
                    initial_intercept = float(last_entry["intercept"])
                    warm_start_from = float(last_entry["strength"])
                else:
                    initial_weights = np.zeros(self.dataset.n_features, dtype=np.float64)
                    initial_intercept = 0.0
                    warm_start_from = None
                entry = solver.solve_strength(
                    ascending_index=ascending_index,
                    solve_position=solve_position,
                    initial_weights=initial_weights,
                    initial_intercept=initial_intercept,
                    warm_start_from_strength=warm_start_from,
                )
                path.append(entry)
                self.state["next_solve_position"] = len(path)
                self.state["resource_usage"]["comparisons_used"] = budget.used
                self._commit(
                    stage=PHASE_PATH_SOLVE,
                    stage_detail=f"strength-{ascending_index:08d}",
                    phase_complete=len(path) == len(solve_order),
                )
                if (
                    crash_after_committed_strengths is not None
                    and len(path) >= crash_after_committed_strengths
                ):
                    self._hard_exit()
        self.state["resource_usage"]["comparisons_used"] = budget.used

    def _validate(self) -> None:
        if self.state is None:
            raise EngineError(E_STATE, "state is not initialized", stage=PHASE_PATH_VALIDATION)
        standardization_doc = self.state["standardization"]
        validation = validate_path(
            config=self.config,
            dataset=self.dataset,
            path_entries=self.state["path"],
            stats=standardization_doc["stats"],
            stats_version=standardization_doc["stats_version"],
        )
        self.state["validation"] = validation
        self._commit(stage=PHASE_PATH_VALIDATION, stage_detail="all-strengths")

    def _build_result(self) -> dict[str, Any]:
        if self.state is None:
            raise EngineError(E_STATE, "state is not initialized", stage=PHASE_PATH_SUMMARY)
        path = self.state["path"]
        entries_ascending = sorted(path, key=lambda item: float(item["strength"]))
        best_entry = min(
            entries_ascending,
            key=lambda item: (float(item["objective"]), float(item["strength"])),
        )
        converged_count = sum(1 for entry in entries_ascending if entry["converged"])
        plan = self._plan_from_state()
        standardization_doc = self.state["standardization"]
        result = {
            "schema_version": 1,
            "status": "ok",
            "run_id": self.config.run_id,
            "config_fingerprint": self.config.fingerprint,
            "data_fingerprint": self.dataset.fingerprint,
            "n_samples": self.dataset.n_samples,
            "n_features": self.dataset.n_features,
            "feature_names": list(self.dataset.feature_names),
            "standardization": {
                "contract": "standardization-v1",
                "method": self.config.standardization,
                "stats_version": standardization_doc["stats_version"],
                "stats": standardization_doc["stats"],
            },
            "model": {
                "loss": self.config.model.loss,
                "regularization": self.config.model.regularization,
                "l1_ratio": self.config.model.l1_ratio,
                "strengths_ascending": list(self.config.model.strengths_ascending),
            },
            "precision": {
                "mode": self.config.precision.mode,
                "fixed_scale": self.config.precision.fixed_scale,
                "fixed_rounding": self.config.precision.fixed_rounding,
            },
            "execution": {
                "path_solve_order": "strength_descending",
                "reduction_order": plan.reduction_order,
                "dispatch_order": plan.dispatch_order,
                "workers": plan.workers,
                "block_size": plan.block_size,
                "degradation_categories": list(plan.degradation_categories),
            },
            "chunk_plan": plan.to_dict(),
            "path": path,
            "validation": self.state["validation"],
            "summary": {
                "completed_strengths": len(path),
                "path_solve_order": "strength_descending",
                "path_checksum": sha256_value(path),
                "best_objective_strength": float(best_entry["strength"]),
                "best_objective": float(best_entry["objective"]),
                "converged_strengths": converged_count,
                "max_iteration_strengths": len(path) - converged_count,
                "norm_by_strength_ascending": [
                    {
                        "strength": float(entry["strength"]),
                        "strength_index": int(entry["strength_index"]),
                        "coefficient_norm_l2": float(entry["coefficient_norm_l2"]),
                        "objective": float(entry["objective"]),
                        "converged": bool(entry["converged"]),
                    }
                    for entry in entries_ascending
                ],
                "degradation_categories": list(plan.degradation_categories),
            },
            "resource_usage": {
                "comparison_limit": int(self.config.execution.comparison_budget),
                "comparisons_used": int(
                    self.state["resource_usage"].get("comparisons_used", 0)
                ),
                "estimated_comparisons": int(plan.estimated_comparisons),
                "estimated_resident_bytes": int(plan.estimated_resident_bytes),
            },
        }
        return result

    def _summarize(self) -> dict[str, Any]:
        result = self._build_result()
        if self.state is None:
            raise EngineError(E_STATE, "state is not initialized", stage=PHASE_PATH_SUMMARY)
        self.state["summary"] = result["summary"]
        self.state["result_document"] = result
        self._commit(stage=PHASE_PATH_SUMMARY, stage_detail="complete")
        return result

    def _write_result(self, result: dict[str, Any]) -> Path:
        self.config.output_directory.mkdir(parents=True, exist_ok=True)
        result_path = self.config.output_directory / "result.json"
        atomic_write_json(result_path, result)
        return result_path

    def run(
        self,
        *,
        crash_after_committed_strengths: int | None = None,
        crash_during_strength: int | None = None,
        crash_after_iterations: int | None = None,
        fault_worker_at: str | None = None,
    ) -> dict[str, Any]:
        self.preflight_limits()
        loaded = self._load_existing()
        if loaded and self.state is not None:
            if self.state["phase"] == PHASE_PATH_SUMMARY:
                result = self.state.get("result_document")
                if not isinstance(result, dict):
                    raise self._corrupt_state("complete snapshot has no result document")
                self._write_result(result)
                return result
        if self.state is None:
            self._standardize()
        if self.state is None:
            raise EngineError(E_STATE, "standardization did not produce state")
        if _PHASE_RANK[self.state["phase"]] < _PHASE_RANK[PHASE_PATH_SOLVE] or len(
            self.state["path"]
        ) < len(self.config.model.strengths_ascending):
            self._solve_path(
                crash_after_committed_strengths=crash_after_committed_strengths,
                crash_during_strength=crash_during_strength,
                crash_after_iterations=crash_after_iterations,
                fault_worker_at=fault_worker_at,
            )
        if self.state is None:
            raise EngineError(E_STATE, "path solve did not produce state")
        if _PHASE_RANK[self.state["phase"]] < _PHASE_RANK[PHASE_PATH_VALIDATION]:
            self._validate()
        if self.state is None:
            raise EngineError(E_STATE, "validation did not produce state")
        if _PHASE_RANK[self.state["phase"]] < _PHASE_RANK[PHASE_PATH_SUMMARY]:
            result = self._summarize()
        else:
            stored = self.state.get("result_document")
            if not isinstance(stored, dict):
                raise self._corrupt_state("summary snapshot has no result document")
            result = stored
        self._write_result(result)
        return result


def run_training(config_path: str | Path, **run_options: Any) -> dict[str, Any]:
    return TrainingEngine(config_path).run(**run_options)

