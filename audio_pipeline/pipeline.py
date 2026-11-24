"""End-to-end deterministic loudness delivery pipeline."""

from __future__ import annotations

import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .anomalies import normalize_anomalies, trace_anomalies
from .config import load_task
from .decision import decide
from .errors import PipelineError
from .measurement import (
    StopController,
    measure_input,
)
from .models import (
    Decision,
    InputResult,
    InputSpec,
    Measurement,
    RunOutcome,
    TaskSpec,
    UnifiedView,
)
from .output import commit_group, read_existing_state, serialize_report
from .scheduler import (
    BudgetManager,
    ScheduledItem,
    run_bounded,
)
from .snapshots import merge_snapshots, snapshot_source_index
from .state import StateStore
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    ensure_inside,
    load_json,
    rounded,
    sha256_bytes,
)
from .wavio import (
    WavInfo,
    decode_wav,
    encode_wav_bytes,
    inspect_wav,
    resample_rational,
)


@dataclass
class PipelineOptions:
    workspace_root: Path | None = None
    output_dir: Path | None = None
    state_dir: Path | None = None
    resume: bool = False
    simulate_interrupt_after_chunks: int | None = None


@dataclass
class _InputContext:
    spec: InputSpec
    info: WavInfo
    defaults: Any
    view: UnifiedView
    state: StateStore
    stop: StopController
    interrupt_after_chunks: int | None


@dataclass
class _WorkValue:
    result: InputResult
    wav_bytes: bytes
    expected_frames: int


def _signal_exit_code(signum: int) -> int:
    return 128 + int(signum)


def _dedupe_codes(codes: list[str]) -> list[str]:
    result: list[str] = []
    for code in codes:
        if code not in result:
            result.append(code)
    return result


def _header_errors(spec: InputSpec, info: WavInfo,
                   task: TaskSpec) -> list[PipelineError]:
    errors: list[PipelineError] = []

    def add(code: str, message: str) -> None:
        if not any(error.code == code for error in errors):
            errors.append(PipelineError(code, message, input_id=spec.input_id))

    if info.is_float or info.bits_per_sample not in (16, 24):
        add("E_BIT_DEPTH",
            f"WAV bit depth {info.bits_per_sample} is not 16 or 24")
    elif info.container_bits != info.bits_per_sample:
        add("E_BIT_DEPTH",
            f"WAV container width {info.container_bits} != valid width "
            f"{info.bits_per_sample}")
    elif info.bits_per_sample not in task.allowed_bit_depths:
        add("E_BIT_DEPTH",
            f"WAV bit depth {info.bits_per_sample} is not in the allowed set")
    if info.sample_rate not in task.allowed_sample_rates:
        add("E_SR", f"sample rate {info.sample_rate} is not allowed")
    if spec.declared_sample_rate is not None and info.sample_rate != spec.declared_sample_rate:
        add(
            "E_SR",
            f"declared sample rate {spec.declared_sample_rate} != WAV {info.sample_rate}",
        )
    if spec.declared_bit_depth is not None and info.bits_per_sample != spec.declared_bit_depth:
        add(
            "E_BIT_DEPTH",
            f"declared bit depth {spec.declared_bit_depth} != WAV {info.bits_per_sample}",
        )
    if info.channels != spec.channels:
        add(
            "E_CHANNELS",
            f"WAV has {info.channels} channels but layout {spec.layout} needs {spec.channels}",
        )
    if info.frames <= 0:
        add("E_WAV", "WAV contains no audio frames")
    return errors


def _validate_inputs(task: TaskSpec) -> list[tuple[InputSpec, WavInfo | None,
                                                     list[PipelineError]]]:
    validated: list[tuple[InputSpec, WavInfo | None, list[PipelineError]]] = []
    for spec in task.inputs:
        errors = list(spec.config_errors)
        info: WavInfo | None = None
        if errors:
            validated.append((spec, info, errors))
            continue
        try:
            ensure_inside(task.workspace_root, spec.path)
            info = inspect_wav(spec.path)
            errors = _header_errors(spec, info, task)
        except PipelineError as exc:
            if exc.input_id is None:
                exc.input_id = spec.input_id
            errors.append(exc)
        validated.append((spec, info, errors))
    return validated


def _result_from_validation(spec: InputSpec, info: WavInfo | None,
                            errors: list[PipelineError]) -> InputResult:
    result = InputResult(
        order=spec.order,
        input_id=spec.input_id,
        canonical_id=spec.canonical_id,
        path=str(spec.path),
        status="rejected" if errors else "pending",
        codes=_dedupe_codes([error.code for error in errors]),
        errors=[error.as_dict() for error in errors],
    )
    if info is not None:
        result.diagnostics["header"] = {
            "frames": info.frames,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "bit_depth": info.bits_per_sample,
            "container_bits": info.container_bits,
            "sha256": info.sha256,
        }
    return result


def _snapshot_issues_for(view: UnifiedView, spec: InputSpec) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for issue in view.issues:
        issue_input = issue.get("input_id")
        if issue_input in (spec.canonical_id, spec.input_id):
            result.append(issue)
    return result


def _process_input(item: ScheduledItem) -> _WorkValue:
    context: _InputContext = item.payload
    spec = context.spec
    info = context.info
    defaults = context.defaults
    stop = context.stop
    stop.boundary(input_id=spec.input_id, boundary="input-start")
    measurement, samples, state_issues, chunks = measure_input(
        spec=spec,
        info=info,
        defaults=defaults,
        view=context.view,
        state=context.state,
        stop=stop,
        interrupt_after_chunks=context.interrupt_after_chunks,
    )
    stop.boundary(input_id=spec.input_id, boundary="input-complete")
    decision = decide(measurement, context.view, defaults=defaults)
    result = InputResult(
        order=spec.order,
        input_id=spec.input_id,
        canonical_id=spec.canonical_id,
        path=str(spec.path),
        status="measured",
        codes=[measurement.code] if measurement.code else [],
        reused=measurement.measurement_reused,
        measurement=measurement,
        decision=decision,
    )
    result.diagnostics["header"] = {
        "frames": info.frames,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "bit_depth": info.bits_per_sample,
        "container_bits": info.container_bits,
        "sha256": info.sha256,
    }
    if state_issues:
        result.codes.append("E_DIGEST")
        result.diagnostics["state_issues"] = state_issues
    if chunks:
        result.diagnostics["chunks_written"] = chunks
    snapshot_issues = _snapshot_issues_for(context.view, spec)
    if snapshot_issues:
        result.diagnostics["snapshot_issues"] = snapshot_issues
        for issue in snapshot_issues:
            result.codes.append(str(issue.get("code", "E_SNAPSHOT")))

    if samples is None:
        # A complete cached measurement can be reused, but the deliverable still
        # needs sample-domain resampling.
        samples = decode_wav(info, block_frames=defaults.decode_block_frames)
    resampled = resample_rational(
        samples, info.sample_rate, defaults.target_sample_rate
    )
    expected_frames = int(round(
        info.frames * defaults.target_sample_rate / info.sample_rate
    ))
    actual_frames = int(resampled.shape[0])
    if abs(actual_frames - expected_frames) > 1:
        raise PipelineError(
            "E_RUNTIME",
            f"resampled frame count {actual_frames} differs from theoretical "
            f"{expected_frames} by more than one frame",
            input_id=spec.input_id,
            recoverable=False,
        )
    wav_bytes = encode_wav_bytes(
        resampled, defaults.target_sample_rate, defaults.output_bit_depth
    )
    result.output = {
        "file": f"{spec.input_id}.wav",
        "sample_rate": defaults.target_sample_rate,
        "bit_depth": defaults.output_bit_depth,
        "frame_count": actual_frames,
        "theoretical_frame_count": expected_frames,
        "frame_count_error": actual_frames - expected_frames,
    }
    result.codes = _dedupe_codes(result.codes)
    return _WorkValue(result=result, wav_bytes=wav_bytes,
                      expected_frames=expected_frames)


def _measurement_dict(measurement: Measurement | None) -> dict[str, Any] | None:
    if measurement is None:
        return None
    return {
        "source_sample_rate": measurement.source_sample_rate,
        "channels": measurement.channels,
        "source_bit_depth": measurement.source_bit_depth,
        "frames": measurement.frames,
        "window_frames": measurement.window_frames,
        "hop_frames": measurement.hop_frames,
        "window_ms": rounded(measurement.window_ms, 6),
        "hop_ms": rounded(measurement.hop_ms, 6),
        "partial_window_policy": measurement.partial_window_policy,
        "relative_gate_lu": measurement.relative_gate_lu,
        "absolute_gate_lufs": measurement.absolute_gate_lufs,
        "total_windows": measurement.total_windows,
        "abs_gated_window_count": measurement.abs_gated_window_count,
        "gated_window_count": measurement.gated_window_count,
        "integrated_lufs": measurement.integrated_lufs,
        "short_term_peak_lufs": measurement.short_term_peak_lufs,
        "short_term_floor_lufs": measurement.short_term_floor_lufs,
        "dynamic_range_db": measurement.dynamic_range_db,
        "silent": measurement.silent,
        "measurement_reused": measurement.measurement_reused,
    }


def _decision_dict(decision: Decision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "action": decision.action,
        "gain_db": decision.gain_db,
        "limiter": decision.limiter,
        "target_lufs": decision.target_lufs,
        "dynamic_range_min_db": decision.dynamic_range_min_db,
        "dynamic_range_max_db": decision.dynamic_range_max_db,
        "dynamic_range_verdict": decision.dynamic_range_verdict,
        "compression_threshold_lufs": decision.compression_threshold_lufs,
        "compression_ratio": decision.compression_ratio,
        "reasons": decision.reasons,
        "decision_revision": decision.decision_revision,
    }


def _result_dict(result: InputResult) -> dict[str, Any]:
    measurement = result.measurement
    decision = result.decision
    header = result.diagnostics.get("header", {})
    input_frames = (
        measurement.frames if measurement is not None
        else header.get("frames") if isinstance(header, dict) else None
    )
    return {
        "order": result.order,
        "input_id": result.input_id,
        "canonical_id": result.canonical_id,
        "path": result.path,
        "status": result.status,
        "business_codes": _dedupe_codes(
            [code for code in result.codes if code.startswith("E_")]
        ),
        "errors": result.errors,
        "anomaly_ids": result.anomaly_ids,
        "input_frames": input_frames,
        "window_count": measurement.total_windows if measurement else None,
        "threshold_window_count": (
            measurement.gated_window_count if measurement else None
        ),
        "weighted_loudness_lufs": (
            measurement.integrated_lufs if measurement else None
        ),
        "dynamic_range_verdict": (
            decision.dynamic_range_verdict if decision else None
        ),
        "dynamic_range": (
            {
                "min_db": decision.dynamic_range_min_db,
                "max_db": decision.dynamic_range_max_db,
                "range_db": (
                    measurement.dynamic_range_db if measurement else None
                ),
                "verdict": decision.dynamic_range_verdict,
            }
            if decision else None
        ),
        "suggested_action": decision.action if decision else None,
        "gain_adjustment_db": decision.gain_db if decision else None,
        "limiter": decision.limiter if decision else None,
        "diagnostics": result.diagnostics,
        "measurement": _measurement_dict(result.measurement),
        "decision": _decision_dict(result.decision),
        "output": result.output,
    }


def _global_issues(view: UnifiedView, anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = list(view.issues)
    for anomaly in anomalies:
        if not anomaly.get("valid_kind", True):
            issues.append({
                "code": "E_ANOMALY",
                "message": f"unknown anomaly kind {anomaly.get('kind')!r}",
                "source": {"kind": "anomaly", "injection_id": anomaly["injection_id"]},
                "recoverable": True,
            })
    return issues


def _output_version(task: TaskSpec, view: UnifiedView,
                    validated: list[tuple[InputSpec, WavInfo | None,
                                           list[PipelineError]]],
                    anomalies: list[dict[str, Any]],
                    runtime_issues: list[dict[str, Any]] | None = None) -> str:
    descriptor = {
        "schema": "delivery-output-version/1",
        "pipeline_version": __version__,
        "task_revision": task.task_revision,
        "target_sample_rate": task.measurement.target_sample_rate,
        "output_bit_depth": task.measurement.output_bit_depth,
        "measurement_revision": view.measurement_revision,
        "decision_revision": view.decision_revision,
        "snapshot_summary": view.snapshot_summary,
        "anomalies": anomalies,
        "runtime_issues": runtime_issues or [],
        "inputs": [
            {
                "id": spec.input_id,
                "canonical_id": spec.canonical_id,
                "sha256": info.sha256 if info else None,
                "frames": info.frames if info else None,
                "sample_rate": info.sample_rate if info else None,
                "bit_depth": info.bits_per_sample if info else None,
                "codes": [error.code for error in errors],
            }
            for spec, info, errors in validated
        ],
    }
    return sha256_bytes(canonical_json_bytes(descriptor))


def _build_report(
    *,
    task: TaskSpec,
    view: UnifiedView,
    results: list[InputResult],
    anomalies: list[dict[str, Any]],
    global_issues: list[dict[str, Any]],
    version: str,
    exit_code: int,
    scheduler: dict[str, Any],
    interruption: dict[str, Any] | None = None,
    first_error: PipelineError | None = None,
) -> dict[str, Any]:
    measured = [result for result in results if result.status == "measured"]
    rejected = [result for result in results if result.status == "rejected"]
    failed = [
        result for result in results
        if result.status in ("failed", "cancelled", "not_processed", "interrupted")
    ]
    traced = trace_anomalies(anomalies, results, view)
    report_status = "ok"
    if exit_code >= 128:
        report_status = "interrupted"
    elif first_error is not None or failed:
        report_status = "failed"
    elif rejected or global_issues or any(
        item["status"] != "detected" for item in traced
    ):
        report_status = "degraded"
    elif exit_code == 3:
        report_status = "degraded"
    report = {
        "schema": task.report_schema,
        "pipeline_version": __version__,
        "task_id": task.task_id,
        "task_revision": task.task_revision,
        "output_version": version,
        "status": report_status,
        "exit_code": exit_code,
        "summary": {
            "declared_inputs": len(task.inputs),
            "measured_inputs": len(measured),
            "rejected_inputs": len(rejected),
            "failed_inputs": len(failed),
            "interrupted_inputs": sum(
                1 for result in results if result.status == "interrupted"
            ),
            "measurement_reused_inputs": sum(
                1 for result in measured if result.reused
            ),
            "criteria_conflicts": len(view.conflicts),
            "anomalies": len(traced),
            "anomalies_detected": sum(
                1 for item in traced if item["status"] == "detected"
            ),
        },
        "target": {
            "sample_rate": task.measurement.target_sample_rate,
            "bit_depth": task.measurement.output_bit_depth,
            "window_ms": task.measurement.window_ms,
            "hop_ms": task.measurement.hop_ms,
            "relative_gate_lu": task.measurement.relative_gate_lu,
            "absolute_gate_lufs": task.measurement.absolute_gate_lufs,
            "partial_window_policy": task.measurement.partial_window_policy,
        },
        "criteria": {
            "snapshot_summary": view.snapshot_summary,
            "unified_view": snapshot_source_index(view),
            "conflicts": list(view.conflicts),
            "issues": global_issues,
        },
        "anomaly_injections": traced,
        "scheduler": scheduler,
        "interruption": interruption,
        "first_error": first_error.as_dict() if first_error else None,
        "inputs": [_result_dict(result) for result in results],
    }
    return report


def run_pipeline(task_path: Path, options: PipelineOptions | None = None) -> RunOutcome:
    options = options or PipelineOptions()
    task = load_task(task_path, options.workspace_root)
    workspace = task.workspace_root
    ensure_inside(workspace, task.path)
    output_option = options.output_dir
    state_option = options.state_dir
    output_dir = (
        output_option if output_option is not None and output_option.is_absolute()
        else workspace / (output_option or "output")
    ).resolve()
    state_dir = (
        state_option if state_option is not None and state_option.is_absolute()
        else workspace / (state_option or "state")
    ).resolve()
    ensure_inside(workspace, output_dir)
    ensure_inside(workspace, state_dir)
    ensure_inside(workspace, task.snapshots.v1_path)
    ensure_inside(workspace, task.snapshots.v2_db_path)
    ensure_inside(workspace, task.snapshots.v2_shard_root)
    if task.anomaly_file is not None:
        ensure_inside(workspace, task.anomaly_file)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    state = StateStore(state_dir)
    stop = StopController.create()
    stop.interrupt_after_chunks = options.simulate_interrupt_after_chunks
    old_handlers: dict[int, Any] = {}

    def handler(signum, _frame):
        stop.request_signal(int(signum))

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass

    try:
        view = merge_snapshots(task)
        anomalies = normalize_anomalies(task.anomaly_items)
        validated = _validate_inputs(task)
        version = _output_version(task, view, validated, anomalies)
        if not view.snapshot_summary["v1"]["valid"] and not view.snapshot_summary[
            "v2"
        ]["valid"]:
            raise PipelineError(
                "E_DIGEST", "no valid measurement criteria snapshot remains",
                recoverable=False,
            )

        results_by_order: dict[int, InputResult] = {}
        work_by_order: dict[int, _WorkValue] = {}
        for spec, info, errors in validated:
            results_by_order[spec.order] = _result_from_validation(spec, info, errors)

        schedulable: list[ScheduledItem] = []
        for spec, info, errors in validated:
            if errors or info is None:
                continue
            output_frames = int(round(
                info.frames * task.measurement.target_sample_rate / info.sample_rate
            ))
            decode_bytes = min(
                info.data_size,
                task.measurement.decode_block_frames * info.block_align * 4 + 4096,
            )
            sample_bytes = int(
                (info.frames + 2 * output_frames + 131072)
                * info.channels * np.dtype(np.float32).itemsize
            )
            schedulable.append(ScheduledItem(
                order=spec.order,
                input_id=spec.input_id,
                decode_bytes=max(1, decode_bytes),
                sample_bytes=max(1, sample_bytes),
                payload=_InputContext(
                    spec=spec,
                    info=info,
                    defaults=task.measurement,
                    view=view,
                    state=state,
                    stop=stop,
                    interrupt_after_chunks=options.simulate_interrupt_after_chunks,
                ),
            ))

        budget = BudgetManager(
            task.measurement.decode_buffer_bytes,
            task.measurement.sample_buffer_bytes,
        )
        scheduled_results, first_error, scheduler_metrics = run_bounded(
            schedulable,
            _process_input,
            max_workers=task.measurement.max_workers,
            budget=budget,
            stop=stop,
        )
        for scheduled in scheduled_results:
            if scheduled.value is not None:
                work_by_order[scheduled.order] = scheduled.value
                results_by_order[scheduled.order] = scheduled.value.result
            elif scheduled.error is not None:
                current = results_by_order.get(scheduled.order)
                if current is None:
                    continue
                current.status = "failed" if not scheduled.error.recoverable else "rejected"
                current.codes.append(scheduled.error.code)
                current.errors.append(scheduled.error.as_dict())
        for spec, _info, _errors in validated:
            result = results_by_order[spec.order]
            if result.status == "pending":
                result.status = "cancelled" if stop.signum else "not_processed"

        scheduler_metrics = {
            **scheduler_metrics,
            "max_workers": task.measurement.max_workers,
            "scheduled_inputs": len(schedulable),
        }
        for result in results_by_order.values():
            if result.output is not None:
                result.output["path"] = str(
                    output_dir / str(result.output["file"])
                )
        signum = stop.signum
        if signum is not None:
            first_error = None
            exit_code = _signal_exit_code(signum)
            interruption = {
                "signal": signum,
                "boundary": stop.last_boundary,
                "input_id": stop.last_input_id,
                "details": stop.last_details,
                "marker": str(state_dir / "interrupted.json"),
                "resume_supported": True,
            }
            marker = {
                "schema": "pipeline-interrupt/1",
                "task_id": task.task_id,
                "signal": signum,
                "exit_code": exit_code,
                "boundary": stop.last_boundary,
                "input_id": stop.last_input_id,
                "details": stop.last_details,
                "persisted_chunks_only": True,
            }
            state.write_interrupt_marker(marker)
            results = [results_by_order[index] for index in sorted(results_by_order)]
            for result in results:
                if result.status in ("pending", "cancelled", "not_processed", "failed"):
                    result.status = "interrupted"
            report = _build_report(
                task=task,
                view=view,
                results=results,
                anomalies=anomalies,
                global_issues=_global_issues(view, anomalies),
                version=version,
                exit_code=exit_code,
                scheduler=scheduler_metrics,
                interruption=interruption,
                first_error=first_error,
            )
            partial_path = state_dir / "partial_report.json"
            atomic_write_json(partial_path, report)
            state.record_run(version, "interrupted", partial_path, interruption)
            return RunOutcome(exit_code, report, partial_path, interrupted=True)

        results = [results_by_order[index] for index in sorted(results_by_order)]
        runtime_issues = []
        for result in results:
            if result.diagnostics.get("state_issues"):
                runtime_issues.append({
                    "input_id": result.input_id,
                    "codes": [
                        issue.get("code")
                        for issue in result.diagnostics["state_issues"]
                    ],
                    "chunks": [
                        issue.get("source", {}).get("chunk_index")
                        for issue in result.diagnostics["state_issues"]
                    ],
                })
        if runtime_issues:
            version = _output_version(
                task, view, validated, anomalies, runtime_issues
            )
        global_issues = _global_issues(view, anomalies)
        traced_preview = trace_anomalies(anomalies, results, view)
        if any(item["status"] != "detected" for item in traced_preview):
            global_issues.append({
                "code": "E_ANOMALY",
                "message": "one or more declared anomaly injections were not detected",
                "source": {"kind": "anomaly-manifest"},
                "recoverable": True,
            })
        degraded = (
            any(result.status in ("rejected", "failed", "cancelled", "not_processed")
                for result in results)
            or any(
                code not in ("E_SILENT", "E_BELOW_GATE")
                for result in results for code in result.codes
            )
            or any(issue.get("code", "").startswith("E_") for issue in global_issues)
        )
        exit_code = 4 if first_error is not None else (3 if degraded else 0)
        report = _build_report(
            task=task,
            view=view,
            results=results,
            anomalies=anomalies,
            global_issues=global_issues,
            version=version,
            exit_code=exit_code,
            scheduler=scheduler_metrics,
            first_error=first_error,
        )
        if first_error is not None:
            partial_path = state_dir / "partial_report.json"
            atomic_write_json(partial_path, report)
            state.record_run(version, "failed", partial_path, first_error.as_dict())
            return RunOutcome(4, report, partial_path, interrupted=False)
        report_bytes = serialize_report(report)
        wav_payloads = {
            result.input_id: work_by_order[result.order].wav_bytes
            for result in results
            if result.status == "measured" and result.order in work_by_order
        }
        expected_frames = {
            result.input_id: work_by_order[result.order].expected_frames
            for result in results
            if result.status == "measured" and result.order in work_by_order
        }
        existing_paths = {
            input_id: output_dir / f"{input_id}.wav"
            for input_id in wav_payloads
        }
        existing = read_existing_state(output_dir / "report.json", existing_paths)
        try:
            commit = commit_group(
                output_dir,
                wav_payloads=wav_payloads,
                expected_frames=expected_frames,
                report_bytes=report_bytes,
                version=version,
                existing=existing,
            )
        except PipelineError as exc:
            partial = {
                **report,
                "status": "failed",
                "exit_code": 4,
                "first_error": exc.as_dict(),
            }
            partial_path = state_dir / "partial_report.json"
            atomic_write_json(partial_path, partial)
            state.record_run(version, "commit_failed", partial_path, exc.as_dict())
            return RunOutcome(4, partial, partial_path, interrupted=False)
        state.clear_interrupt_marker()
        state.record_run(version, "complete", output_dir / "report.json", {
            "exit_code": exit_code,
            "commit": commit,
        })
        committed_report = report
        if commit.get("skipped"):
            try:
                loaded = load_json(output_dir / "report.json", code="E_IO")
                if isinstance(loaded, dict):
                    committed_report = loaded
            except PipelineError:
                committed_report = report
        return RunOutcome(exit_code, committed_report, output_dir / "report.json")
    finally:
        state.close()
        for signum, old_handler in old_handlers.items():
            try:
                signal.signal(signum, old_handler)
            except (ValueError, OSError):
                pass
