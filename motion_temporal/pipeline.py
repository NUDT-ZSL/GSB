"""Four-stage orchestrator with checkpoint recovery and deterministic reports."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .aggregation import aggregate_temporal
from .attribution import diagnose_attribution
from .canonical import canonical_bytes, sha256_bytes
from .checkpoint import CheckpointStore
from .codes import (
    E_INCONSISTENT,
    E_INPUT_INVALID,
    E_RESOURCE_LIMIT,
    E_OK,
    S_STOPPED,
    STAGES,
)
from .config import PipelineConfig, make_run_id, parse_model_revision
from .encoding import encode_motion
from .errors import BusinessError, GracefulStop
from .generator import GeneratedClip
from .injections import (
    InjectionSpec,
    apply_frame_injections,
    inject_aggregation_payload,
    inject_output_mask,
    output_drop_record,
)
from .metrics import evaluate_quality
from .models import ClipSpec, FrameSample
from .output import build_output, drop_output_frame
from .sampling import sampling_payload
from .selfcheck import validate_pipeline
from .snapshots import SnapshotStore


def _normalize_injection_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stage_order = {stage: index for index, stage in enumerate(STAGES)}
    ordered = sorted(records, key=lambda item: (stage_order.get(item["stage"], 99), item["frame_index"], item["kind"]))
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(ordered, start=1):
        item = dict(record)
        item["injection_index"] = index
        normalized.append(item)
    return normalized


def _resource_adjusted(
    spec: ClipSpec,
    frames: list[FrameSample],
    config: PipelineConfig,
) -> tuple[ClipSpec, list[FrameSample], PipelineConfig, list[dict[str, Any]]]:
    degradations: list[dict[str, Any]] = []
    effective_spec = spec
    effective_frames = list(frames)
    effective_config = replace(config)
    if len(frames) > config.max_sequence_frames:
        if config.resource_policy == "error":
            raise BusinessError(
                E_RESOURCE_LIMIT,
                f"sequence frames {len(frames)} exceed limit {config.max_sequence_frames}",
                spec.clip_id,
                config.max_sequence_frames,
                {"limit": "max_sequence_frames", "actual": len(frames), "max": config.max_sequence_frames},
            )
        effective_frames = effective_frames[: config.max_sequence_frames]
        effective_spec = replace(spec, frame_count=config.max_sequence_frames)
        degradations.append(
            {
                "category": "sequence_truncated",
                "limit": config.max_sequence_frames,
                "actual": len(frames),
                "applied": config.max_sequence_frames,
            }
        )
    if config.window_length > config.max_history_windows:
        if config.resource_policy == "error":
            raise BusinessError(
                E_RESOURCE_LIMIT,
                f"history window {config.window_length} exceeds limit {config.max_history_windows}",
                spec.clip_id,
                0,
                {
                    "limit": "max_history_windows",
                    "actual": config.window_length,
                    "max": config.max_history_windows,
                },
            )
        effective_config.window_length = config.max_history_windows
        degradations.append(
            {
                "category": "history_window_truncated",
                "limit": config.max_history_windows,
                "actual": config.window_length,
                "applied": config.max_history_windows,
            }
        )
    if len(effective_frames) != effective_spec.frame_count:
        missing = sorted(set(range(effective_spec.frame_count)) - {frame.frame_index for frame in effective_frames})
        raise BusinessError(
            E_INPUT_INVALID,
            "frame list does not match frame_count",
            effective_spec.clip_id,
            missing[0] if missing else None,
        )
    return effective_spec, effective_frames, effective_config, degradations


def _stage_checksum(stage: str, payload: dict[str, Any] | None) -> str | None:
    return None if payload is None else payload.get("content_checksum")


def _snapshot_summary(store: SnapshotStore | None) -> dict[str, Any] | None:
    if store is None:
        return None
    try:
        return {
            category: {
                "version": envelope["version"],
                "content_checksum": envelope["content_checksum"],
            }
            for category, envelope in store.verify_all().items()
        }
    except BusinessError:
        return None


def _error_report(
    *,
    spec: ClipSpec | None,
    run_id: str | None,
    error: BusinessError,
    injection_plan: list[dict[str, Any]],
    injection_records: list[dict[str, Any]],
    degradations: list[dict[str, Any]],
    stage_payloads: dict[str, dict[str, Any]],
    checkpoint_summary: dict[str, Any] | None,
    snapshot_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "component": "motion-temporal",
        "clip_id": spec.clip_id if spec is not None else error.clip_id,
        "run_id": run_id,
        "model_revision": spec.model_revision if spec is not None else None,
        "temporal_policy": spec.temporal_policy if spec is not None else None,
        "motion_mode": spec.motion_mode if spec is not None else None,
        "seed": spec.seed if spec is not None else None,
        "frame_interval_ms": spec.frame_interval_ms if spec is not None else None,
        "object_count": spec.object_count if spec is not None else None,
        "canvas": [spec.canvas_height, spec.canvas_width] if spec is not None else None,
        "status": "error",
        "business_code": error.code,
        "error": error.to_dict(),
        "injection_records": _normalize_injection_records(injection_records),
        "injection_plan": injection_plan,
        "attribution": [],
        "metrics": None,
        "degradations": degradations,
        "frames": stage_payloads.get("output", {}).get("frames", []),
        "partial_stage_checksums": {
            stage: _stage_checksum(stage, payload) for stage, payload in stage_payloads.items()
        },
        "selfcheck": None,
        "checkpoints": checkpoint_summary,
        "snapshots": snapshot_summary,
    }


def run_clip(
    clip: GeneratedClip,
    cache_dir: str | Path,
    *,
    config: PipelineConfig | None = None,
    injections: Sequence[InjectionSpec] = (),
    stop_at: dict[str, Any] | None = None,
    delay_ms: float = 0.0,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run or resume one clip and always return a deterministic JSON-safe report."""

    config = config or PipelineConfig()
    injection_specs = list(injections)
    injection_plan = [injection.to_dict() for injection in injection_specs]
    spec = clip.spec
    degradation: list[dict[str, Any]] = []
    injection_records: list[dict[str, Any]] = []
    stage_payloads: dict[str, dict[str, Any]] = {}
    checkpoint_store: CheckpointStore | None = None
    snapshot_store: SnapshotStore | None = None
    run_id = make_run_id(spec, config, injection_plan)
    try:
        effective_spec, frames, effective_config, degradation = _resource_adjusted(spec, list(clip.frames), config)
        run_id = make_run_id(effective_spec, effective_config, injection_plan)
        frame_sequence = sorted(range(effective_spec.frame_count))
        checkpoint_store = CheckpointStore(cache_dir, run_id)
        snapshot_root = Path(snapshot_dir) if snapshot_dir is not None else Path(cache_dir) / "snapshots"
        snapshot_store = SnapshotStore(snapshot_root / run_id)
        revision_version = parse_model_revision(effective_spec.model_revision)
        snapshot_store.put(
            "model_parameters",
            revision_version,
            {
                "model_revision": effective_spec.model_revision,
                "encoder": "mask-centroid-v1",
                "aggregator": "recursive-window-v1",
            },
        )
        snapshot_store.put(
            "temporal_policy",
            revision_version,
            {
                "temporal_policy": effective_spec.temporal_policy,
                "frame_interval_ms": effective_spec.frame_interval_ms,
                "window_length": effective_config.window_length,
                "decay": effective_config.decay,
            },
        )

        injected_frames, input_records = apply_frame_injections(effective_spec, frames, injection_specs)
        injection_records.extend(input_records)

        def callback(stage: str):
            def invoke(frame_index: int) -> None:
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                if (
                    stop_at is not None
                    and str(stop_at.get("stage")) == stage
                    and int(stop_at.get("frame", -1)) == frame_index
                ):
                    raise GracefulStop(stage, frame_index, effective_spec.clip_id)

            return invoke

        loaded = checkpoint_store.load_prefix()
        stage_reused: list[str] = []
        stage_computed: list[str] = []

        if "sampling" in loaded:
            sampling = loaded["sampling"]["payload"]
            stage_reused.append("sampling")
        else:
            sampling = sampling_payload(
                effective_spec,
                injected_frames,
                frame_callback=callback("sampling"),
            )
            checkpoint_store.commit("sampling", sampling, frame_sequence)
            stage_computed.append("sampling")
        stage_payloads["sampling"] = sampling

        if "encoding" in loaded:
            encoding = loaded["encoding"]["payload"]
            stage_reused.append("encoding")
        else:
            encoding = encode_motion(
                effective_spec,
                injected_frames,
                sampling,
                frame_callback=callback("encoding"),
            )
            checkpoint_store.commit("encoding", encoding, frame_sequence)
            stage_computed.append("encoding")
        stage_payloads["encoding"] = encoding

        aggregation_input = encoding
        aggregation_records: list[dict[str, Any]] = []
        injection_window = effective_config.window_length
        injection_decay = effective_config.decay
        for injection in injection_specs:
            if injection.stage != "aggregation":
                continue
            if injection.kind == "window_zero":
                injection_window = 0
                aggregation_records.append(
                    {
                        "stage": injection.stage,
                        "kind": injection.kind,
                        "business_code": injection.expected_code,
                        "clip_id": effective_spec.clip_id,
                        "frame_index": injection.frame_index,
                        "params": injection.params,
                        "applied": True,
                    }
                )
            elif injection.kind == "invalid_decay":
                injection_decay = float(injection.params.get("decay", 0.0))
                aggregation_records.append(
                    {
                        "stage": injection.stage,
                        "kind": injection.kind,
                        "business_code": injection.expected_code,
                        "clip_id": effective_spec.clip_id,
                        "frame_index": injection.frame_index,
                        "params": injection.params,
                        "applied": True,
                    }
                )
            elif injection.kind == "extrapolation_oob":
                aggregation_input, record = inject_aggregation_payload(effective_spec, aggregation_input, injection)
                if record is not None:
                    aggregation_records.append(record)
        injection_records.extend(aggregation_records)

        if "aggregation" in loaded:
            aggregation = loaded["aggregation"]["payload"]
            stage_reused.append("aggregation")
        else:
            aggregation = aggregate_temporal(
                effective_spec,
                aggregation_input,
                window_length=injection_window,
                decay=injection_decay,
                max_history_windows=effective_config.max_history_windows,
                extrapolation_margin=effective_config.extrapolation_margin,
                frame_callback=callback("aggregation"),
            )
            aggregation_checkpoint = checkpoint_store.commit("aggregation", aggregation, frame_sequence)
            stage_computed.append("aggregation")
            snapshot_store.put(
                "history_state",
                int(aggregation_checkpoint["checkpoint_version"]),
                {
                    "clip_id": effective_spec.clip_id,
                    "window_length": aggregation["window_length"],
                    "decay": aggregation["decay"],
                    "state_hashes": [frame["state_hash"] for frame in aggregation["frames"]],
                    "history_digests": [frame["history_window"]["digest"] for frame in aggregation["frames"]],
                },
            )
        stage_payloads["aggregation"] = aggregation

        output_aggregation = aggregation
        output_records: list[dict[str, Any]] = []
        for injection in injection_specs:
            if injection.stage == "output" and injection.kind == "mask_shape":
                output_aggregation, record = inject_output_mask(effective_spec, output_aggregation, injection)
                output_records.append(record)
        injection_records.extend(output_records)

        if "output" in loaded:
            output = loaded["output"]["payload"]
            stage_reused.append("output")
            output_was_reused = True
        else:
            output = build_output(
                effective_spec,
                output_aggregation,
                sampling,
                frame_callback=callback("output"),
            )
            drop_records: list[dict[str, Any]] = []
            for injection in injection_specs:
                if injection.stage == "output" and injection.kind == "drop_frame":
                    output = drop_output_frame(output, injection.frame_index)
                    drop_records.append(output_drop_record(effective_spec, injection))
            injection_records.extend(drop_records)
            output_was_reused = False
        stage_payloads["output"] = output

        reused_frames = frame_sequence if output_was_reused else []
        recomputed_frames = [] if output_was_reused else frame_sequence
        selfcheck = validate_pipeline(
            stage_payloads,
            clip_id=effective_spec.clip_id,
            expected_frames=frame_sequence,
            affected_frames=frame_sequence,
            reused_frames=reused_frames,
            recomputed_frames=recomputed_frames,
        )
        if not selfcheck["ok"]:
            raise BusinessError(
                E_INCONSISTENT,
                f"cross-stage selfcheck failed: {selfcheck['categories']}",
                effective_spec.clip_id,
                (selfcheck["issues"][0].get("missing") or [None])[0] if selfcheck["issues"] else None,
                {"selfcheck": selfcheck},
            )

        if not output_was_reused:
            output_checkpoint = checkpoint_store.commit("output", output, frame_sequence)
            stage_computed.append("output")
            snapshot_store.put(
                "history_state",
                int(output_checkpoint["checkpoint_version"]),
                {
                    "clip_id": effective_spec.clip_id,
                    "window_length": aggregation["window_length"],
                    "decay": aggregation["decay"],
                    "state_hashes": [frame["state_hash"] for frame in aggregation["frames"]],
                    "history_digests": [frame["history_window"]["digest"] for frame in aggregation["frames"]],
                    "output_checksum": output["content_checksum"],
                },
            )

        attribution = diagnose_attribution(
            effective_spec,
            aggregation,
            encoding,
            acceleration_threshold=effective_config.acceleration_threshold,
        )
        metrics = evaluate_quality(
            effective_spec,
            output,
            frames,
            hit_iou_threshold=effective_config.hit_iou_threshold,
        )
        prediction_bytes = canonical_bytes(output["frames"])
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "component": "motion-temporal",
            "clip_id": effective_spec.clip_id,
            "run_id": run_id,
            "model_revision": effective_spec.model_revision,
            "temporal_policy": effective_spec.temporal_policy,
            "motion_mode": effective_spec.motion_mode,
            "seed": effective_spec.seed,
            "frame_interval_ms": effective_spec.frame_interval_ms,
            "object_count": effective_spec.object_count,
            "canvas": [effective_spec.canvas_height, effective_spec.canvas_width],
            "status": "ok",
            "business_code": E_OK,
            "error": None,
            "injection_records": _normalize_injection_records(injection_records),
            "injection_plan": injection_plan,
            "attribution": attribution["records"],
            "attribution_checksum": attribution["content_checksum"],
            "metrics": metrics,
            "degradations": degradation,
            "frames": output["frames"],
            "frame_indices": output["frame_indices"],
            "predictions_checksum": sha256_bytes(prediction_bytes),
            "predictions_byte_length": len(prediction_bytes),
            "stage_checksums": {
                stage: _stage_checksum(stage, payload) for stage, payload in stage_payloads.items()
            },
            "selfcheck": selfcheck,
            "reuse_recompute": {
                "reused_frames": reused_frames,
                "recomputed_frames": recomputed_frames,
                "stage_reused": stage_reused,
                "stage_computed": stage_computed,
            },
            "checkpoints": checkpoint_store.summary(),
            "snapshots": _snapshot_summary(snapshot_store),
        }
        return report
    except GracefulStop as stop:
        checkpoint_summary = None
        snapshot_summary = None
        if checkpoint_store is not None:
            try:
                checkpoint_summary = checkpoint_store.summary()
            except BusinessError:
                checkpoint_summary = None
        snapshot_summary = _snapshot_summary(snapshot_store)
        return {
            "schema_version": "1.0",
            "component": "motion-temporal",
            "clip_id": effective_spec.clip_id,
            "run_id": run_id,
            "status": "paused",
            "business_code": S_STOPPED,
            "error": {
                "code": S_STOPPED,
                "message": "graceful frame-boundary stop; committed checkpoints remain reusable",
                "clip_id": effective_spec.clip_id,
                "frame_index": stop.frame_index,
                "stage": stop.stage,
            },
            "injection_records": _normalize_injection_records(injection_records),
            "attribution": [],
            "metrics": None,
            "degradations": degradation,
            "frames": stage_payloads.get("output", {}).get("frames", []),
            "partial_stage_checksums": {
                stage: _stage_checksum(stage, payload) for stage, payload in stage_payloads.items()
            },
            "selfcheck": None,
            "checkpoints": checkpoint_summary,
            "snapshots": snapshot_summary,
        }
    except BusinessError as error:
        checkpoint_summary = None
        snapshot_summary = None
        if checkpoint_store is not None:
            try:
                checkpoint_summary = checkpoint_store.summary()
            except BusinessError:
                checkpoint_summary = None
        snapshot_summary = _snapshot_summary(snapshot_store)
        return _error_report(
            spec=spec,
            run_id=run_id,
            error=error,
            injection_plan=injection_plan,
            injection_records=injection_records,
            degradations=degradation,
            stage_payloads=stage_payloads,
            checkpoint_summary=checkpoint_summary,
            snapshot_summary=snapshot_summary,
        )


def run_batch(
    clips: Iterable[GeneratedClip],
    cache_dir: str | Path,
    *,
    config: PipelineConfig | None = None,
    injections: Sequence[InjectionSpec] = (),
    stop_at: dict[str, Any] | None = None,
    delay_ms: float = 0.0,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    config = config or PipelineConfig()
    clip_list = list(clips)
    if len(clip_list) > config.max_parallel_clips:
        error = BusinessError(
            E_RESOURCE_LIMIT,
            f"batch clip count {len(clip_list)} exceeds max_parallel_clips {config.max_parallel_clips}",
            frame_index=0,
            details={
                "limit": "max_parallel_clips",
                "actual": len(clip_list),
                "max": config.max_parallel_clips,
            },
        )
        return {
            "schema_version": "1.0",
            "component": "motion-temporal",
            "status": "error",
            "business_code": error.code,
            "error": error.to_dict(),
            "clip_reports": [],
            "degradations": [],
            "injection_records": [],
        }
    reports = [
        run_clip(
            clip,
            cache_dir,
            config=config,
            injections=injections,
            stop_at=stop_at,
            delay_ms=delay_ms,
            snapshot_dir=snapshot_dir,
        )
        for clip in clip_list
    ]
    statuses = {report["status"] for report in reports}
    if "error" in statuses:
        status = "error"
        code = next((report["business_code"] for report in reports if report["status"] == "error"), E_INPUT_INVALID)
    elif "paused" in statuses:
        status = "paused"
        code = S_STOPPED
    else:
        status = "ok"
        code = E_OK
    combined = {
        "schema_version": "1.0",
        "component": "motion-temporal",
        "status": status,
        "business_code": code,
        "clip_reports": reports,
        "degradations": [
            {"clip_id": report["clip_id"], **item}
            for report in reports
            for item in report.get("degradations", [])
        ],
        "injection_records": [
            {"clip_id": report["clip_id"], **item}
            for report in reports
            for item in report.get("injection_records", [])
        ],
    }
    combined["report_checksum"] = sha256_bytes(canonical_bytes(combined))
    return combined

