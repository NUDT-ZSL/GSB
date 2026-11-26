from __future__ import annotations

from typing import Any

import numpy as np

from .config import Config
from .errors import E_TOLERANCE_EXCEEDED, DiagnosticError
from .precision import PrecisionContext


EPS = 1e-12
REL_EPS = 1e-9
LEVEL_ORDER = ("feature", "node", "confidence", "classification")


def relative_deviation(absolute: float, reference: float) -> float:
    return float(absolute) / max(abs(float(reference)), REL_EPS)


def amplification_ratio(downstream_abs: float, upstream_abs: float) -> float:
    return float(downstream_abs) / max(float(upstream_abs), EPS)


def summarize_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "interval": [0.0, 0.0],
        }
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": len(values),
        "min": round(float(np.min(array)), 9),
        "max": round(float(np.max(array)), 9),
        "mean": round(float(np.mean(array)), 9),
        "p50": round(float(np.percentile(array, 50)), 9),
        "p95": round(float(np.percentile(array, 95)), 9),
        "interval": [round(float(np.min(array)), 9), round(float(np.max(array)), 9)],
    }


def normalize_frame_values(frame: dict[str, Any], context: PrecisionContext, cfg: Config) -> dict[str, float]:
    specs = {
        "energy_norm": (float(frame.get("energy_rms") or 0.0), cfg.energy_scale),
        "f0_norm": (float(frame.get("f0_candidate_hz") or 0.0), cfg.f0_scale_hz),
        "zcr_norm": (float(frame.get("zcr") or 0.0), cfg.zcr_scale),
        "pause_norm": (float(frame.get("pause_duration_ms") or 0), cfg.pause_scale_ms),
    }
    result: dict[str, float] = {}
    for name, (numerator, denominator) in specs.items():
        value = context.div(context.put(numerator), context.put(denominator))
        result[name] = context.to_float(value)
    return result


def build_frame_deviation(
    frame: dict[str, Any], cfg: Config, profile_name: str
) -> dict[str, Any]:
    reference_context = PrecisionContext("fp64")
    profile_context = PrecisionContext(profile_name)
    reference = normalize_frame_values(frame, reference_context, cfg)
    observed = normalize_frame_values(frame, profile_context, cfg)
    feature_errors: dict[str, dict[str, float]] = {}
    absolute_errors: list[float] = []
    relative_errors: list[float] = []
    for feature_name in sorted(reference):
        absolute = abs(observed[feature_name] - reference[feature_name])
        relative = relative_deviation(absolute, reference[feature_name])
        feature_errors[feature_name] = {
            "reference": round(reference[feature_name], 9),
            "observed": round(observed[feature_name], 9),
            "absolute_error": round(absolute, 12),
            "relative_error": round(relative, 9),
        }
        absolute_errors.append(absolute)
        relative_errors.append(relative)
    absolute_error = max(absolute_errors) if absolute_errors else 0.0
    relative_error = max(relative_errors) if relative_errors else 0.0
    return {
        "utterance_id": frame["utterance_id"],
        "frame_index": int(frame["frame_index"]),
        "precision_profile": profile_context.name,
        "level": "feature",
        "position": f"{frame['utterance_id']}:frame:{int(frame['frame_index']):04d}",
        "feature_errors": feature_errors,
        "absolute_error": round(absolute_error, 12),
        "relative_error": round(relative_error, 9),
    }


def _flatten_path_margins(result: dict[str, Any]) -> list[dict[str, Any]]:
    margins: list[dict[str, Any]] = []
    for rule_trace in result.get("decision_path", []):
        for condition in rule_trace.get("conditions", []):
            margins.append(
                {
                    "rule_id": rule_trace["rule_id"],
                    "feature": condition["feature"],
                    "margin": float(condition["margin"]),
                    "result": bool(condition["result"]),
                }
            )
    return margins


def build_candidate_deviation(
    judgment: dict[str, Any],
    reference_judgment: dict[str, Any],
    tree_version: str,
) -> dict[str, Any]:
    observed = judgment[tree_version]
    reference = reference_judgment[tree_version]
    if observed is None or reference is None:
        return {}

    feature_diffs = [
        abs(float(observed["feature_values"].get(key, 0.0)) - float(reference["feature_values"].get(key, 0.0)))
        for key in sorted(reference["feature_values"])
    ]
    feature_abs = max(feature_diffs) if feature_diffs else 0.0
    feature_ref = max(abs(float(v)) for v in reference["feature_values"].values()) if reference["feature_values"] else 0.0
    feature_rel = relative_deviation(feature_abs, feature_ref)

    observed_margins = _flatten_path_margins(observed)
    reference_margins = _flatten_path_margins(reference)
    node_diffs: list[float] = []
    node_rel_diffs: list[float] = []
    node_details: list[dict[str, Any]] = []
    for index, (obs_margin, ref_margin) in enumerate(zip(observed_margins, reference_margins)):
        absolute = abs(obs_margin["margin"] - ref_margin["margin"])
        relative = relative_deviation(absolute, ref_margin["margin"])
        node_diffs.append(absolute)
        node_rel_diffs.append(relative)
        if absolute > 0:
            node_details.append(
                {
                    "path_index": index,
                    "rule_id": obs_margin["rule_id"],
                    "feature": obs_margin["feature"],
                    "reference_margin": round(ref_margin["margin"], 9),
                    "observed_margin": round(obs_margin["margin"], 9),
                    "absolute_error": round(absolute, 12),
                    "relative_error": round(relative, 9),
                }
            )
    node_abs = max(node_diffs) if node_diffs else 0.0
    node_rel = max(node_rel_diffs) if node_rel_diffs else 0.0

    confidence_abs = abs(float(observed["confidence"]) - float(reference["confidence"]))
    confidence_rel = relative_deviation(confidence_abs, reference["confidence"])
    class_abs = abs(float(observed["class_margin"]) - float(reference["class_margin"]))
    class_rel = relative_deviation(class_abs, reference["class_margin"])
    label_changed = observed["label"] != reference["label"]
    if label_changed:
        # A discrete flip is not merely a small margin perturbation.
        class_abs = max(class_abs, 1.0)
        class_rel = max(class_rel, 1.0)

    return {
        "utterance_id": judgment["utterance_id"],
        "candidate_id": judgment["candidate_id"],
        "frame_index": judgment["frame_index"],
        "boundary_type": judgment["boundary_type"],
        "precision_profile": judgment["precision_profile"]["name"],
        "tree_version": tree_version,
        "position": f"{judgment['utterance_id']}:candidate:{judgment['frame_index']:04d}:{tree_version}",
        "label_changed": label_changed,
        "reference_label": reference["label"],
        "observed_label": observed["label"],
        "stages": {
            "feature": {
                "absolute_error": round(feature_abs, 12),
                "relative_error": round(feature_rel, 9),
            },
            "node": {
                "absolute_error": round(node_abs, 12),
                "relative_error": round(node_rel, 9),
            },
            "confidence": {
                "absolute_error": round(confidence_abs, 12),
                "relative_error": round(confidence_rel, 9),
            },
            "classification": {
                "absolute_error": round(class_abs, 12),
                "relative_error": round(class_rel, 9),
            },
        },
        "amplification": {
            "node_vs_feature": round(amplification_ratio(node_abs, feature_abs), 6),
            "confidence_vs_node": round(amplification_ratio(confidence_abs, node_abs), 6),
            "classification_vs_confidence": round(amplification_ratio(class_abs, confidence_abs), 6),
            "classification_vs_feature": round(amplification_ratio(class_abs, feature_abs), 6),
        },
        "node_error_details": node_details,
    }


def summarize_error_spectrum(
    frame_deviations: list[dict[str, Any]],
    candidate_deviations: list[dict[str, Any]],
) -> dict[str, Any]:
    abs_by_level: dict[str, list[float]] = {level: [] for level in LEVEL_ORDER}
    rel_by_level: dict[str, list[float]] = {level: [] for level in LEVEL_ORDER}
    for row in frame_deviations:
        abs_by_level["feature"].append(float(row["absolute_error"]))
        rel_by_level["feature"].append(float(row["relative_error"]))
    for row in candidate_deviations:
        for level in ("node", "confidence", "classification"):
            abs_by_level[level].append(float(row["stages"][level]["absolute_error"]))
            rel_by_level[level].append(float(row["stages"][level]["relative_error"]))

    levels: dict[str, Any] = {}
    for level in LEVEL_ORDER:
        levels[level] = {
            "absolute": summarize_values(abs_by_level[level]),
            "relative": summarize_values(rel_by_level[level]),
        }

    amplification_values = {
        "node_vs_feature": [],
        "confidence_vs_node": [],
        "classification_vs_confidence": [],
        "classification_vs_feature": [],
    }
    for row in candidate_deviations:
        for key in amplification_values:
            amplification_values[key].append(float(row["amplification"][key]))
    amplification = {key: summarize_values(values) for key, values in amplification_values.items()}

    dominant_level = "feature"
    dominant_score = -1.0
    for level in LEVEL_ORDER:
        score = float(levels[level]["absolute"]["p95"])
        if score > dominant_score + 1e-15:
            dominant_score = score
            dominant_level = level

    return {
        "levels": levels,
        "amplification": amplification,
        "dominant_error_source": dominant_level,
        "label_flip_count": sum(1 for row in candidate_deviations if row.get("label_changed")),
        "frame_detail_count": len(frame_deviations),
        "candidate_detail_count": len(candidate_deviations),
    }


def check_tolerances(
    frame_deviations: list[dict[str, Any]],
    candidate_deviations: list[dict[str, Any]],
    cfg: Config,
) -> None:
    ordered_frames = sorted(
        frame_deviations,
        key=lambda row: (row["utterance_id"], row["frame_index"], row["precision_profile"]),
    )
    for row in ordered_frames:
        tolerance = float(cfg.error_tolerances["feature"])
        if float(row["absolute_error"]) > tolerance:
            raise DiagnosticError(
                E_TOLERANCE_EXCEEDED,
                "feature normalization error exceeds tolerance",
                field="error_tolerances.feature",
                details={
                    "first_position": row["position"],
                    "level": "feature",
                    "absolute_error": row["absolute_error"],
                    "tolerance": tolerance,
                },
            )
    ordered_candidates = sorted(
        candidate_deviations,
        key=lambda row: (
            row["utterance_id"],
            row["frame_index"],
            row["tree_version"],
            row["precision_profile"],
        ),
    )
    for row in ordered_candidates:
        for level in ("node", "confidence", "classification"):
            value = float(row["stages"][level]["absolute_error"])
            tolerance = float(cfg.error_tolerances[level])
            if value > tolerance:
                raise DiagnosticError(
                    E_TOLERANCE_EXCEEDED,
                    f"{level} error exceeds tolerance",
                    field=f"error_tolerances.{level}",
                    details={
                        "first_position": row["position"],
                        "level": level,
                        "absolute_error": value,
                        "tolerance": tolerance,
                    },
                )


def truth_observation_details(
    records: list[dict[str, Any]], frame_truth: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    truth_index = {
        (row["utterance_id"], int(row["frame_index"])): row for row in frame_truth
    }
    details: list[dict[str, Any]] = []
    for record in records:
        for frame in record["frames"]:
            truth = truth_index.get(
                (record["utterance_id"], int(frame["frame_index"])), {}
            )
            if int(frame.get("pause_label", 0)) == 1 or not truth:
                continue
            row = {
                "utterance_id": record["utterance_id"],
                "frame_index": int(frame["frame_index"]),
                "position": f"{record['utterance_id']}:frame:{int(frame['frame_index']):04d}",
            }
            f0_true = float(truth.get("f0_true") or 0.0)
            f0_estimated = float(frame.get("f0_candidate_hz") or 0.0)
            if f0_true > 1.0:
                absolute = abs(f0_estimated - f0_true)
                row["f0"] = {
                    "true_hz": round(f0_true, 6),
                    "estimated_hz": round(f0_estimated, 6),
                    "absolute_error_hz": round(absolute, 9),
                    "relative_error": round(relative_deviation(absolute, f0_true), 9),
                }
            energy_true = float(truth.get("energy_true") or 0.0)
            energy_estimated = float(frame.get("energy_rms") or 0.0)
            if energy_true > 1e-6:
                absolute = abs(energy_estimated - energy_true)
                row["energy"] = {
                    "true_rms": round(energy_true, 9),
                    "estimated_rms": round(energy_estimated, 9),
                    "absolute_error_rms": round(absolute, 12),
                    "relative_error": round(relative_deviation(absolute, energy_true), 9),
                }
            if "f0" in row or "energy" in row:
                details.append(row)
    return details


def summarize_truth_observation_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    f0_errors: list[float] = []
    energy_errors: list[float] = []
    f0_relative: list[float] = []
    energy_relative: list[float] = []
    for row in details:
        if "f0" in row:
            f0_errors.append(float(row["f0"]["absolute_error_hz"]))
            f0_relative.append(float(row["f0"]["relative_error"]))
        if "energy" in row:
            energy_errors.append(float(row["energy"]["absolute_error_rms"]))
            energy_relative.append(float(row["energy"]["relative_error"]))
    return {
        "f0_hz_absolute": summarize_values(f0_errors),
        "f0_relative": summarize_values(f0_relative),
        "energy_rms_absolute": summarize_values(energy_errors),
        "energy_relative": summarize_values(energy_relative),
    }


def truth_observation_summary(
    records: list[dict[str, Any]], frame_truth: list[dict[str, Any]]
) -> dict[str, Any]:
    details = truth_observation_details(records, frame_truth)
    return summarize_truth_observation_details(details)
