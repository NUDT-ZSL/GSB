"""Cross-version normalization and curve/keyframe consistency accounting."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .catalog import CANONICAL_CHANNELS
from .io_utils import json_bytes
from .models import FrameCurves, VersionData
from .smoothing import emotion_resolutions


def curve_sha256(curves: FrameCurves) -> str:
    digest = hashlib.sha256()
    digest.update(
        json_bytes(
            {
                "fps": curves.fps,
                "frame_count": curves.frame_count,
                "channels": list(curves.channels),
            }
        )
    )
    digest.update(np.asarray(curves.values, dtype="<f8").tobytes())
    return digest.hexdigest()


def _normalization_differences(versions: list[VersionData]) -> list[dict[str, Any]]:
    baseline = versions[0]
    baseline_phonemes = {item.id: item for item in baseline.phonemes()}
    differences: list[dict[str, Any]] = []
    for version in versions[1:]:
        version_phonemes = {item.id: item for item in version.phonemes()}
        for phoneme_id in sorted(set(baseline_phonemes) | set(version_phonemes)):
            left = baseline_phonemes.get(phoneme_id)
            right = version_phonemes.get(phoneme_id)
            if left is None or right is None:
                differences.append(
                    {
                        "kind": "phoneme_presence",
                        "phoneme_id": phoneme_id,
                        "version": version.version,
                        "baseline_present": left is not None,
                        "version_present": right is not None,
                    }
                )
                continue
            fields = {
                "canonical": [left.canonical, right.canonical],
                "viseme": [left.viseme, right.viseme],
                "start_us": [left.start_us, right.start_us],
                "end_us": [left.end_us, right.end_us],
                "targets": [left.targets, right.targets],
            }
            for field, values in fields.items():
                if values[0] != values[1]:
                    differences.append(
                        {
                            "kind": "normalized_phoneme",
                            "phoneme_id": phoneme_id,
                            "version": version.version,
                            "field": field,
                            "baseline_value": values[0],
                            "version_value": values[1],
                        }
                    )
    return differences


def _curve_differences(
    baseline: FrameCurves, curves: FrameCurves, version: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if baseline.frame_count != curves.frame_count:
        rows.append(
            {
                "kind": "frame_count",
                "version": version,
                "baseline_value": baseline.frame_count,
                "version_value": curves.frame_count,
            }
        )
    channel_count = min(len(baseline.channels), len(curves.channels))
    for channel_index in range(channel_count):
        left = baseline.values[:, channel_index]
        right = curves.values[:, channel_index]
        frame_count = min(len(left), len(right))
        mismatch = np.flatnonzero(~np.isclose(left[:frame_count], right[:frame_count], rtol=0.0, atol=1e-12))
        for frame in mismatch.tolist():
            rows.append(
                {
                    "kind": "curve",
                    "version": version,
                    "channel": baseline.channels[channel_index],
                    "frame": int(frame),
                    "time_seconds": round(frame / baseline.fps, 9),
                    "baseline_value": float(left[frame]),
                    "version_value": float(right[frame]),
                    "delta": float(right[frame] - left[frame]),
                }
            )
    if len(curves.channels) != len(baseline.channels):
        rows.append(
            {
                "kind": "channel_order",
                "version": version,
                "baseline_value": list(baseline.channels),
                "version_value": list(curves.channels),
            }
        )
    return rows


def compare_versions(
    versions: list[VersionData],
    curve_by_version: dict[str, FrameCurves],
    *,
    fps: float,
) -> dict[str, Any]:
    baseline = versions[0]
    baseline_curves = curve_by_version[baseline.version]
    version_rows = []
    all_curve_differences: list[dict[str, Any]] = []
    max_abs_deviation = 0.0
    emotion_conflicts: list[dict[str, Any]] = []
    for version in versions:
        curves = curve_by_version[version.version]
        differences = (
            []
            if version.version == baseline.version
            else _curve_differences(baseline_curves, curves, version.version)
        )
        frame_count = min(baseline_curves.frame_count, curves.frame_count)
        if frame_count > 0:
            if version.version == baseline.version:
                max_abs_deviation = 0.0
            else:
                max_abs_deviation = max(
                    max_abs_deviation,
                    float(
                        np.max(
                            np.abs(
                                baseline_curves.values[:frame_count]
                                - curves.values[:frame_count]
                            )
                        )
                    ),
                )
        all_curve_differences.extend(differences)
        for resolved in emotion_resolutions(version):
            if resolved.conflict is not None:
                emotion_conflicts.append(
                    {
                        **resolved.conflict,
                        "version": version.version,
                    }
                )
        version_rows.append(
            {
                "version": version.version,
                "annotation": str(version.annotation_path),
                "bindings": str(version.bindings_path),
                "label_style": version.label_style,
                "time_base": version.time_base,
                "source_fps": version.source_fps,
                "audio_sample_rate": version.audio_sample_rate,
                "channel_sources": dict(version.channel_sources),
                "frame_count": curves.frame_count,
                "curve_sha256": curve_sha256(curves),
            }
        )
    normalization_differences = _normalization_differences(versions)
    identical = (
        not all_curve_differences
        and not normalization_differences
        and len({row["curve_sha256"] for row in version_rows}) == 1
    )
    return {
        "format": "museum-lipsync-cross-version-diff/v1",
        "fps": float(fps),
        "identical": identical,
        "baseline_version": baseline.version,
        "versions": version_rows,
        "max_abs_deviation": max_abs_deviation,
        "curve_differences": all_curve_differences,
        "normalization_differences": normalization_differences,
        "emotion_conflicts": emotion_conflicts,
        "reproduction": {
            "comparison": "float64 per-frame absolute values after target-fps resampling",
            "curve_hash": "sha256(json metadata + little-endian float64 matrix)",
            "channels": list(CANONICAL_CHANNELS),
        },
    }
