from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import section_revision, validate_rule_labels
from .errors import fail
from .synth import load_truth, truth_frame_kinds
from .util import seal_record, sha256_json
from .features import CallMeta


def _metric_value(metric: str, metrics: dict[str, Any]) -> Any:
    if metric not in metrics:
        fail("E_CONFIG_INVALID", detail=f"unknown attribution metric: {metric}")
    return metrics[metric]


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "gte":
        return float(left) >= float(right)
    if op == "lte":
        return float(left) <= float(right)
    if op == "gt":
        return float(left) > float(right)
    if op == "lt":
        return float(left) < float(right)
    if op == "eq":
        return left == right
    fail("E_CONFIG_INVALID", detail=f"unknown rule operator: {op}")
    return False


def evaluate_condition(condition: dict[str, Any], metrics: dict[str, Any]) -> bool:
    if condition.get("always") is True:
        return True
    if "all" in condition:
        return all(
            evaluate_condition(item, metrics) for item in condition["all"]
        )
    if "any" in condition:
        return any(
            evaluate_condition(item, metrics) for item in condition["any"]
        )
    return _compare(
        _metric_value(str(condition.get("metric")), metrics),
        str(condition.get("op")),
        condition.get("value"),
    )


def classify_metrics(
    metrics: dict[str, Any],
    attribution_config: dict[str, Any],
    controlled: set[str],
    call_id: str,
) -> str:
    rules = attribution_config.get("rules", [])
    validate_rule_labels(rules, controlled, call_id)
    for rule in rules:
        if evaluate_condition(rule.get("when", {}), metrics):
            label = rule.get("label")
            if label not in controlled:
                fail("E_LABEL_INVALID", call_id=call_id, label=label)
            return str(label)
    label = attribution_config.get("default_label", "stationary_floor")
    if label not in controlled:
        fail("E_LABEL_INVALID", call_id=call_id, label=label)
    return str(label)


def _segment_metrics(
    energies: list[float],
    floors: list[float],
    zcrs: list[float],
    start_frame: int,
    end_frame: int,
    noise_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    energy_array = np.asarray(energies, dtype=np.float64)
    floor_array = np.asarray(floors, dtype=np.float64)
    mean_energy = float(np.mean(energy_array))
    mean_floor = float(np.mean(floor_array))
    ratio = mean_energy / max(mean_floor, 1e-18)
    deviation_db = 10.0 * math.log10(max(ratio, 1e-18))
    mean_square = float(np.mean(energy_array * energy_array))
    variance = max(mean_square - mean_energy * mean_energy, 0.0)
    volatility = math.sqrt(variance) / mean_energy if mean_energy > 0 else 0.0
    floor_span_db = 10.0 * math.log10(max(float(np.max(floor_array)), 1e-18)) - (
        10.0 * math.log10(max(float(np.min(floor_array)), 1e-18))
    )
    overlaps = any(
        start_frame <= int(step["boundary_frame"]) < end_frame
        for step in noise_steps
    )
    return {
        "mean_energy": round(mean_energy, 12),
        "mean_noise_floor": round(mean_floor, 12),
        "deviation_db": round(deviation_db, 12),
        "abs_deviation_db": round(abs(deviation_db), 12),
        "volatility": round(volatility, 12),
        "zcr_mean": round(float(np.mean(np.asarray(zcrs, dtype=np.float64))), 12),
        "floor_span_db": round(floor_span_db, 12),
        "overlaps_noise_step": bool(overlaps),
    }


def recompute_metrics_from_evidence(
    evidence: dict[str, Any],
    start_frame: int,
    end_frame: int,
    noise_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        energies = [float(value) for value in evidence["energies"]]
        floors = [float(value) for value in evidence["noise_floors"]]
        zcr_mean = float(evidence["zcr_mean"])
    except (KeyError, TypeError, ValueError):
        fail(
            "E_LINEAGE_BROKEN",
            detail="segment evidence is incomplete",
        )
    expected_count = end_frame - start_frame
    if len(energies) != expected_count or len(floors) != expected_count:
        fail(
            "E_LINEAGE_BROKEN",
            detail="segment evidence length does not match event range",
        )
    return _segment_metrics(
        energies,
        floors,
        [zcr_mean] * expected_count,
        start_frame,
        end_frame,
        noise_steps,
    )


def compute_attribution_record(
    call: CallMeta,
    feature_record: dict[str, Any],
    noise_record: dict[str, Any],
    truth: dict[str, Any],
    config: dict[str, Any],
    version: int,
    snapshot_revision: str,
    forced_label: str | None = None,
) -> dict[str, Any]:
    feature_count = int(feature_record["frame_count"])
    truth_count = int(truth.get("frame_count", -1))
    if truth_count != feature_count:
        fail(
            "E_SEGMENT_SPLIT",
            call_id=call.call_id,
            feature_frames=feature_count,
            truth_frames=truth_count,
        )
    kinds = truth_frame_kinds(truth, feature_count)
    if any(kind == "unknown" for kind in kinds):
        first_unknown = kinds.index("unknown")
        fail(
            "E_SEGMENT_SPLIT",
            call_id=call.call_id,
            frame=first_unknown,
            detail="truth labels do not cover every frame",
        )

    attribution_config = config["attribution"]
    controlled_list = attribution_config.get("controlled_labels", [])
    controlled = set(controlled_list)
    if forced_label is not None and forced_label not in controlled:
        fail("E_LABEL_INVALID", call_id=call.call_id, label=forced_label)

    frames = feature_record["frames"]
    floors = noise_record["noise_floor_curve"]
    noise_steps = noise_record.get("steps", [])
    silence_segments: list[dict[str, Any]] = []
    speech_ranges: list[dict[str, int]] = []
    segment_index = 0
    frame_cursor = 0
    while frame_cursor < feature_count:
        kind = kinds[frame_cursor]
        end = frame_cursor + 1
        while end < feature_count and kinds[end] == kind:
            end += 1
        if kind == "speech":
            speech_ranges.append(
                {
                    "start_frame": frame_cursor,
                    "end_frame": end,
                }
            )
        elif kind == "silence":
            for frame_index in range(frame_cursor, end):
                if bool(frames[frame_index].get("is_speech")):
                    fail(
                        "E_SEGMENT_SPLIT",
                        call_id=call.call_id,
                        frame=frame_index,
                        detail="truth silence overlaps observed speech activity",
                    )
            energies = [float(frames[i]["energy"]) for i in range(frame_cursor, end)]
            frame_floors = [float(floors[i]) for i in range(frame_cursor, end)]
            zcrs = [
                float(frames[i]["zero_crossing_rate"]) for i in range(frame_cursor, end)
            ]
            metrics = _segment_metrics(
                energies,
                frame_floors,
                zcrs,
                frame_cursor,
                end,
                noise_steps,
            )
            label = forced_label or classify_metrics(
                metrics, attribution_config, controlled, call.call_id
            )
            if label not in controlled:
                fail("E_LABEL_INVALID", call_id=call.call_id, label=label)
            segment_id = f"{call.call_id}-S{segment_index + 1:03d}"
            segment_index += 1
            silence_segments.append(
                {
                    "segment_id": segment_id,
                    "call_id": call.call_id,
                    "start_frame": frame_cursor,
                    "end_frame": end,
                    "start_ms": frame_cursor * call.frame_ms,
                    "end_ms": end * call.frame_ms,
                    "frame_count": end - frame_cursor,
                    "label": label,
                    "metrics": metrics,
                    "evidence": {
                        "energies": [round(value, 12) for value in energies],
                        "noise_floors": [round(value, 12) for value in frame_floors],
                        "zcr_mean": metrics["zcr_mean"],
                    },
                }
            )
        else:
            fail(
                "E_SEGMENT_SPLIT",
                call_id=call.call_id,
                frame=frame_cursor,
                kind=kind,
            )
        frame_cursor = end

    attribution_revision = section_revision(config, "attribution")
    truth_hash = sha256_json(truth)
    lineage = {
        "feature_version": int(feature_record["feature_version"]),
        "feature_checksum": feature_record["record_checksum"],
        "noise_floor_version": int(noise_record["noise_floor_version"]),
        "noise_floor_checksum": noise_record["record_checksum"],
        "attribution_threshold_revision": attribution_revision,
        "truth_revision": truth.get("revision"),
        "truth_fingerprint": truth_hash,
        "snapshot_revision": snapshot_revision,
        "snapshot_version": int(version),
    }
    for segment in silence_segments:
        segment["lineage"] = dict(lineage)

    record: dict[str, Any] = {
        "call_id": call.call_id,
        "attribution_version": int(version),
        "attribution_config_revision": attribution_revision,
        "source_feature_version": int(feature_record["feature_version"]),
        "source_feature_checksum": feature_record["record_checksum"],
        "source_noise_version": int(noise_record["noise_floor_version"]),
        "source_noise_checksum": noise_record["record_checksum"],
        "truth_revision": truth.get("revision"),
        "truth_fingerprint": truth_hash,
        "truth_frame_kinds": kinds,
        "speech_ranges": speech_ranges,
        "noise_steps": noise_steps,
        "silence_segments": silence_segments,
        "lineage": lineage,
    }
    record["dependencies"] = {
        "source_noise_checksum": noise_record["record_checksum"],
        "truth_fingerprint": truth_hash,
        "truth_revision": truth.get("revision"),
        "attribution_config_revision": attribution_revision,
        "frame_count": feature_count,
    }
    record["segments_checksum"] = sha256_json(silence_segments)
    return seal_record(record)


def load_truth_for_call(data_dir: Any, call_id: str) -> dict[str, Any]:
    return load_truth(data_dir, call_id)


def validate_attribution_record(record: dict[str, Any]) -> None:
    segments = record.get("silence_segments")
    if not isinstance(segments, list):
        fail("E_INCONSISTENT", stage="attribution", call_id=record.get("call_id"))
    previous_end = -1
    for segment in segments:
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if start < previous_end or start >= end:
            fail(
                "E_INCONSISTENT",
                stage="attribution",
                call_id=record.get("call_id"),
                segment_id=segment.get("segment_id"),
            )
        previous_end = end
    if record.get("segments_checksum") != sha256_json(segments):
        fail("E_INCONSISTENT", stage="attribution", call_id=record.get("call_id"))

