"""Truth-based observation metrics kept outside the conversion pipeline."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .errors import fail

if TYPE_CHECKING:
    from .pipeline import ConversionEngine


def evaluate_snapshots(
    engine: "ConversionEngine", utterance_ids: list[str] | None = None
) -> dict[str, Any]:
    params_artifacts, _ = engine._snapshot_stage("params")
    alignment_artifacts, _ = engine._snapshot_stage("alignment")
    ids = sorted(
        utterance_ids
        if utterance_ids is not None
        else params_artifacts.keys()
    )
    missing = sorted(set(ids) - set(params_artifacts))
    if missing:
        fail(
            "E_UTTERANCE_NOT_FOUND",
            "评估请求包含未知 utterance_id",
            field="utterance_id",
            details={"missing": missing},
        )
    per_utterance: dict[str, Any] = {}
    total_frames = 0
    total_f0_error = 0.0
    total_energy_error = 0.0
    total_alignment_correct = 0
    for utterance_id in ids:
        truth = engine.dataset.read_truth(utterance_id)
        params = params_artifacts[utterance_id]
        alignment = alignment_artifacts[utterance_id]
        param_frames = params["frames"]
        alignment_frames = alignment["frames"]
        if not (len(truth) == len(param_frames) == len(alignment_frames)):
            fail(
                "E_FRAME_MISMATCH",
                "评估时帧数不一致",
                field="frame_count",
                details={
                    "utterance_id": utterance_id,
                    "truth_frame_count": len(truth),
                    "param_frame_count": len(param_frames),
                    "alignment_frame_count": len(alignment_frames),
                },
            )
        f0_errors = [
            abs(float(frame["mapped_f0_hz"]) - float(truth_frame["f0_true"]))
            for frame, truth_frame in zip(param_frames, truth)
        ]
        energy_errors = [
            abs(
                float(frame["mapped_energy_rms"])
                - float(truth_frame["energy_true"])
            )
            for frame, truth_frame in zip(param_frames, truth)
        ]
        alignment_correct = sum(
            1
            for frame, truth_frame in zip(alignment_frames, truth)
            if int(frame["phoneme_id"]) == int(truth_frame["phoneme_id"])
        )
        count = len(truth)
        total_frames += count
        total_f0_error += sum(f0_errors)
        total_energy_error += sum(energy_errors)
        total_alignment_correct += alignment_correct
        per_utterance[utterance_id] = {
            "frame_count": count,
            "f0_mae_hz": round(sum(f0_errors) / count, 6),
            "energy_mae": round(sum(energy_errors) / count, 8),
            "alignment_accuracy": round(alignment_correct / count, 6),
        }
    if total_frames == 0:
        overall = {
            "frame_count": 0,
            "f0_mae_hz": 0.0,
            "energy_mae": 0.0,
            "alignment_accuracy": 0.0,
        }
    else:
        overall = {
            "frame_count": total_frames,
            "f0_mae_hz": round(total_f0_error / total_frames, 6),
            "energy_mae": round(total_energy_error / total_frames, 8),
            "alignment_accuracy": round(
                total_alignment_correct / total_frames, 6
            ),
        }
    return {
        "ok": True,
        "frame_count_consistent": True,
        "utterances": per_utterance,
        "overall": overall,
    }

