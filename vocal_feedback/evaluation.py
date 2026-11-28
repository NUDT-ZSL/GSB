from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config


def _load_annotation_payload(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: annotation file must contain an object")
    return payload


def _frame_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    frames = payload.get("frames", payload.get("frame_annotations", []))
    if isinstance(frames, dict):
        times = frames.get("timestamp_s", frames.get("times", []))
        f0_values = frames.get("f0_hz", frames.get("frequencies", []))
        midi_values = frames.get("midi", [])
        voiced_values = frames.get("voiced", [])
        result = []
        for index, time_s in enumerate(times):
            midi = midi_values[index] if index < len(midi_values) else None
            f0 = f0_values[index] if index < len(f0_values) else None
            voiced = (
                bool(voiced_values[index])
                if index < len(voiced_values)
                else f0 is not None
            )
            result.append(
                {
                    "timestamp_s": float(time_s),
                    "f0_hz": f0,
                    "midi": midi,
                    "voiced": voiced,
                }
            )
        return result
    return [dict(entry) for entry in frames]


def _nearest_frame_index(
    sorted_times: np.ndarray, query_time: float, max_distance: float
) -> int | None:
    if len(sorted_times) == 0:
        return None
    position = int(np.searchsorted(sorted_times, query_time))
    candidates = [position - 1, position]
    valid = [index for index in candidates if 0 <= index < len(sorted_times)]
    if not valid:
        return None
    best = min(valid, key=lambda index: abs(float(sorted_times[index]) - query_time))
    if abs(float(sorted_times[best]) - query_time) > max_distance:
        return None
    return best


def _frame_metrics(
    frames: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    config: Config,
    tolerance_cents: float,
) -> dict[str, Any]:
    if not frames or not annotations:
        return {
            "reference_voiced_count": 0,
            "predicted_voiced_count": 0,
            "voiced_precision": 0.0,
            "voiced_recall": 0.0,
            "voiced_f1": 0.0,
            "pitch_hit_rate_reference_voiced": 0.0,
            "pitch_mae_cents": None,
            "evaluated_frame_count": 0,
        }
    predicted_times = np.asarray([float(frame["timestamp_s"]) for frame in frames])
    order = np.argsort(predicted_times)
    sorted_times = predicted_times[order]
    max_distance = max(config.hop_length_s * 0.55, 0.006)
    true_positive = 0
    false_positive = 0
    false_negative = 0
    reference_voiced_count = 0
    predicted_voiced_count = sum(bool(frame["voiced"]) for frame in frames)
    pitch_hits = 0
    cents_errors: list[float] = []
    compared = 0
    for truth in annotations:
        truth_time = float(truth["timestamp_s"])
        truth_voiced = bool(truth.get("voiced", truth.get("f0_hz") is not None))
        prediction_index = _nearest_frame_index(sorted_times, truth_time, max_distance)
        if prediction_index is None:
            if truth_voiced:
                reference_voiced_count += 1
                false_negative += 1
            continue
        prediction = frames[int(order[prediction_index])]
        predicted_voiced = bool(prediction["voiced"])
        if truth_voiced:
            reference_voiced_count += 1
        if truth_voiced and predicted_voiced:
            true_positive += 1
        elif truth_voiced:
            false_negative += 1
        elif predicted_voiced:
            false_positive += 1
        truth_midi = truth.get("midi")
        if truth_midi is None and truth.get("f0_hz") is not None:
            truth_midi = 69.0 + 12.0 * math.log2(float(truth["f0_hz"]) / 440.0)
        if truth_voiced and predicted_voiced and truth_midi is not None and prediction["midi"] is not None:
            error = (float(prediction["midi"]) - float(truth_midi)) * 100.0
            cents_errors.append(error)
            compared += 1
            if abs(error) <= tolerance_cents:
                pitch_hits += 1
    precision = (
        true_positive / predicted_voiced_count if predicted_voiced_count else 0.0
    )
    recall = true_positive / reference_voiced_count if reference_voiced_count else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reference_voiced_count": reference_voiced_count,
        "predicted_voiced_count": predicted_voiced_count,
        "voiced_true_positive": true_positive,
        "voiced_false_positive": false_positive,
        "voiced_false_negative": false_negative,
        "voiced_precision": round(precision, 6),
        "voiced_recall": round(recall, 6),
        "voiced_f1": round(f1, 6),
        "pitch_hit_count": pitch_hits,
        "pitch_hit_rate_reference_voiced": round(pitch_hits / reference_voiced_count, 6)
        if reference_voiced_count
        else 0.0,
        "pitch_hit_rate_on_compared_frames": round(pitch_hits / compared, 6)
        if compared
        else 0.0,
        "pitch_mae_cents": round(float(np.mean(np.abs(cents_errors))), 3)
        if cents_errors
        else None,
        "evaluated_frame_count": compared,
    }


def _overlap(left_start: float, left_end: float, right_start: float, right_end: float) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def _note_metrics(
    notes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    tolerance_s: float,
) -> dict[str, Any]:
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    for truth in annotations:
        truth_start = float(truth["start_s"])
        truth_end = float(truth["end_s"])
        duration = max(truth_end - truth_start, 1e-9)
        choices = []
        for index, note in enumerate(notes):
            if index in used:
                continue
            overlap = _overlap(truth_start, truth_end, float(note["start_s"]), float(note["end_s"]))
            if overlap / duration > 0.2:
                choices.append((overlap / duration, index))
        if choices:
            _, index = max(choices, key=lambda item: item[0])
            used.add(index)
            matched.append((truth, notes[index]))
    start_errors = [
        abs(float(note["start_s"]) - float(truth["start_s"])) for truth, note in matched
    ]
    end_errors = [
        abs(float(note["end_s"]) - float(truth["end_s"])) for truth, note in matched
    ]
    boundary_errors = start_errors + end_errors
    precision = len(matched) / len(notes) if notes else 0.0
    recall = len(matched) / len(annotations) if annotations else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    boundary_hits = sum(
        1
        for truth, note in matched
        if abs(float(note["start_s"]) - float(truth["start_s"])) <= tolerance_s
        and abs(float(note["end_s"]) - float(truth["end_s"])) <= tolerance_s
    )
    return {
        "reference_note_count": len(annotations),
        "predicted_note_count": len(notes),
        "matched_note_count": len(matched),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "start_boundary_mae_ms": round(float(np.mean(start_errors)) * 1000.0, 3)
        if start_errors
        else None,
        "end_boundary_mae_ms": round(float(np.mean(end_errors)) * 1000.0, 3)
        if end_errors
        else None,
        "boundary_mae_ms": round(float(np.mean(boundary_errors)) * 1000.0, 3)
        if boundary_errors
        else None,
        "boundary_hit_rate": round(boundary_hits / len(matched), 6) if matched else 0.0,
        "tolerance_s": tolerance_s,
    }


def _phoneme_metrics(
    phonemes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    tolerance_s: float,
) -> dict[str, Any]:
    predicted = sorted(phonemes, key=lambda item: (float(item["start_s"]), float(item["end_s"])))
    truth = sorted(annotations, key=lambda item: (float(item["start_s"]), float(item["end_s"])))
    pairs = list(zip(truth, predicted))
    boundary_errors: list[float] = []
    hits = 0
    label_matches = 0
    for truth_item, predicted_item in pairs:
        start_error = abs(float(predicted_item["start_s"]) - float(truth_item["start_s"]))
        end_error = abs(float(predicted_item["end_s"]) - float(truth_item["end_s"]))
        boundary_errors.extend([start_error, end_error])
        if max(start_error, end_error) <= tolerance_s:
            hits += 1
        truth_label = str(truth_item.get("label", "")).lower()
        predicted_label = str(predicted_item.get("label", "")).lower()
        if truth_label and truth_label == predicted_label:
            label_matches += 1
    boundary_mae = float(np.mean(boundary_errors)) if boundary_errors else None
    return {
        "reference_phoneme_count": len(truth),
        "predicted_phoneme_count": len(predicted),
        "paired_phoneme_count": len(pairs),
        "boundary_mae_ms": round(boundary_mae * 1000.0, 3)
        if boundary_mae is not None
        else None,
        "boundary_hit_rate": round(hits / len(pairs), 6) if pairs else 0.0,
        "label_match_rate": round(label_matches / len(pairs), 6) if pairs else 0.0,
        "tolerance_s": tolerance_s,
    }


def evaluate_sample(
    report: dict[str, Any],
    annotation_path: str | Path,
    config: Config,
) -> dict[str, Any]:
    payload = _load_annotation_payload(annotation_path)
    tolerances = payload.get("tolerances", {}) if isinstance(payload.get("tolerances"), dict) else {}
    pitch_tolerance = float(tolerances.get("pitch_cents", config.pitch_hit_cents))
    note_tolerance = float(
        tolerances.get("note_boundary_s", config.boundary_tolerance_s)
    )
    phoneme_tolerance = float(
        tolerances.get("phoneme_boundary_s", config.phoneme_boundary_tolerance_s)
    )
    frame_metrics = _frame_metrics(
        report["frames"], _frame_records(payload), config, pitch_tolerance
    )
    note_metrics = _note_metrics(
        report["notes"], payload.get("notes", []), note_tolerance
    )
    phoneme_metrics = _phoneme_metrics(
        report["phonemes"], payload.get("phonemes", []), phoneme_tolerance
    )
    expected_syllables = payload.get("syllable_count", len(report["syllables"]))
    mapping_validation = report["mapping_validation"]
    mapping_consistent = bool(
        mapping_validation["syllable_count_matches_text"]
        and int(expected_syllables) == int(mapping_validation["text_syllable_count"])
    )
    thresholds = {
        "pitch_hit_rate": float(tolerances.get("min_pitch_hit_rate", 0.90)),
        "voiced_f1": float(tolerances.get("min_voiced_f1", 0.88)),
        "note_boundary_mae_ms": float(tolerances.get("max_note_boundary_mae_ms", 100.0)),
        "phoneme_boundary_mae_ms": float(
            tolerances.get("max_phoneme_boundary_mae_ms", 100.0)
        ),
        "phoneme_boundary_hit_rate": float(
            tolerances.get("min_phoneme_boundary_hit_rate", 0.85)
        ),
    }
    checks = {
        "pitch_hit_rate": frame_metrics["pitch_hit_rate_reference_voiced"]
        >= thresholds["pitch_hit_rate"],
        "voiced_f1": frame_metrics["voiced_f1"] >= thresholds["voiced_f1"],
        "note_boundary_mae": (
            note_metrics["boundary_mae_ms"] is not None
            and note_metrics["boundary_mae_ms"] <= thresholds["note_boundary_mae_ms"]
        ),
        "phoneme_boundary_mae": (
            phoneme_metrics["boundary_mae_ms"] is not None
            and phoneme_metrics["boundary_mae_ms"]
            <= thresholds["phoneme_boundary_mae_ms"]
        ),
        "phoneme_boundary_hit_rate": phoneme_metrics["boundary_hit_rate"]
        >= thresholds["phoneme_boundary_hit_rate"],
        "syllable_mapping_consistent": mapping_consistent,
    }
    return {
        "schema_version": "1.0",
        "sample_id": report["sample_id"],
        "annotation_file": str(annotation_path),
        "tolerances": {
            "pitch_cents": pitch_tolerance,
            "note_boundary_s": note_tolerance,
            "phoneme_boundary_s": phoneme_tolerance,
        },
        "frame_metrics": frame_metrics,
        "note_metrics": note_metrics,
        "phoneme_metrics": phoneme_metrics,
        "syllable_mapping": {
            "expected_syllable_count": int(expected_syllables),
            "text_syllable_count": mapping_validation["text_syllable_count"],
            "mapped_syllable_count": mapping_validation["mapped_syllable_count"],
            "consistent": mapping_consistent,
        },
        "thresholds": thresholds,
        "checks": checks,
        "passed": all(checks.values()),
    }

