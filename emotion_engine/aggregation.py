"""Shared construction of a complete, causal view over all arrived frames."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import EngineConfig
from .errors import E_STREAM_BATCH_MISMATCH, EngineError
from .features import validate_speakers
from .segments import build_segments, validate_segment_boundaries
from .tiering import classify_sequence, count_tier_switches, tier_counts
from .util import sha256_json
from .windows import WINDOW_SEMANTICS, attach_windows


def _base_rows(frames: Sequence[Mapping[str, Any]], speakers: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for frame, speaker in zip(frames, speakers):
        row = dict(frame)
        row["speaker_id"] = str(speaker)
        rows.append(row)
    return rows


def build_view(
    features: Sequence[Mapping[str, Any]],
    speakers: Sequence[str],
    config: EngineConfig,
    identity: Mapping[str, Any] | None = None,
    allowed_speakers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build one full view from an exact prefix; no accumulator approximations."""

    declared = (
        sorted({str(item) for item in speakers})
        if allowed_speakers is None
        else [str(item) for item in allowed_speakers]
    )
    validate_speakers(speakers, declared, len(features))
    rows = _base_rows(features, speakers)
    classified = classify_sequence(rows, config)
    for row, tier in zip(rows, classified["tiers"]):
        row["instant_tier"] = tier
    rows = attach_windows(rows, config)
    segments = build_segments(rows, config)
    return finalize_view(rows, segments, classified, config, identity)


def finalize_view(
    rows: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    classified: Mapping[str, Any],
    config: EngineConfig,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize metadata and checksums after the shared row/segment construction."""

    rows = [dict(row) for row in rows]
    segments = [dict(segment) for segment in segments]
    window_tiers = [str(row["tier"]) for row in rows]
    final_counts = tier_counts(window_tiers, config)
    instant_tiers = [str(row["instant_tier"]) for row in rows]
    instant_counts = tier_counts(instant_tiers, config)
    view: dict[str, Any] = {
        "identity": dict(identity or {}),
        "config": {
            "schema_version": config.schema_version,
            "config_sha256": config.content_hash(),
            "feature_version": config.feature_version,
            "tier_algorithm_version": config.tier_algorithm_version,
            "tier_schema_version": str(config.tier_schema["version"]),
            "tier_schema_sha256": config.tier_schema_hash(),
            "threshold_policy": config.threshold_policy_signature(),
            "window": config.window_signature(),
            "comparison": config.raw["comparison"],
        },
        "window_semantics": {
            **WINDOW_SEMANTICS,
            "length_frames": config.window_length,
            "tie_rule": config.tie_rule,
        },
        "frame_count": len(rows),
        "speaker_count": len({str(row["speaker_id"]) for row in rows}),
        "frames": rows,
        "segments": segments,
        "summary": {
            "tier_counts": final_counts,
            "instant_tier_counts": instant_counts,
            "tier": max(
                final_counts,
                key=lambda label: (
                    final_counts[label],
                    -list(config.tier_schema["tier_labels"]).index(label),
                ),
            )
            if rows
            else None,
            "tier_switch_count": count_tier_switches(window_tiers),
            "instant_tier_switch_count": int(
                classified.get("instant_tier_switch_count", count_tier_switches(instant_tiers))
            ),
            "threshold_stay_count": int(classified.get("threshold_stay_count", 0)),
            "speech_frame_count": sum(1 for row in rows if bool(row["is_speech"])),
            "segment_count": len(segments),
            "speaker_ids": sorted({str(row["speaker_id"]) for row in rows}),
        },
        "diagnostics": {
            "near_threshold_frames": classified.get("near_threshold_frames", []),
            "threshold_stays": classified.get("threshold_stays", []),
            "first_frame": rows[0]["frame_index"] if rows else None,
            "last_frame": rows[-1]["frame_index"] if rows else None,
            "comparison_precision": config.declared_precision,
        },
    }
    view["checksums"] = {
        "frames": sha256_json(rows),
        "segments": sha256_json(segments),
        "windows": sha256_json([row["window"] for row in rows]),
    }
    return view


def validate_view_structure(view: Mapping[str, Any], config: EngineConfig) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    frames = list(view["frames"])
    indices = [int(frame["frame_index"]) for frame in frames]
    expected_indices = list(range(len(frames)))
    checks.append(
        {
            "name": "frame_indices_contiguous",
            "ok": indices == expected_indices,
            "expected": f"0..{len(frames) - 1}" if frames else "empty",
            "actual": indices,
        }
    )
    try:
        validate_segment_boundaries(view["segments"], [str(frame["speaker_id"]) for frame in frames])
        boundary_ok = True
        boundary_error = None
    except Exception as exc:  # converted into a check, not swallowed from the API
        boundary_ok = False
        boundary_error = str(exc)
    checks.append(
        {
            "name": "segment_boundaries_match_speaker_switches",
            "ok": boundary_ok,
            "error": boundary_error,
        }
    )
    window_ok = all(
        int(frame["window"]["window_end_frame"]) == int(frame["frame_index"])
        and int(frame["window"]["window_size"]) <= config.window_length
        and bool(frame["window"]["insufficient_window"]) == (int(frame["window"]["window_size"]) < config.window_length)
        for frame in frames
    )
    checks.append({"name": "causal_window_bounds", "ok": window_ok})
    tier_window_alignment = all(
        str(frame["tier"]) == str(frame["window"]["tier"])
        for frame in frames
    )
    checks.append(
        {
            "name": "frame_tier_matches_window_tier",
            "ok": tier_window_alignment,
        }
    )
    switch_sequence = [str(frame["tier"]) for frame in frames]
    checks.append(
        {
            "name": "tier_switch_sequence",
            "ok": len(switch_sequence) == len(frames),
            "tier_switch_count": count_tier_switches(switch_sequence),
        }
    )
    return checks


def first_frame_deviation(
    stream_view: Mapping[str, Any],
    batch_view: Mapping[str, Any],
    config: EngineConfig,
) -> dict[str, Any] | None:
    stream_frames = list(stream_view["frames"])
    batch_frames = list(batch_view["frames"])
    limit = min(len(stream_frames), len(batch_frames))
    for index in range(limit):
        stream = stream_frames[index]
        batch = batch_frames[index]
        differences: dict[str, Any] = {}
        for field in ("frame_index", "speaker_id", "tier", "instant_tier", "is_speech"):
            if stream[field] != batch[field]:
                differences[field] = {"stream": stream[field], "batch": batch[field]}
        for field in ("valence", "arousal", "frame_weight"):
            if abs(float(stream[field]) - float(batch[field])) > config.comparison_abs_tol:
                differences[field] = {
                    "stream": stream[field],
                    "batch": batch[field],
                    "abs_delta": abs(float(stream[field]) - float(batch[field])),
                }
        for field in (
            "window_start_frame",
            "window_end_frame",
            "window_size",
            "insufficient_window",
            "padding_frames",
            "tier",
        ):
            if stream["window"][field] != batch["window"][field]:
                differences[f"window.{field}"] = {
                    "stream": stream["window"][field],
                    "batch": batch["window"][field],
                }
        if dict(stream["window"]["tier_counts"]) != dict(batch["window"]["tier_counts"]):
            differences["window.tier_counts"] = {
                "stream": stream["window"]["tier_counts"],
                "batch": batch["window"]["tier_counts"],
            }
        for field in ("valence_mean", "arousal_mean"):
            if abs(float(stream["window"][field]) - float(batch["window"][field])) > config.comparison_abs_tol:
                differences[f"window.{field}"] = {
                    "stream": stream["window"][field],
                    "batch": batch["window"][field],
                    "abs_delta": abs(
                        float(stream["window"][field]) - float(batch["window"][field])
                    ),
                }
        if differences:
            return {
                "frame_index": int(stream["frame_index"]),
                "stream_frame": stream,
                "batch_frame": batch,
                "differences": differences,
            }
    if len(stream_frames) != len(batch_frames):
        first = min(len(stream_frames), len(batch_frames))
        return {
            "frame_index": first,
            "differences": {
                "frame_count": {
                    "stream": len(stream_frames),
                    "batch": len(batch_frames),
                }
            },
        }
    if stream_view["segments"] != batch_view["segments"]:
        return {
            "frame_index": 0,
            "differences": {
                "segments": {
                    "stream": stream_view["segments"],
                    "batch": batch_view["segments"],
                }
            },
        }
    if stream_view.get("summary") != batch_view.get("summary"):
        return {
            "frame_index": max(0, limit - 1),
            "differences": {
                "summary": {
                    "stream": stream_view.get("summary"),
                    "batch": batch_view.get("summary"),
                }
            },
        }
    return None


def assert_stream_batch_match(
    stream_view: Mapping[str, Any],
    batch_view: Mapping[str, Any],
    config: EngineConfig,
) -> None:
    deviation = first_frame_deviation(stream_view, batch_view, config)
    if deviation is None:
        return
    raise EngineError(
        E_STREAM_BATCH_MISMATCH,
        "stream checkpoint differs from the offline prefix view",
        first_deviation=deviation,
        stream_stats=stream_view.get("summary"),
        batch_stats=batch_view.get("summary"),
    )
