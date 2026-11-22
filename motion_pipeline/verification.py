"""Cross-stage consistency, independent metric recomputation, and recovery checks."""

from __future__ import annotations

from typing import Any

from .config import FRAME_HEIGHT, FRAME_WIDTH
from .deterministic import canonical_bytes, checksum_without
from .errors import BusinessError
from .stages import recompute_clip_metrics, recompute_overall_metrics


def _fail(stage: str, clip_id: str | None, check: str, message: str, **detail: Any) -> None:
    raise BusinessError(
        "E_CROSS_STAGE_INVALID",
        message,
        stage=stage,
        clip_id=clip_id,
        field=check,
        detail={"check": check, **detail},
    )


def verify_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Validate all business invariants over committed stage payloads."""

    stages = checkpoint["stages"]
    if len(stages) != 4:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "cross-stage verification requires all four committed stages",
            stage="result_aggregation",
            detail={"committed_stages": len(stages)},
        )
    payloads = [record["payload"] for record in stages]
    stage_names = [record["stage"] for record in stages]
    checks: list[dict[str, Any]] = []
    clip_ids = payloads[0].get("clip_ids")
    if not isinstance(clip_ids, list) or not clip_ids:
        _fail("frame_sample", None, "clip_ids", "stage 1 has no clip ids")
    for index, payload in enumerate(payloads):
        if payload.get("clip_ids") != clip_ids:
            _fail(
                stage_names[index],
                None,
                "clip_ids",
                "stage clip id sets are not identical",
                expected=clip_ids,
                actual=payload.get("clip_ids"),
            )
        if set(payload.get("clips", {})) != set(clip_ids):
            _fail(
                stage_names[index],
                None,
                "clip_payloads",
                "stage clip payload map does not match its clip id set",
            )
    checks.append(
        {
            "name": "clip_id_sets_identical",
            "ok": True,
            "clip_count": len(clip_ids),
        }
    )

    sampled = payloads[0]
    encoded = payloads[1]
    extrapolated = payloads[2]
    aggregated = payloads[3]
    for clip_id in clip_ids:
        sample = sampled["clips"][clip_id]
        sample_body = {
            key: value for key, value in sample.items() if key != "sample_checksum"
        }
        if sample.get("sample_checksum") != checksum_without(
            {"sample_checksum": sample.get("sample_checksum"), **sample_body},
            "sample_checksum",
        ):
            _fail(
                "frame_sample",
                clip_id,
                "sample_checksum",
                "frame sample checksum does not match",
            )
        observed = set(sample["observed_indices"])
        forecast = set(sample["forecast_indices"])
        frame_indices = set(range(sample["frame_count"]))
        if observed & forecast:
            _fail(
                "frame_sample",
                clip_id,
                "observed_forecast_disjoint",
                "observed and extrapolated frame sets overlap",
                overlap=sorted(observed & forecast),
            )
        if observed | forecast != frame_indices:
            _fail(
                "frame_sample",
                clip_id,
                "frame_coverage",
                "observed and extrapolated frame sets do not cover every frame",
                missing=sorted(frame_indices - (observed | forecast)),
            )
        for pair in sample["pairs"]:
            if (
                pair["from_frame_index"] not in observed
                or pair["to_frame_index"] not in observed
            ):
                _fail(
                    "frame_sample",
                    clip_id,
                    "pair_observation",
                    "a sampled pair references a non-observed frame",
                )
        check_name = f"frame_coverage::{clip_id}"
        checks.append(
            {
                "name": check_name,
                "ok": True,
                "observed_count": len(observed),
                "forecast_count": len(forecast),
            }
        )

        encoded_clip = encoded["clips"][clip_id]
        encoded_body = {
            key: value
            for key, value in encoded_clip.items()
            if key != "feature_checksum"
        }
        if encoded_clip.get("feature_checksum") != checksum_without(
            {"feature_checksum": encoded_clip.get("feature_checksum"), **encoded_body},
            "feature_checksum",
        ):
            _fail(
                "motion_encoding",
                clip_id,
                "feature_checksum",
                "motion feature checksum does not match",
            )

        extrapolated_clip = extrapolated["clips"][clip_id]
        extrapolated_body = {
            key: value
            for key, value in extrapolated_clip.items()
            if key != "extrapolation_checksum"
        }
        if extrapolated_clip.get("extrapolation_checksum") != checksum_without(
            {
                "extrapolation_checksum": extrapolated_clip.get(
                    "extrapolation_checksum"
                ),
                **extrapolated_body,
            },
            "extrapolation_checksum",
        ):
            _fail(
                "temporal_extrapolation",
                clip_id,
                "extrapolation_checksum",
                "temporal extrapolation checksum does not match",
            )
        prediction_indices = [
            frame["frame_index"] for frame in extrapolated_clip["prediction_frames"]
        ]
        if prediction_indices != sample["forecast_indices"]:
            _fail(
                "temporal_extrapolation",
                clip_id,
                "prediction_frames",
                "extrapolated frame indices do not match the forecast set",
                expected=sample["forecast_indices"],
                actual=prediction_indices,
            )
        consumed_values: list[int] = []
        for frame in extrapolated_clip["prediction_frames"]:
            state = frame.get("state")
            if not isinstance(state, dict):
                _fail(
                    "temporal_extrapolation",
                    clip_id,
                    "history_state",
                    "predicted frame has no explicit history state",
                )
            if not isinstance(state.get("input_history_summary_hash"), str) or not isinstance(
                state.get("output_history_summary_hash"), str
            ):
                _fail(
                    "temporal_extrapolation",
                    clip_id,
                    "history_summary_hash",
                    "predicted frame state has no history summary hash",
                )
            if state.get("consumed_frame_index") != frame["frame_index"]:
                _fail(
                    "temporal_extrapolation",
                    clip_id,
                    "consumed_frame_index",
                    "predicted frame consumed frame index is inconsistent",
                )
            if state.get("previous_consumed_frame_index") != frame["frame_index"] - 1:
                _fail(
                    "temporal_extrapolation",
                    clip_id,
                    "previous_consumed_frame_index",
                    "recursive consumed frames are not contiguous",
                )
            consumed_values.append(state["consumed_frame_index"])
            for mask_entry in frame["mask"]:
                row, col, object_id = mask_entry
                if (
                    row < 0
                    or row >= FRAME_HEIGHT
                    or col < 0
                    or col >= FRAME_WIDTH
                    or object_id < 0
                ):
                    _fail(
                        "temporal_extrapolation",
                        clip_id,
                        "predicted_mask",
                        "predicted mask coordinate is outside the frame",
                    )
        if consumed_values and consumed_values[-1] != sample["forecast_indices"][-1]:
            _fail(
                "temporal_extrapolation",
                clip_id,
                "final_consumed_frame",
                "final consumed frame does not equal the final forecast frame",
            )
        checks.append(
            {
                "name": f"history_state::{clip_id}",
                "ok": True,
                "consumed_frames": len(consumed_values),
            }
        )

        aggregated_clip = aggregated["clips"][clip_id]
        if aggregated_clip["prediction_frames"] != extrapolated_clip["prediction_frames"]:
            _fail(
                "result_aggregation",
                clip_id,
                "prediction_sequence",
                "aggregated prediction sequence differs from stage 3",
            )
        details = aggregated_clip["trajectory_hits"]
        expected_detail_pairs = {
            (frame_index, object_id)
            for frame_index in sample["forecast_indices"]
            for object_id in range(len(encoded["clips"][clip_id]["objects"]))
        }
        actual_detail_pairs = {
            (detail["frame_index"], detail["object_id"]) for detail in details
        }
        if actual_detail_pairs != expected_detail_pairs or len(details) != len(
            expected_detail_pairs
        ):
            _fail(
                "result_aggregation",
                clip_id,
                "trajectory_hits",
                "hit details do not cover each forecast frame and object exactly once",
            )
        recomputed_clip = recompute_clip_metrics(details)
        if recomputed_clip != aggregated_clip["trajectory_stats"]:
            _fail(
                "result_aggregation",
                clip_id,
                "trajectory_stats",
                "per-clip trajectory metrics cannot be independently recomputed",
                expected=recomputed_clip,
                actual=aggregated_clip["trajectory_stats"],
            )
        aggregated_body = {
            key: value
            for key, value in aggregated_clip.items()
            if key != "aggregation_checksum"
        }
        if aggregated_clip.get("aggregation_checksum") != checksum_without(
            {
                "aggregation_checksum": aggregated_clip.get("aggregation_checksum"),
                **aggregated_body,
            },
            "aggregation_checksum",
        ):
            _fail(
                "result_aggregation",
                clip_id,
                "aggregation_checksum",
                "aggregation checksum does not match",
            )
        checks.append(
            {
                "name": f"metrics_recomputed::{clip_id}",
                "ok": True,
                "detail_count": len(details),
            }
        )
    recomputed_overall = recompute_overall_metrics(aggregated["clips"])
    if recomputed_overall != aggregated["overall_metrics"]:
        _fail(
            "result_aggregation",
            None,
            "overall_metrics",
            "overall metrics cannot be independently recomputed",
            expected=recomputed_overall,
            actual=aggregated["overall_metrics"],
        )
    checks.append(
        {
            "name": "overall_metrics_recomputed",
            "ok": True,
            "total_count": recomputed_overall["total_count"],
        }
    )
    return {
        "passed": True,
        "clip_ids": clip_ids,
        "checks": checks,
        "overall_metrics": recomputed_overall,
        "checkpoint_versions": checkpoint["version_history"],
    }
def verify_final_result(
    final_result: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    """Check final artifact indices/bytes against stage 4 and checkpoint."""

    verification = verify_checkpoint(checkpoint)
    aggregated = checkpoint["stages"][3]["payload"]
    expected_config = {
        "history_window_length": checkpoint["stages"][2]["payload"]["config"][
            "window_length"
        ],
        "decay": checkpoint["stages"][2]["payload"]["config"]["decay"],
        "observation_ratio": checkpoint["stages"][0]["payload"]["config"][
            "observation_ratio"
        ],
        "hit_distance_px": aggregated["config"]["hit_distance_px"],
    }
    if final_result.get("config") != expected_config:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result config differs from the committed stage configs",
            stage="result_aggregation",
            field="config",
        )
    if final_result.get("clip_ids") != checkpoint["task_clip_ids"]:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result clip ids do not match the checkpoint",
            stage="result_aggregation",
            field="clip_ids",
        )
    expected_versions = checkpoint["version_history"]
    if final_result.get("checkpoint_versions") != expected_versions:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result checkpoint version trajectory is stale",
            stage="result_aggregation",
            field="checkpoint_versions",
        )
    if final_result.get("per_clip") != {
        clip_id: {
            "prediction_frames": aggregated["clips"][clip_id]["prediction_frames"],
            "trajectory_stats": aggregated["clips"][clip_id]["trajectory_stats"],
        }
        for clip_id in checkpoint["task_clip_ids"]
    }:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result predictions or statistics differ from stage 4",
            stage="result_aggregation",
            field="per_clip",
        )
    if final_result.get("overall_metrics") != aggregated["overall_metrics"]:
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result overall metrics differ from stage 4",
            stage="result_aggregation",
            field="overall_metrics",
        )
    if final_result.get("result_checksum") != checksum_without(
        final_result, "result_checksum"
    ):
        raise BusinessError(
            "E_CROSS_STAGE_INVALID",
            "final result checksum does not match",
            stage="result_aggregation",
            field="result_checksum",
        )
    return verification


def compare_recovery(
    reference_checkpoint: dict[str, Any],
    recovered_checkpoint: dict[str, Any],
    *,
    reference_final_bytes: bytes | None = None,
    recovered_final_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Compare both runs and raise E_RECOVERY_MISMATCH at the first divergence."""

    reference_stages = reference_checkpoint.get("stages", [])
    recovered_stages = recovered_checkpoint.get("stages", [])
    for index in range(min(len(reference_stages), len(recovered_stages))):
        reference_stage = reference_stages[index]
        recovered_stage = recovered_stages[index]
        if canonical_bytes(reference_stage) != canonical_bytes(recovered_stage):
            raise BusinessError(
                "E_RECOVERY_MISMATCH",
                "recovery differs from the uninterrupted baseline",
                stage=reference_stage.get("stage", "unknown"),
                version=min(
                    reference_stage.get("version", index + 1),
                    recovered_stage.get("version", index + 1),
                ),
                detail={
                    "first_divergent_stage": reference_stage.get("stage", "unknown"),
                    "baseline_checksum": reference_stage.get("content_checksum"),
                    "recovered_checksum": recovered_stage.get("content_checksum"),
                },
            )
    if len(reference_stages) != len(recovered_stages):
        index = min(len(reference_stages), len(recovered_stages))
        raise BusinessError(
            "E_RECOVERY_MISMATCH",
            "recovery checkpoint stage count differs from the baseline",
            stage=(
                reference_stages[index].get("stage", "unknown")
                if index < len(reference_stages)
                else "final_result"
            ),
            detail={
                "baseline_stage_count": len(reference_stages),
                "recovered_stage_count": len(recovered_stages),
            },
        )
    if reference_checkpoint.get("version_history") != recovered_checkpoint.get(
        "version_history"
    ):
        raise BusinessError(
            "E_RECOVERY_MISMATCH",
            "recovery checkpoint version trajectory differs from the baseline",
            stage="checkpoint_versions",
            detail={
                "baseline": reference_checkpoint.get("version_history"),
                "recovered": recovered_checkpoint.get("version_history"),
            },
        )
    if reference_final_bytes is not None and recovered_final_bytes is not None:
        if reference_final_bytes != recovered_final_bytes:
            raise BusinessError(
                "E_RECOVERY_MISMATCH",
                "recovered final artifact is not byte-identical to the baseline",
                stage="result_aggregation",
                detail={
                    "baseline_bytes": len(reference_final_bytes),
                    "recovered_bytes": len(recovered_final_bytes),
                },
            )
    return {
        "equal": True,
        "stage_count": len(reference_stages),
        "checkpoint_versions": reference_checkpoint.get("version_history"),
        "final_bytes_equal": (
            reference_final_bytes == recovered_final_bytes
            if reference_final_bytes is not None and recovered_final_bytes is not None
            else None
        ),
    }
