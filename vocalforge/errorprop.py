"""Level-wise error propagation and dominant-source attribution."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import AppError
from .model import Dataset, FeatureFrame, FrameTruth, PrecisionConfig, ProfileResult
from .precision import quantize_scalar
from .util import (
    digest_payload,
    frame_intervals,
    quantile_summary,
    stable_round,
)

LEVELS = (
    "feature_scaling",
    "embedding_normalization",
    "log_transform",
    "similarity_accumulation",
)


def _truth_map(dataset: Dataset) -> dict[str, dict[int, FrameTruth]]:
    return {
        utterance.utterance_id: {row.frame_index: row for row in utterance.truth}
        for utterance in dataset.utterances
    }


def _safe_scale(value: float, floor: float = 1e-12) -> float:
    return max(abs(float(value)), floor)


def _round_detail(
    reference: float,
    observed: float,
    scale: float,
    rel_floor: float,
) -> tuple[float, float, float, float]:
    reference_n = float(reference) / max(scale, 1e-12)
    observed_n = float(observed) / max(scale, 1e-12)
    absolute = abs(observed_n - reference_n)
    relative = absolute / max(abs(reference_n), rel_floor)
    return (
        stable_round(reference_n, 12),
        stable_round(observed_n, 12),
        stable_round(absolute, 12),
        stable_round(relative, 12),
    )


def compute_error_spectrum(
    dataset: Dataset,
    canonical: ProfileResult,
    target: ProfileResult,
    config: PrecisionConfig,
) -> dict[str, Any]:
    truth = _truth_map(dataset)
    details: dict[str, list[dict[str, Any]]] = {level: [] for level in LEVELS}
    fixed_trace: list[dict[str, Any]] = []
    for utterance in sorted(dataset.utterances, key=lambda item: item.utterance_id):
        canonical_frames = canonical.frames_by_utterance[utterance.utterance_id]
        target_frames = target.frames_by_utterance[utterance.utterance_id]
        for canonical_frame, target_frame in zip(canonical_frames, target_frames):
            truth_frame = truth[utterance.utterance_id][canonical_frame.frame_index]
            f0_scale = _safe_scale(truth_frame.f0_true, 1.0)
            energy_scale = _safe_scale(truth_frame.energy_true, 1e-12)

            feature_ref = math.sqrt(
                (canonical_frame.f0_candidate / f0_scale) ** 2
                + (canonical_frame.energy / energy_scale) ** 2
            )
            feature_obs = math.sqrt(
                (target_frame.f0_candidate / f0_scale) ** 2
                + (target_frame.energy / energy_scale) ** 2
            )
            ref_n, obs_n, abs_dev, rel_dev = _round_detail(
                feature_ref, feature_obs, math.sqrt(2.0), 1e-12
            )
            details["feature_scaling"].append(
                _detail(utterance.utterance_id, target_frame.frame_index, ref_n, obs_n, abs_dev, rel_dev)
            )

            canonical_norm = math.sqrt(
                sum(float(v) * float(v) for v in canonical_frame.timbre_vector)
            )
            target_norm = math.sqrt(
                sum(float(v) * float(v) for v in target_frame.timbre_vector)
            )
            norm_scale = max(
                math.sqrt(truth_frame.energy_true) * 4.0
                + truth_frame.f0_true / 400.0,
                1e-12,
            )
            ref_n, obs_n, abs_dev, rel_dev = _round_detail(
                canonical_norm, target_norm, norm_scale, 1e-12
            )
            details["embedding_normalization"].append(
                _detail(utterance.utterance_id, target_frame.frame_index, ref_n, obs_n, abs_dev, rel_dev)
            )

            log_ref = math.log1p(math.sqrt(max(0.0, canonical_frame.energy))) + math.log1p(
                max(0.0, canonical_frame.f0_candidate) / 100.0
            )
            log_obs = math.log1p(math.sqrt(max(0.0, target_frame.energy))) + math.log1p(
                max(0.0, target_frame.f0_candidate) / 100.0
            )
            log_scale = math.log1p(math.sqrt(max(0.0, truth_frame.energy_true))) + math.log1p(
                max(0.0, truth_frame.f0_true) / 100.0
            )
            ref_n, obs_n, abs_dev, rel_dev = _round_detail(
                log_ref, log_obs, max(log_scale, 1e-12), 1e-12
            )
            details["log_transform"].append(
                _detail(utterance.utterance_id, target_frame.frame_index, ref_n, obs_n, abs_dev, rel_dev)
            )

            sim_ref = canonical.clone_scores[utterance.utterance_id]
            sim_obs = target.clone_scores[utterance.utterance_id]
            ref_n, obs_n, abs_dev, rel_dev = _round_detail(
                sim_ref, sim_obs, 1.0, 1e-12
            )
            details["similarity_accumulation"].append(
                _detail(utterance.utterance_id, target_frame.frame_index, ref_n, obs_n, abs_dev, rel_dev)
            )

            if target.profile == "fixed":
                expected_energy = quantize_scalar(
                    canonical_frame.energy, config.fixed_scale, config.rounding_mode
                )
                expected_f0 = quantize_scalar(
                    canonical_frame.f0_candidate, config.fixed_scale, config.rounding_mode
                )
                fixed_trace.append(
                    {
                        "utterance_id": utterance.utterance_id,
                        "frame_index": target_frame.frame_index,
                        "scale": config.fixed_scale,
                        "rounding_mode": config.rounding_mode,
                        "canonical_energy": stable_round(canonical_frame.energy, 12),
                        "profile_energy": stable_round(target_frame.energy, 12),
                        "expected_profile_energy": stable_round(expected_energy, 12),
                        "canonical_f0": stable_round(canonical_frame.f0_candidate, 9),
                        "profile_f0": stable_round(target_frame.f0_candidate, 9),
                        "expected_profile_f0": stable_round(expected_f0, 9),
                    }
                )

    level_payloads: dict[str, Any] = {}
    p95_values: list[float] = []
    previous_p95: float | None = None
    for level in LEVELS:
        rows = details[level]
        absolute = [row["abs_dev"] for row in rows]
        relative = [row["rel_dev"] for row in rows]
        abs_summary = quantile_summary(absolute)
        rel_summary = quantile_summary(relative)
        p95 = abs_summary["p95"]
        if previous_p95 is None:
            amplification = 1.0
        else:
            amplification = stable_round(
                p95 / max(previous_p95, 1e-12), 12
            )
        intervals_by_utterance: list[dict[str, Any]] = []
        for utterance_id in sorted({row["utterance_id"] for row in rows}):
            frames = [
                row["frame_index"]
                for row in rows
                if row["utterance_id"] == utterance_id
            ]
            for start, end in frame_intervals(frames):
                intervals_by_utterance.append(
                    {
                        "utterance_id": utterance_id,
                        "start_frame": start,
                        "end_frame": end,
                    }
                )
        level_payloads[level] = {
            "absolute_deviation": abs_summary,
            "relative_deviation": rel_summary,
            "amplification_ratio": amplification,
            "amplification_ratio_to_feature": stable_round(
                p95 / max(p95_values[0] if p95_values else p95, 1e-12), 12
            ),
            "intervals": intervals_by_utterance,
            "detail_count": len(rows),
            "details": rows,
        }
        p95_values.append(p95)
        previous_p95 = p95

    dominant_index = max(
        range(len(LEVELS)),
        key=lambda index: (p95_values[index], -index),
    )
    dominant = LEVELS[dominant_index]
    if p95_values[dominant_index] <= 1e-15:
        dominant = "none"
    payload = {
        "spectrum_version": "errorprop-v1",
        "reference_profile": "float64",
        "target_profile": target.profile,
        "observation_source": "truth_jsonl:f0_true,energy_true",
        "config": config.to_dict(),
        "levels": level_payloads,
        "dominant_source": dominant,
        "dominant_p95": p95_values[dominant_index],
        "level_order": list(LEVELS),
        "fixed_trace": fixed_trace,
    }
    payload["spectrum_digest"] = digest_payload(payload)
    return payload


def _detail(
    utterance_id: str,
    frame_index: int,
    reference: float,
    observed: float,
    abs_dev: float,
    rel_dev: float,
) -> dict[str, Any]:
    return {
        "utterance_id": utterance_id,
        "frame_index": frame_index,
        "reference": reference,
        "observed": observed,
        "abs_dev": abs_dev,
        "rel_dev": rel_dev,
    }


def _fail(first_difference: dict[str, Any], message: str) -> None:
    raise AppError(
        "E_ERRPROP_INCONSISTENT",
        message,
        stage="errorprop.validate",
        details={"first_difference": first_difference},
    )


def validate_error_spectrum(spectrum: dict[str, Any]) -> dict[str, Any]:
    levels = spectrum.get("levels", {})
    if tuple(spectrum.get("level_order", ())) != LEVELS:
        _fail(
            {"level": "level_order", "expected": list(LEVELS), "actual": spectrum.get("level_order")},
            "Error-propagation level order is invalid.",
        )
    p95_values: list[float] = []
    for level in LEVELS:
        payload = levels.get(level)
        if not isinstance(payload, dict):
            _fail({"level": level}, "Missing error-propagation level.")
        details = payload.get("details", [])
        if not isinstance(details, list) or not details:
            _fail({"level": level, "position": 0}, "Level has no recomputable detail rows.")
        abs_values: list[float] = []
        rel_values: list[float] = []
        for position, row in enumerate(details):
            expected_abs = abs(float(row["observed"]) - float(row["reference"]))
            expected_rel = expected_abs / max(abs(float(row["reference"])), 1e-12)
            if (
                abs(expected_abs - float(row["abs_dev"])) > 1e-10
                or abs(expected_rel - float(row["rel_dev"])) > 1e-9
            ):
                _fail(
                    {
                        "level": level,
                        "position": position,
                        "utterance_id": row.get("utterance_id"),
                        "frame_index": row.get("frame_index"),
                        "expected_abs": expected_abs,
                        "stored_abs": row.get("abs_dev"),
                    },
                    "A level deviation cannot be recomputed from its detail.",
                )
            abs_values.append(float(row["abs_dev"]))
            rel_values.append(float(row["rel_dev"]))
        expected_abs_summary = quantile_summary(abs_values)
        expected_rel_summary = quantile_summary(rel_values)
        for metric, expected in (
            ("absolute_deviation", expected_abs_summary),
            ("relative_deviation", expected_rel_summary),
        ):
            stored = payload.get(metric, {})
            for key, expected_value in expected.items():
                if abs(float(stored.get(key, 0.0)) - expected_value) > 1e-9:
                    _fail(
                        {
                            "level": level,
                            "metric": metric,
                            "quantile": key,
                            "expected": expected_value,
                            "stored": stored.get(key),
                        },
                        "A level quantile cannot be recomputed from its detail.",
                    )
        intervals = payload.get("intervals", [])
        previous: tuple[str, int, int] | None = None
        for position, interval in enumerate(intervals):
            current = (
                str(interval["utterance_id"]),
                int(interval["start_frame"]),
                int(interval["end_frame"]),
            )
            if current[1] > current[2]:
                _fail(
                    {"level": level, "position": position, **interval},
                    "Error interval is reversed.",
                )
            if previous is not None and current < previous:
                _fail(
                    {
                        "level": level,
                        "position": position,
                        "previous": previous,
                        "current": current,
                    },
                    "Error intervals are not ordered.",
                )
            previous = current
        p95 = expected_abs_summary["p95"]
        if p95_values:
            expected_ratio = p95 / max(p95_values[-1], 1e-12)
        else:
            expected_ratio = 1.0
        if abs(float(payload.get("amplification_ratio", 0.0)) - expected_ratio) > 1e-9:
            _fail(
                {
                    "level": level,
                    "expected": expected_ratio,
                    "stored": payload.get("amplification_ratio"),
                },
                "Amplification ratio cannot be recomputed.",
            )
        p95_values.append(p95)
    if spectrum.get("target_profile") == "fixed":
        trace = spectrum.get("fixed_trace", [])
        if not trace:
            _fail(
                {"field": "fixed_trace", "position": 0},
                "Fixed profile has no recomputable trace.",
            )
        for position, row in enumerate(trace):
            expected_energy = quantize_scalar(
                float(row["canonical_energy"]),
                int(row["scale"]),
                str(row["rounding_mode"]),
            )
            expected_f0 = quantize_scalar(
                float(row["canonical_f0"]),
                int(row["scale"]),
                str(row["rounding_mode"]),
            )
            if (
                abs(expected_energy - float(row["profile_energy"])) > 1e-10
                or abs(expected_f0 - float(row["profile_f0"])) > 1e-8
            ):
                _fail(
                    {
                        "field": "fixed_trace",
                        "position": position,
                        "utterance_id": row.get("utterance_id"),
                        "frame_index": row.get("frame_index"),
                        "expected_energy": expected_energy,
                        "profile_energy": row.get("profile_energy"),
                    },
                    "Fixed-point deviation cannot be recomputed from scale and rounding.",
                )
    stored_digest = spectrum.get("spectrum_digest")
    check = dict(spectrum)
    check.pop("spectrum_digest", None)
    if stored_digest != digest_payload(check):
        _fail(
            {"field": "spectrum_digest"},
            "Error spectrum checksum does not match its content.",
        )
    return {
        "ok": True,
        "levels_checked": list(LEVELS),
        "fixed_trace_checked": len(spectrum.get("fixed_trace", [])),
        "dominant_source": spectrum.get("dominant_source"),
    }
