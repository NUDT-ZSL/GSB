from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .blocking import BlockPlan, decide_block_plan, whole_block_plan
from .checkpoint import CheckpointStore, ForcedTermination
from .config import EngineConfig, PRECISIONS
from .data import Dataset, PreparedFeatures, load_jsonl_dataset, prepare_features
from .diagnostics import build_error_spectrum
from .errors import DiagnosticError, DiagnosticIssue
from .injection import InjectionController, parse_injection_arguments
from .precision import PrecisionOps
from .snapshots import load_snapshot
from .solver import PathRun, PathStepResult, fit_regularization_path
from .utils import (
    canonical_json_bytes,
    sha256_file,
    sha256_json,
    write_json_atomic,
)
from .validation import validate_results


@dataclass
class PrecisionOutcome:
    run: PathRun
    resume: dict[str, Any]
    checkpoint: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.to_dict(),
            "resume": self.resume,
            "checkpoint": self.checkpoint,
        }


def _step_from_dict(value: dict[str, Any]) -> PathStepResult:
    return PathStepResult(
        path_index=int(value["path_index"]),
        source_index=int(value["source_index"]),
        strength=float(value["strength"]),
        coefficients=tuple(float(item) for item in value["coefficients"]),
        intercept=float(value["intercept"]),
        objective=float(value["objective"]),
        blocked_objective=float(value["blocked_objective"]),
        whole_objective=float(value["whole_objective"]),
        objective_gap=float(value["objective_gap"]),
        convergence_delta=float(value["convergence_delta"]),
        gradient_inf_norm=float(value["gradient_inf_norm"]),
        iterations=int(value["iterations"]),
        converged=bool(value["converged"]),
        warm_start=bool(value["warm_start"]),
        block_plan_sha256=str(value["block_plan_sha256"]),
    )


def _checkpoint_payload(
    *,
    stage: str,
    config: EngineConfig,
    precision: str,
    data_sha256: str,
    feature_sha256: str,
    block_plan: BlockPlan,
    strengths: list[float],
    completed_steps: list[PathStepResult],
    convergence_schedule: list[tuple[int, bool]] | None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "config_checksum": config.checksum,
        "config_fingerprint": config.fingerprint(),
        "precision": precision,
        "data_sha256": data_sha256,
        "feature_sha256": feature_sha256,
        "block_plan_sha256": block_plan.decision_sha256,
        "strengths": strengths,
        "convergence_schedule": (
            [list(item) for item in convergence_schedule]
            if convergence_schedule is not None
            else None
        ),
        "next_path_index": len(completed_steps),
        "completed_steps": [step.to_dict() for step in completed_steps],
    }


def train_precision(
    *,
    matrix: np.ndarray,
    labels: np.ndarray,
    config: EngineConfig,
    precision: str,
    block_plan: BlockPlan,
    whole_plan: BlockPlan,
    strengths: list[float],
    data_sha256: str,
    checkpoint_directory: str | Path | None = None,
    resume: bool = True,
    abort_after_steps: int | None = None,
    on_step: Callable[[int, PathStepResult], None] | None = None,
    convergence_schedule: list[tuple[int, bool]] | None = None,
) -> PrecisionOutcome:
    ops = PrecisionOps.from_config(config, precision)
    executed_matrix, integer_matrix = ops.prepare_features(matrix)
    feature_sha256 = sha256_json(matrix.tolist())
    store = (
        CheckpointStore(checkpoint_directory)
        if checkpoint_directory is not None
        else None
    )
    identity = {
        "config_checksum": config.checksum,
        "config_fingerprint": config.fingerprint(),
        "precision": precision,
        "data_sha256": data_sha256,
        "feature_sha256": feature_sha256,
        "block_plan_sha256": block_plan.decision_sha256,
        "convergence_schedule": (
            [list(item) for item in convergence_schedule]
            if convergence_schedule is not None
            else None
        ),
    }
    loaded = store.latest(expected=identity) if store and resume else None
    loaded_version = loaded.version if loaded else None
    completed_steps: list[PathStepResult] = []
    start_index = 0
    if loaded is not None:
        if loaded.payload["strengths"] != strengths:
            raise DiagnosticError(
                "E_CHECKPOINT_IDENTITY_MISMATCH",
                "checkpoint",
                "checkpoint regularization path differs from current config",
            )
        completed_steps = [
            _step_from_dict(item)
            for item in loaded.payload.get("completed_steps", [])
        ]
        start_index = int(loaded.payload.get("next_path_index", len(completed_steps)))
        if start_index != len(completed_steps):
            raise DiagnosticError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint",
                "checkpoint next_path_index does not match completed steps",
            )
    if loaded is not None and loaded.stage == "path_complete":
        run = PathRun(
            precision=precision,
            steps=completed_steps,
            coefficients=np.asarray(
                completed_steps[-1].coefficients, dtype=np.float64
            ),
            intercept=completed_steps[-1].intercept,
            matrix=executed_matrix,
            integer_matrix=integer_matrix,
            block_plan=block_plan,
            whole_plan=whole_plan,
        )
        return PrecisionOutcome(
            run=run,
            resume={
                "resumed": True,
                "loaded_checkpoint_version": loaded_version,
                "loaded_stage": loaded.stage,
                "reused_steps": len(completed_steps),
                "recomputed_steps": 0,
            },
            checkpoint=(store.metadata() if store else {}),
        )
    committed_steps = list(completed_steps)
    new_commits = 0

    def handle_step(path_index: int, step: PathStepResult) -> None:
        nonlocal new_commits
        committed_steps.append(step)
        new_commits += 1
        checkpoint = None
        if store is not None:
            checkpoint = store.save(
                "path_step_committed",
                _checkpoint_payload(
                    stage="path_step_committed",
                    config=config,
                    precision=precision,
                    data_sha256=data_sha256,
                    feature_sha256=feature_sha256,
                    block_plan=block_plan,
                    strengths=strengths,
                    completed_steps=committed_steps,
                    convergence_schedule=convergence_schedule,
                ),
            )
        if on_step is not None:
            on_step(path_index, step)
        if (
            abort_after_steps is not None
            and abort_after_steps > 0
            and new_commits >= abort_after_steps
        ):
            raise ForcedTermination(
                stage="path_solve",
                committed_steps=len(committed_steps),
                checkpoint=checkpoint.path if checkpoint else "",
            )

    run = fit_regularization_path(
        matrix=executed_matrix,
        labels=labels,
        strengths=strengths,
        config=config,
        ops=ops,
        block_plan=block_plan,
        whole_plan=whole_plan,
        integer_matrix=integer_matrix,
        start_index=start_index,
        completed_steps=completed_steps,
        on_step=handle_step,
        convergence_schedule=convergence_schedule,
    )
    if store is not None:
        store.save(
            "path_complete",
            _checkpoint_payload(
                stage="path_complete",
                config=config,
                precision=precision,
                data_sha256=data_sha256,
                feature_sha256=feature_sha256,
                block_plan=block_plan,
                strengths=strengths,
                completed_steps=run.steps,
                convergence_schedule=convergence_schedule,
            ),
        )
    return PrecisionOutcome(
        run=run,
        resume={
            "resumed": loaded is not None,
            "loaded_checkpoint_version": loaded_version,
            "loaded_stage": loaded.stage if loaded else None,
            "reused_steps": len(completed_steps),
            "recomputed_steps": len(run.steps) - len(completed_steps),
        },
        checkpoint=(store.metadata() if store else {}),
    )


def _run_inference(
    *,
    prepared: PreparedFeatures,
    dataset: Dataset,
    run: PathRun,
    config: EngineConfig,
) -> dict[str, Any]:
    ops = PrecisionOps.from_config(config, run.precision)
    final = run.steps[-1]
    coefficients = np.asarray(final.coefficients, dtype=np.float64)
    logits = ops.matmul(run.matrix, coefficients) + final.intercept
    probabilities = ops.sigmoid(logits)
    predictions = (probabilities >= 0.5).astype(np.int64)
    labels = dataset.labels.astype(np.int64)
    accuracy = float(np.mean(predictions == labels))
    rows = []
    for sample_id, probability, prediction, label in zip(
        dataset.sample_ids,
        probabilities.tolist(),
        predictions.tolist(),
        labels.tolist(),
    ):
        rows.append(
            {
                "sample_id": sample_id,
                "score": float(probability),
                "prediction": int(prediction),
                "label": int(label),
            }
        )
    return {
        "precision": run.precision,
        "path_index": final.path_index,
        "strength": final.strength,
        "accuracy": accuracy,
        "positive_count": int(np.sum(predictions)),
        "prediction_sha256": sha256_json(rows),
        "rows": rows,
    }


def _write_predictions(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [canonical_json_bytes(row).decode("utf-8") for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _outcome_with_precision_config(
    config: EngineConfig, precision: str, outcome: PrecisionOutcome
) -> dict[str, Any]:
    payload = outcome.to_dict()
    payload["run"]["precision_config"] = PrecisionOps.from_config(
        config, precision
    ).to_dict()
    return payload


class DiagnosticEngine:
    def __init__(
        self,
        *,
        data_path: str | Path,
        snapshot_path: str | Path,
        output_directory: str | Path,
        precisions: Iterable[str] = PRECISIONS,
        injection_specs: Iterable[dict[str, Any]] | None = None,
        resume: bool = True,
    ) -> None:
        self.data_path = Path(data_path)
        self.snapshot_path = Path(snapshot_path)
        self.output_directory = Path(output_directory)
        self.precisions = tuple(dict.fromkeys(precisions))
        self.injection_specs = list(injection_specs or ())
        self.resume = resume

    def run(
        self,
        *,
        abort_after_steps: int | None = None,
        abort_precision: str | None = None,
    ) -> dict[str, Any]:
        for precision in self.precisions:
            if precision not in PRECISIONS:
                raise DiagnosticError(
                    "E_CONFIG_INVALID",
                    "config",
                    f"unknown precision {precision!r}",
                )
        self.output_directory.mkdir(parents=True, exist_ok=True)
        config = load_snapshot(self.snapshot_path)
        dataset = load_jsonl_dataset(self.data_path, config)
        controller = InjectionController.from_specs(
            [*config.injections, *self.injection_specs]
        )
        if config.tolerance <= 0.0:
            fallback_solver_tolerance = 1e-4
            controller.issues.append(
                DiagnosticIssue(
                    stage="result_validation",
                    code="E_TOLERANCE_NONPOSITIVE",
                    message="solver tolerance is not positive",
                    action="use_default_solver_tolerance",
                    degrade_category="default_validation_tolerance",
                    details={
                        "injected_tolerance": config.tolerance,
                        "fallback_tolerance": fallback_solver_tolerance,
                        "scope": "solver",
                    },
                )
            )
            config = config.with_updates(tolerance=fallback_solver_tolerance)
        prepared = prepare_features(dataset, config, controller)
        block_plan = decide_block_plan(
            sample_count=dataset.sample_count,
            feature_count=prepared.matrix.shape[1],
            config=config,
            injector=controller,
        )
        whole_plan = whole_block_plan(
            sample_count=dataset.sample_count,
            feature_count=prepared.matrix.shape[1],
            config=config,
        )
        strengths, _ = controller.inject_strength(
            list(config.strength_path)
        )
        for index, strength in enumerate(strengths):
            if strength <= 0.0:
                fallback = 1e-8
                strengths[index] = fallback
                controller.issues.append(
                    DiagnosticIssue(
                        stage="path_solve",
                        code="E_REG_STRENGTH_NONPOSITIVE",
                        message="regularization strength is not positive",
                        action="clamp_to_positive_floor",
                        degrade_category="regularization_strength_clamped",
                        details={
                            "path_index": index,
                            "injected_value": strength,
                            "fallback": fallback,
                        },
                    )
                )
        data_sha256 = sha256_file(self.data_path)
        feature_checkpoint = CheckpointStore(self.output_directory / "checkpoints")
        feature_checkpoint.save(
            "features_ready",
            {
                "stage": "features_ready",
                "config_checksum": config.checksum,
                "config_fingerprint": config.fingerprint(),
                "data_sha256": data_sha256,
                "feature_sha256": sha256_json(prepared.matrix.tolist()),
                "sample_count": dataset.sample_count,
                "feature_count": int(prepared.matrix.shape[1]),
                "feature_names": list(prepared.feature_names),
                "standardization": prepared.stats.to_dict(),
                "block_plan_sha256": block_plan.decision_sha256,
                "issues": controller.issue_dicts(),
            },
        )
        outcomes: dict[str, PrecisionOutcome] = {}
        reference = train_precision(
            matrix=prepared.matrix,
            labels=dataset.labels,
            config=config,
            precision="float64",
            block_plan=whole_plan,
            whole_plan=whole_plan,
            strengths=strengths,
            convergence_schedule=None,
            data_sha256=data_sha256,
            checkpoint_directory=self.output_directory / "precisions" / "reference-whole",
            resume=self.resume,
        )
        canonical_schedule = [
            (step.iterations, step.converged) for step in reference.run.steps
        ]
        try:
            for precision in self.precisions:
                try:
                    outcomes[precision] = train_precision(
                        matrix=prepared.matrix,
                        labels=dataset.labels,
                        config=config,
                        precision=precision,
                        block_plan=block_plan,
                        whole_plan=whole_plan,
                        strengths=strengths,
                        convergence_schedule=canonical_schedule,
                        data_sha256=data_sha256,
                        checkpoint_directory=(
                            self.output_directory / "precisions" / precision
                        ),
                        resume=self.resume,
                        abort_after_steps=(
                            abort_after_steps
                            if abort_precision is None or abort_precision == precision
                            else None
                        ),
                    )
                except ForcedTermination as exc:
                    terminated = {
                        "status": "terminated",
                        "forced_termination": exc.to_dict(),
                        "completed_precisions": list(outcomes),
                        "checkpoints": {
                            precision: (
                                self.output_directory
                                / "precisions"
                                / precision
                            ).as_posix()
                            for precision in [*outcomes, precision]
                        },
                    }
                    write_json_atomic(
                        self.output_directory / "terminated.json", terminated
                    )
                    raise
        except ForcedTermination:
            raise
        diagnostic_reports: dict[str, Any] = {}
        for precision_name, outcome in outcomes.items():
            diagnostic_reports[precision_name] = build_error_spectrum(
                config=config,
                reference=reference.run,
                observed=outcome.run,
                labels=dataset.labels,
                block_plan=block_plan,
            )
        validation_tolerance = config.objective_tolerance
        injected_tolerance, tolerance_injected = controller.inject_tolerance(
            validation_tolerance
        )
        if tolerance_injected and injected_tolerance <= 0.0:
            fallback_tolerance = max(config.objective_tolerance, 1e-6)
            controller.record_tolerance_fallback(
                injected_value=injected_tolerance,
                fallback=fallback_tolerance,
            )
            validation_tolerance = fallback_tolerance
        elif injected_tolerance > 0.0:
            validation_tolerance = injected_tolerance
        issues = controller.issue_dicts()
        validation = validate_results(
            config=config,
            dataset=dataset,
            prepared=prepared,
            labels=dataset.labels,
            runs={name: outcome.run for name, outcome in outcomes.items()},
            reference=reference.run,
            block_plan=block_plan,
            injection_issues=issues,
            tolerance=validation_tolerance,
        )
        inference_run = outcomes.get(config.precision)
        if inference_run is None:
            inference_run = (
                outcomes.get("float64") if "float64" in outcomes else reference
            )
        inference = _run_inference(
            prepared=prepared,
            dataset=dataset,
            run=inference_run.run,
            config=config,
        )
        predictions_path = self.output_directory / "predictions.jsonl"
        _write_predictions(predictions_path, inference.pop("rows"))
        prediction_checksum = sha256_file(predictions_path)
        inference["predictions_path"] = str(predictions_path)
        inference["predictions_file_sha256"] = prediction_checksum
        run_id = sha256_json(
            {
                "config_fingerprint": config.fingerprint(),
                "data_sha256": data_sha256,
                "precisions": list(self.precisions),
                "strengths": strengths,
                "injections": controller.applied,
                "block_plan_sha256": block_plan.decision_sha256,
            }
        )
        report = {
            "status": "degraded" if issues else "ok",
            "run_id": run_id,
            "snapshot": {
                "path": str(self.snapshot_path),
                "snapshot_id": config.snapshot_id,
                "version": config.version,
                "generation": config.generation,
                "created_at_ms": config.created_at_ms,
                "checksum": config.checksum,
                "metadata_sources": config.metadata_sources,
            },
            "data": {
                "path": str(self.data_path),
                "sha256": data_sha256,
                "sample_count": dataset.sample_count,
                "label_counts": {
                    "0": int(np.sum(dataset.labels == 0)),
                    "1": int(np.sum(dataset.labels == 1)),
                },
                "feature_count": int(prepared.matrix.shape[1]),
                "feature_names": list(prepared.feature_names),
                "standardization": prepared.stats.to_dict(),
            },
            "path": {
                "regularization": config.regularization,
                "strengths": strengths,
                "ordered_strengths": [
                    {
                        "path_index": step.path_index,
                        "source_index": step.source_index,
                        "strength": step.strength,
                        "iterations": step.iterations,
                        "converged": step.converged,
                    }
                    for step in reference.run.steps
                ],
                "max_iter": config.max_iter,
                "tolerance": config.tolerance,
                "ordered_from": "strongest_to_weakest",
                "convergence_controller": "float64_whole_reference_schedule",
            },
            "blocking": {
                "decision": block_plan.to_dict(),
                "whole_plan": whole_plan.to_dict(),
                "shared_across_precisions": True,
            },
            "precisions": {
                name: _outcome_with_precision_config(config, name, outcome)
                for name, outcome in outcomes.items()
            },
            "reference_whole": _outcome_with_precision_config(
                config, "float64", reference
            ),
            "diagnostics": diagnostic_reports,
            "validation": validation,
            "inference": inference,
            "injections": issues,
            "degradation_categories": validation["degradation_categories"],
            "recovery": {
                "resume_enabled": self.resume,
                "feature_checkpoint": feature_checkpoint.metadata(),
            },
        }
        write_json_atomic(self.output_directory / "run_report.json", report)
        return report


def run_engine(
    *,
    data_path: str | Path,
    snapshot_path: str | Path,
    output_directory: str | Path,
    precisions: Iterable[str] = PRECISIONS,
    injections: Iterable[str] = (),
    resume: bool = True,
    abort_after_steps: int | None = None,
    abort_precision: str | None = None,
) -> dict[str, Any]:
    engine = DiagnosticEngine(
        data_path=data_path,
        snapshot_path=snapshot_path,
        output_directory=output_directory,
        precisions=precisions,
        injection_specs=parse_injection_arguments(injections),
        resume=resume,
    )
    return engine.run(
        abort_after_steps=abort_after_steps,
        abort_precision=abort_precision,
    )

