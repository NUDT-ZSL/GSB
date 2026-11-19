"""End-to-end job execution, report writing and persistent workbench state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointStore
from .config import TrainingConfig
from .data import (
    Dataset,
    DatasetSplit,
    load_dataset,
    make_stratified_split,
    prepare_directories,
)
from .diagnostics import (
    DiagnosticsResult,
    build_diagnostics,
    chunk_full_summary,
    require_tolerance_consistency,
)
from .errors import WorkbenchError, inconsistent
from .features import PreparedData, prepare_dataset
from .precision import make_engine
from .registry import JobRegistry
from .serialization import canonical_dumps, sha256_json
from .snapshots import SnapshotStore
from .training import TrainingRun, train_prepared


def config_sha256(config: TrainingConfig) -> str:
    return sha256_json(config.to_dict())


def _atomic_write_json(path: str | Path, payload: dict[str, Any], *, pretty: bool = False) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    else:
        text = canonical_dumps(payload) + "\n"
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def record_business_error(root: str | Path, error: WorkbenchError) -> None:
    cache = Path(root) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(cache / "last_error.json", error.to_dict(), pretty=True)


def clear_business_error(root: str | Path) -> None:
    target = Path(root) / "cache" / "last_error.json"
    if target.exists():
        target.unlink()


def _primary_prepared(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
) -> PreparedData:
    engine = make_engine(
        config.precision,
        fixed_scale=config.fixed_scale,
        rounding_mode=config.rounding_mode,
    )
    return prepare_dataset(dataset, split, config, engine)


def _checkpoint_summary(
    run: TrainingRun,
    checkpoint_path: str,
) -> dict[str, Any]:
    previously_committed = [
        round_number
        for round_number in run.committed_rounds
        if round_number not in set(run.rebuilt_rounds)
    ]
    return {
        "path": checkpoint_path,
        "committed_rounds": list(run.committed_rounds),
        "rebuilt_rounds": list(run.rebuilt_rounds),
        "committed_before_invocation": previously_committed,
        "discarded_rounds": [run.discarded_round] if run.discarded_round else [],
        "started_from_round": run.started_from_round,
        "interrupted": run.interrupted,
        "stop_reason": run.stop_reason,
    }


def _trajectory_summary(run: TrainingRun) -> dict[str, Any]:
    summary = run.summary()
    return {
        "precision": summary["precision"],
        "reduce_mode": summary["reduce_mode"],
        "rounds": summary["rounds"],
        "stop_reason": summary["stop_reason"],
        "converged": summary["converged"],
        "loss_trajectory_hex": summary["loss_trajectory_hex"],
        "validation_trajectory": summary["validation_trajectory"],
        "params_hex": summary["params_hex"],
        "params_sha256": summary["params_sha256"],
        "trajectory_sha256": summary["trajectory_sha256"],
        "core_sha256": summary["core_sha256"],
        "convergence": summary["convergence"],
    }


def _commit_snapshots(
    snapshots: SnapshotStore,
    job_id: str,
    config: TrainingConfig,
    dataset: Dataset,
    diagnostics: DiagnosticsResult,
) -> dict[str, Any]:
    normalization = {
        precision: diagnostics.prepared[precision].statistics_payload()
        for precision in ("float64", "float32", "fixed_point")
    }
    chunk_plan = {
        "chunk_size": config.chunk_size,
        "reduction_order": (
            "chunk_index_ascending; within_chunk_sample_index_ascending; "
            "sequential_left_fold_over_chunk_sums"
        ),
        "full_reduction_order": "sample_index_ascending; single vectorized reduction",
        "plans": {
            precision: diagnostics.prepared[precision].chunk_plan_payload()
            for precision in ("float64", "float32", "fixed_point")
        },
    }
    spectra = {
        "reference": diagnostics.payload["reference"],
        "spectra": diagnostics.payload["spectra"],
        "dominant_source": {
            "precision": diagnostics.payload["dominant_source"]["precision"],
            "stage": diagnostics.payload["dominant_source"]["stage"],
            "severity": diagnostics.payload["dominant_source"]["severity"],
            "amplification_ratio": diagnostics.payload["dominant_source"][
                "amplification_ratio"
            ],
            "worst_detail": diagnostics.payload["dominant_source"]["worst_detail"],
        },
    }
    trajectories = {
        precision: {
            "chunked": _trajectory_summary(diagnostics.runs[precision]["chunked"]),
            "full": _trajectory_summary(diagnostics.runs[precision]["full"]),
        }
        for precision in ("float64", "float32", "fixed_point")
    }
    return snapshots.commit(
        job_id,
        stage_payloads={
            "normalization_statistics": normalization,
            "chunk_plan": chunk_plan,
            "precision_error_spectra": spectra,
            "trajectory_summaries": trajectories,
        },
        input_sha256=dataset.input_sha256,
        config_sha256=config_sha256(config),
    )


def _report_checks(diagnostics: DiagnosticsResult) -> dict[str, Any]:
    verification = diagnostics.payload.get("verification", {})
    sample_check = diagnostics.payload["sample_index_checks"]
    trajectory_raw = verification.get("trajectory_recompute_match")
    spectrum_raw = verification.get("error_spectrum_recompute_match")
    trajectory_ok = None if trajectory_raw is None else bool(trajectory_raw)
    spectrum_ok = None if spectrum_raw is None else bool(spectrum_raw)
    return {
        "sample_indexes_consistent": bool(sample_check["consistent"]),
        "sample_index_issues": sample_check["issues"],
        "trajectory_recompute_match": trajectory_ok,
        "error_spectrum_recompute_match": spectrum_ok,
        "all_ok": bool(
            sample_check["consistent"]
            and trajectory_ok is True
            and spectrum_ok is True
        ),
        "trajectory_digests": verification.get("trajectory_digests"),
        "spectrum_sha256": verification.get("first_spectrum_sha256"),
    }


def _assert_primary_matches_diagnostic_run(
    primary_run: TrainingRun,
    diagnostics: DiagnosticsResult,
    config: TrainingConfig,
) -> None:
    diagnostic_run = diagnostics.runs[config.precision]["chunked"]
    if primary_run.core_digest != diagnostic_run.core_digest:
        raise inconsistent(
            "trajectory_recompute_mismatch",
            "checkpointed primary trajectory differs from the diagnostic chunked trajectory",
            primary_sha256=primary_run.core_digest,
            diagnostic_sha256=diagnostic_run.core_digest,
        )


def _assemble_complete_report(
    *,
    root: Path,
    job_id: str,
    config: TrainingConfig,
    dataset: Dataset,
    split: DatasetSplit,
    primary_run: TrainingRun,
    diagnostics: DiagnosticsResult,
    checkpoint_summary: dict[str, Any],
    snapshot_summary: dict[str, Any] | None,
    business_error: WorkbenchError | None,
) -> dict[str, Any]:
    status = "ERROR" if business_error is not None else "OK"
    return {
        "schema": "numeric-workbench/report/v1",
        "job_id": job_id,
        "status": status,
        "business_error": business_error.to_dict() if business_error else None,
        "recent_business_error_code": (
            business_error.code if business_error is not None else None
        ),
        "input": {
            "path": dataset.path,
            "sha256": dataset.input_sha256,
            "row_count": dataset.row_count,
            "train_count": split.train_count,
            "validation_count": split.validation_count,
            "fingerprint": split.fingerprint(dataset),
        },
        "config": config.to_dict(),
        "config_sha256": config_sha256(config),
        "execution": {
            "precision": config.precision,
            "reduce_mode": "chunked",
            "primary_run": primary_run.summary(),
            "checkpoint": checkpoint_summary,
        },
        "chunk_full_consistency": chunk_full_summary(diagnostics),
        "precision_switch": diagnostics.payload["precision_switch"],
        "error_spectrum": {
            "reference": diagnostics.payload["reference"],
            "spectra": diagnostics.payload["spectra"],
            "dominant_source": diagnostics.payload["dominant_source"],
            "verification": diagnostics.payload["verification"],
        },
        "sample_index_checks": diagnostics.payload["sample_index_checks"],
        "workbench_checks": _report_checks(diagnostics),
        "snapshot": snapshot_summary,
        "outputs": {
            "report": str(root / "runs" / job_id / "report.json"),
            "checkpoint": checkpoint_summary["path"],
            "snapshot": snapshot_summary["path"] if snapshot_summary else None,
        },
    }


def _partial_report(
    *,
    root: Path,
    job_id: str,
    config: TrainingConfig,
    dataset: Dataset,
    split: DatasetSplit,
    primary_run: TrainingRun,
    checkpoint_summary: dict[str, Any],
) -> dict[str, Any]:
    prepared = primary_run.prepared
    flattened = [index for chunk in prepared.chunk_global_indices for index in chunk]
    index_ok = (
        tuple(flattened) == prepared.train_global_indices
        and len(set(flattened)) == len(flattened)
        and tuple(prepared.validation_global_indices) == split.validation_indices
    )
    return {
        "schema": "numeric-workbench/report/v1",
        "job_id": job_id,
        "status": "INTERRUPTED",
        "business_error": None,
        "recent_business_error_code": None,
        "input": {
            "path": dataset.path,
            "sha256": dataset.input_sha256,
            "row_count": dataset.row_count,
            "train_count": split.train_count,
            "validation_count": split.validation_count,
            "fingerprint": split.fingerprint(dataset),
        },
        "config": config.to_dict(),
        "config_sha256": config_sha256(config),
        "execution": {
            "precision": config.precision,
            "reduce_mode": "chunked",
            "primary_run": primary_run.summary(),
            "checkpoint": checkpoint_summary,
        },
        "chunk_full_consistency": None,
        "precision_switch": None,
        "error_spectrum": None,
        "sample_index_checks": {
            "consistent": index_ok,
            "train_sha256": prepared.sample_fingerprint["train_sha256"],
            "validation_sha256": prepared.sample_fingerprint["validation_sha256"],
            "issues": [] if index_ok else [{"category": "partial_index_mismatch"}],
        },
        "workbench_checks": {
            "sample_indexes_consistent": index_ok,
            "trajectory_recompute_match": None,
            "error_spectrum_recompute_match": None,
            "all_ok": False,
            "note": "diagnostics are built after resume completes",
        },
        "snapshot": None,
        "outputs": {
            "report": str(root / "runs" / job_id / "report.json"),
            "checkpoint": checkpoint_summary["path"],
            "snapshot": None,
        },
    }


def _finalize_report(
    root: Path,
    report: dict[str, Any],
    registry: JobRegistry,
) -> dict[str, Any]:
    job_id = report["job_id"]
    report_path = root / "runs" / job_id / "report.json"
    _atomic_write_json(report_path, report, pretty=True)
    _atomic_write_json(root / "cache" / "last_report.json", report, pretty=True)
    registry.update(job_id, status=report["status"], report_path=str(report_path))
    if report.get("business_error"):
        record_business_error(
            root,
            WorkbenchError(
                report["business_error"]["code"],
                report["business_error"]["message"],
                report["business_error"].get("details", {}),
            ),
        )
    else:
        clear_business_error(root)
    return report


def run_job(
    *,
    root: str | Path,
    config: TrainingConfig,
    samples_path: str | Path,
    stop_after_rounds: int | None = None,
    interrupt_round: int | None = None,
    interrupt_after_chunk: int | None = None,
    verify_recompute: bool = True,
    raise_on_business_error: bool = False,
) -> dict[str, Any]:
    base = Path(root)
    prepare_directories(base)
    registry = JobRegistry(base / "runs")
    try:
        dataset = load_dataset(samples_path, config)
        split = make_stratified_split(dataset, config)
        cfg_hash = config_sha256(config)
        registry.register(
            config.job_id,
            input_sha256=dataset.input_sha256,
            config_sha256=cfg_hash,
        )
    except WorkbenchError as error:
        record_business_error(base, error)
        raise

    prepared = _primary_prepared(dataset, split, config)
    checkpoint_store = CheckpointStore(base / "runs")
    checkpoint_path = str(checkpoint_store.path(config.job_id))

    def commit(state: dict[str, Any]) -> None:
        checkpoint_store.commit(
            config.job_id,
            input_sha256=dataset.input_sha256,
            config_sha256=cfg_hash,
            state=state,
        )

    run = train_prepared(
        prepared,
        config,
        reduce_mode="chunked",
        run_id=f"{config.precision}/chunked",
        commit_callback=commit,
        stop_after_rounds=stop_after_rounds,
        interrupt_round=interrupt_round,
        interrupt_after_chunk=interrupt_after_chunk,
        collect_chunk_details=True,
    )
    checkpoint_summary = _checkpoint_summary(run, checkpoint_path)
    if run.interrupted:
        report = _partial_report(
            root=base,
            job_id=config.job_id,
            config=config,
            dataset=dataset,
            split=split,
            primary_run=run,
            checkpoint_summary=checkpoint_summary,
        )
        return _finalize_report(base, report, registry)

    diagnostics = build_diagnostics(
        dataset,
        split,
        config,
        verify_recompute=verify_recompute,
    )
    _assert_primary_matches_diagnostic_run(run, diagnostics, config)
    business_error: WorkbenchError | None = None
    try:
        require_tolerance_consistency(diagnostics)
    except WorkbenchError as error:
        business_error = error

    snapshot_summary = _commit_snapshots(
        SnapshotStore(base / "snapshots"),
        config.job_id,
        config,
        dataset,
        diagnostics,
    )
    report = _assemble_complete_report(
        root=base,
        job_id=config.job_id,
        config=config,
        dataset=dataset,
        split=split,
        primary_run=run,
        diagnostics=diagnostics,
        checkpoint_summary=checkpoint_summary,
        snapshot_summary=snapshot_summary,
        business_error=business_error,
    )
    finalized = _finalize_report(base, report, registry)
    if business_error is not None and raise_on_business_error:
        raise business_error
    return finalized


def resume_job(
    *,
    root: str | Path,
    config: TrainingConfig,
    samples_path: str | Path,
    verify_recompute: bool = True,
    raise_on_business_error: bool = False,
) -> dict[str, Any]:
    base = Path(root)
    prepare_directories(base)
    registry = JobRegistry(base / "runs")
    try:
        record = registry.get(config.job_id)
        dataset = load_dataset(samples_path, config)
        split = make_stratified_split(dataset, config)
        cfg_hash = config_sha256(config)
        if record["input_sha256"] != dataset.input_sha256:
            raise WorkbenchError(
                "E_RESUME_MISMATCH",
                "registered job input hash does not match the requested sample file",
                {"field": "input_sha256"},
            )
        if record["config_sha256"] != cfg_hash:
            raise WorkbenchError(
                "E_RESUME_MISMATCH",
                "registered job configuration hash does not match the requested configuration",
                {"field": "config_sha256"},
            )
        prepared = _primary_prepared(dataset, split, config)
        checkpoint_store = CheckpointStore(base / "runs")
        checkpoint_path = str(checkpoint_store.path(config.job_id))
        initial_state = checkpoint_store.load(
            config.job_id,
            input_sha256=dataset.input_sha256,
            config_sha256=cfg_hash,
            precision=config.precision,
            reduce_mode="chunked",
            chunk_plan_sha256=str(prepared.chunk_plan_fingerprint["sha256"]),
            train_index_sha256=prepared.sample_fingerprint["train_sha256"],
        )
        if initial_state.get("stop_reason") in {
            "converged",
            "max_iterations",
            "completed",
        }:
            raise WorkbenchError(
                "E_RESUME_NOT_AVAILABLE",
                "the checkpoint belongs to an already completed run",
                {
                    "job_id": config.job_id,
                    "stop_reason": initial_state.get("stop_reason"),
                    "rounds_completed": initial_state.get("rounds_completed"),
                },
            )
        registry.update(config.job_id, status="RESUMING")
    except WorkbenchError as error:
        record_business_error(base, error)
        raise

    def commit(state: dict[str, Any]) -> None:
        checkpoint_store.commit(
            config.job_id,
            input_sha256=dataset.input_sha256,
            config_sha256=cfg_hash,
            state=state,
        )

    run = train_prepared(
        prepared,
        config,
        reduce_mode="chunked",
        run_id=f"{config.precision}/chunked",
        initial_state=initial_state,
        commit_callback=commit,
        collect_chunk_details=True,
    )
    checkpoint_summary = _checkpoint_summary(run, checkpoint_path)
    diagnostics = build_diagnostics(
        dataset,
        split,
        config,
        verify_recompute=verify_recompute,
    )
    _assert_primary_matches_diagnostic_run(run, diagnostics, config)
    business_error: WorkbenchError | None = None
    try:
        require_tolerance_consistency(diagnostics)
    except WorkbenchError as error:
        business_error = error
    snapshot_summary = _commit_snapshots(
        SnapshotStore(base / "snapshots"),
        config.job_id,
        config,
        dataset,
        diagnostics,
    )
    report = _assemble_complete_report(
        root=base,
        job_id=config.job_id,
        config=config,
        dataset=dataset,
        split=split,
        primary_run=run,
        diagnostics=diagnostics,
        checkpoint_summary=checkpoint_summary,
        snapshot_summary=snapshot_summary,
        business_error=business_error,
    )
    report["execution"]["resumed"] = True
    finalized = _finalize_report(base, report, registry)
    if business_error is not None and raise_on_business_error:
        raise business_error
    return finalized
