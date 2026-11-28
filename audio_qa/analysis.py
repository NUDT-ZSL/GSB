"""End-to-end segment analysis."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .aggregation import (
    aggregate_clipping,
    aggregate_dc_offset,
    build_clipping_suppress_mask,
)
from .framing import iter_frames
from .grading import (
    CLIP_LIGHT_MAX,
    CLIP_MEDIUM_MAX,
    GRADE_ORDER,
    critical_conditions,
    critical_flags_for_intervals,
    grade_clipping,
    grade_dc_offset,
    grade_impulse,
    grade_noise,
    segment_overall_grade,
)
from .impulses import assign_impulses_to_frames, detect_impulses
from .metrics import classify_clipping_frames, frame_metrics
from .noise import detect_noise_bands, track_noise_floor
from .verification import verify_segment
from .wav import read_mono_wav
from .workflow import map_workflow


def load_manifest(path: str | Path) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if "recordings" not in manifest:
        raise ValueError(f"{path}: manifest has no 'recordings' list")
    return manifest


def _resolve_audio_path(manifest_path: Path, recording: dict) -> Path:
    raw = recording.get("path") or recording.get("wav") or recording.get("file")
    if not raw:
        raise ValueError(f"recording {recording.get('recording_id', '?')} has no path")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return manifest_path.parent / candidate


def _recording_segments(recording: dict, total_samples: int) -> list[dict]:
    segments = recording.get("segments")
    if segments:
        return segments
    return [
        {
            "segment_id": recording.get("recording_id", "recording") + "/seg_00",
            "start_sample": 0,
            "end_sample": total_samples,
            "truth_available": False,
            "ground_truth": None,
        }
    ]


def _grade_clipping_intervals(bands: list[dict]) -> None:
    for band in bands:
        band["grade"] = grade_clipping(float(band["segment_coverage"]))


def _grade_dc_intervals(bands: list[dict]) -> None:
    for band in bands:
        band["grade"] = grade_dc_offset(float(band["abs_dc_offset"]))


def _grade_noise_intervals(bands: list[dict]) -> None:
    for band in bands:
        band["grade"] = grade_noise(float(band["median_noise_floor_dbfs"]))


def _worst_grade(grades: list[str]) -> str | None:
    if not grades:
        return None
    return max(grades, key=lambda item: GRADE_ORDER[item])


def _recording_type_grades(segments: list[dict]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        present = [
            segment["segment_grades"][damage_type]
            for segment in segments
            if segment["segment_grades"].get(damage_type)
        ]
        result[damage_type] = _worst_grade(present)
    return result


def analyze_segment(
    recording_id: str,
    signal: np.ndarray,
    sample_rate: int,
    segment_spec: dict,
    frame_length: int,
    hop: int,
    noise_tolerance_db: float = 6.0,
    noise_mae_limit_db: float = 4.0,
    noise_within_fraction_min: float = 0.80,
    tolerances_ms: dict[str, float] | None = None,
) -> dict:
    segment_start = int(segment_spec["start_sample"])
    segment_end = int(segment_spec["end_sample"])
    segment_start = max(0, min(segment_start, signal.size))
    segment_end = max(segment_start, min(segment_end, signal.size))
    x = np.asarray(signal[segment_start:segment_end], dtype=np.float64)

    frames: list[dict] = []
    for frame in iter_frames(0, x.size, frame_length, hop):
        metrics = frame_metrics(
            x[frame.start : frame.end],
            sample_rate,
            frame.index,
            segment_start + frame.start,
            segment_start + frame.end,
            frame.complete,
        )
        frames.append(metrics)

    classify_clipping_frames(frames, sample_rate)
    suppress_mask = build_clipping_suppress_mask(frames, x, segment_start, sample_rate)
    impulse_events, ratio_hint, duration_hint = detect_impulses(
        x, sample_rate, suppress_mask=suppress_mask
    )
    assign_impulses_to_frames(
        frames, impulse_events, ratio_hint, duration_hint, offset=segment_start
    )
    track_noise_floor(frames, hop, sample_rate)

    clipping_bands = aggregate_clipping(
        frames, hop, sample_rate, segment_start, segment_end
    )
    _grade_clipping_intervals(clipping_bands)
    dc_bands = aggregate_dc_offset(
        frames, hop, sample_rate, segment_start, segment_end
    )
    _grade_dc_intervals(dc_bands)
    noise_bands, noise_tracker = detect_noise_bands(
        frames, hop, sample_rate, segment_start, segment_end
    )
    _grade_noise_intervals(noise_bands)

    impulse_output: list[dict] = []
    for event in impulse_events:
        start = segment_start + int(event["start_sample"])
        end = segment_start + int(event["end_sample"])
        impulse_output.append(
            {
                "type": "impulse",
                "start_sample": start,
                "end_sample": end,
                "duration_samples": int(end - start),
                "duration_seconds": (end - start) / float(sample_rate),
                "peak_amplitude": float(event["peak_amplitude"]),
                "jump_ratio": float(event["jump_ratio"]),
                "jump_amplitude": float(event["jump_amplitude"]),
                "local_floor_amplitude": float(event["local_floor_amplitude"]),
            }
        )
    impulse_output.sort(key=lambda item: item["start_sample"])

    segment_seconds = max(1.0 / sample_rate, (segment_end - segment_start) / float(sample_rate))
    clip_total_coverage = float(sum(float(item["segment_coverage"]) for item in clipping_bands))
    impulse_density = len(impulse_output) / segment_seconds
    impulse_peak = max((float(item["peak_amplitude"]) for item in impulse_output), default=0.0)
    impulse_grade, impulse_grade_detail = grade_impulse(impulse_density, impulse_peak)

    segment_grades: dict[str, str | None] = {
        "clipping": grade_clipping(clip_total_coverage) if clipping_bands else None,
        "dc_offset": _worst_grade([str(item["grade"]) for item in dc_bands]),
        "noise": _worst_grade([str(item["grade"]) for item in noise_bands]),
        "impulse": impulse_grade if impulse_output else None,
    }
    overall_grade, overall_rationale = segment_overall_grade(segment_grades)
    workflow = map_workflow(segment_grades, overall_grade)

    interval_map = {
        "clipping": clipping_bands,
        "dc_offset": dc_bands,
        "noise": noise_bands,
        "impulse": impulse_output,
    }
    manual_review = critical_flags_for_intervals(interval_map, segment_end - segment_start, sample_rate)
    manual_review.extend(
        critical_conditions(
            "segment_clip_coverage_total",
            clip_total_coverage,
            [(CLIP_LIGHT_MAX, 0.003), (CLIP_MEDIUM_MAX, 0.015)],
        )
    )

    result = {
        "recording_id": recording_id,
        "segment_id": str(segment_spec.get("segment_id", f"{recording_id}/seg_{segment_start}")),
        "start_sample": segment_start,
        "end_sample": segment_end,
        "duration_samples": segment_end - segment_start,
        "duration_seconds": (segment_end - segment_start) / float(sample_rate),
        "sample_rate": sample_rate,
        "frame_count": len(frames),
        "complete_frame_count": sum(1 for frame in frames if frame["complete"]),
        "incomplete_frame_count": sum(1 for frame in frames if not frame["complete"]),
        "damage_intervals": interval_map,
        "intervals_flat_count": sum(len(value) for value in interval_map.values()),
        "segment_clip_coverage_total": clip_total_coverage,
        "impulse_density_per_second": float(impulse_density),
        "impulse_peak_amplitude": float(impulse_peak),
        "impulse_grade_detail": impulse_grade_detail,
        "segment_grades": segment_grades,
        "overall_grade": overall_grade,
        "overall_grade_rationale": overall_rationale,
        "workflow": workflow,
        "noise_tracker": {
            **noise_tracker,
            "adaptive_window_seconds": 0.50,
            "history_percentile": 5.0,
            "gated_by": ["clipping", "impulse", "incomplete_frame"],
        },
        "manual_review": manual_review,
        "truth_available": bool(segment_spec.get("truth_available", False)),
        "frames": frames,
    }
    result["verification"] = verify_segment(
        segment_spec,
        result,
        frames,
        sample_rate,
        tolerances_ms=tolerances_ms,
        noise_tolerance_db=noise_tolerance_db,
        noise_mae_limit_db=noise_mae_limit_db,
        noise_within_fraction_min=noise_within_fraction_min,
    )
    return result


def analyze_manifest(
    manifest_path: str | Path,
    frame_length: int,
    hop: int,
    noise_tolerance_db: float = 6.0,
    noise_mae_limit_db: float = 4.0,
    noise_within_fraction_min: float = 0.80,
    tolerances_ms: dict[str, float] | None = None,
    parameter_requested: dict | None = None,
) -> dict:
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    sample_rate_global = int(manifest.get("sample_rate", 0))
    if frame_length <= 0 or hop <= 0:
        raise ValueError("frame_length and hop must be positive sample counts")
    if hop > frame_length:
        raise ValueError("hop must not exceed frame_length")
    if frame_length < 8:
        raise ValueError("frame_length must be at least 8 samples")

    recordings_out: list[dict] = []
    all_segments: list[dict] = []
    for recording in manifest["recordings"]:
        audio_path = _resolve_audio_path(manifest_path, recording)
        signal, sample_rate = read_mono_wav(audio_path)
        if sample_rate_global and sample_rate != sample_rate_global:
            raise ValueError(
                f"{audio_path}: sample rate {sample_rate} differs from manifest {sample_rate_global}"
            )
        segments = _recording_segments(recording, int(signal.size))
        analyzed_segments: list[dict] = []
        for segment_spec in segments:
            analyzed = analyze_segment(
                recording_id=str(recording.get("recording_id", audio_path.stem)),
                signal=signal,
                sample_rate=sample_rate,
                segment_spec=segment_spec,
                frame_length=frame_length,
                hop=hop,
                noise_tolerance_db=noise_tolerance_db,
                noise_mae_limit_db=noise_mae_limit_db,
                noise_within_fraction_min=noise_within_fraction_min,
                tolerances_ms=tolerances_ms,
            )
            analyzed_segments.append(analyzed)
            all_segments.append(analyzed)

        rec_type_grades = _recording_type_grades(analyzed_segments)
        rec_overall, rec_rationale = segment_overall_grade(rec_type_grades)
        rec_workflow = map_workflow(rec_type_grades, rec_overall)
        verified = [s for s in analyzed_segments if s["verification"]["status"] != "unverifiable"]
        recordings_out.append(
            {
                "recording_id": str(recording.get("recording_id", audio_path.stem)),
                "title": recording.get("title", ""),
                "path": str(audio_path),
                "sample_rate": sample_rate,
                "duration_samples": int(signal.size),
                "duration_seconds": signal.size / float(sample_rate),
                "segment_count": len(analyzed_segments),
                "segment_type_grades": rec_type_grades,
                "overall_grade": rec_overall,
                "overall_grade_rationale": rec_rationale,
                "workflow": rec_workflow,
                "verification_summary": {
                    "truth_available_segments": len(verified),
                    "verified_segments": sum(1 for s in verified if s["verification"]["status"] == "verified"),
                    "inconsistent_segments": sum(
                        1 for s in verified if s["verification"]["status"] == "inconsistent"
                    ),
                    "unverifiable_segments": sum(
                        1 for s in analyzed_segments if s["verification"]["status"] == "unverifiable"
                    ),
                },
                "segments": analyzed_segments,
            }
        )

    total_frames = sum(segment["frame_count"] for segment in all_segments)
    incomplete_frames = sum(segment["incomplete_frame_count"] for segment in all_segments)
    type_interval_counts = {
        damage_type: sum(len(segment["damage_intervals"][damage_type]) for segment in all_segments)
        for damage_type in ("clipping", "dc_offset", "noise", "impulse")
    }
    return {
        "parameters": {
            "frame_length_samples": int(frame_length),
            "hop_samples": int(hop),
            "sample_rate": sample_rate_global,
            "frame_length_seconds": frame_length / float(sample_rate_global) if sample_rate_global else None,
            "hop_seconds": hop / float(sample_rate_global) if sample_rate_global else None,
            "requested": parameter_requested or {},
        },
        "manifest": {
            "path": str(manifest_path),
            "seed": manifest.get("seed"),
            "generator": manifest.get("generator"),
            "truth_frame_length": manifest.get("truth_frame_length"),
            "truth_hop": manifest.get("truth_hop"),
        },
        "recordings": recordings_out,
        "summary": {
            "recording_count": len(recordings_out),
            "segment_count": len(all_segments),
            "frame_count": int(total_frames),
            "incomplete_frame_count": int(incomplete_frames),
            "interval_count_by_type": type_interval_counts,
            "truth_available_segments": sum(
                1 for segment in all_segments if segment["verification"]["status"] != "unverifiable"
            ),
            "verified_segments": sum(
                1 for segment in all_segments if segment["verification"]["status"] == "verified"
            ),
            "inconsistent_segments": sum(
                1 for segment in all_segments if segment["verification"]["status"] == "inconsistent"
            ),
            "unverifiable_segments": sum(
                1 for segment in all_segments if segment["verification"]["status"] == "unverifiable"
            ),
        },
    }
