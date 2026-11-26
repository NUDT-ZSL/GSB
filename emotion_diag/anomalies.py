"""Turn-boundary anomaly detection from truth labels and energy envelope."""

from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Sequence

from .config import EngineConfig


def _rms_at(features: Sequence[Dict[str, Any]], index: int) -> float:
    if 0 <= index < len(features):
        return float(features[index].get("rms", 0.0))
    return 0.0


def _energy_window(features: Sequence[Dict[str, Any]], start: int, end: int) -> List[float]:
    start = max(0, start)
    end = min(len(features), end)
    return [_rms_at(features, i) for i in range(start, end)]


def _runs(truth: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    for row in truth:
        speaker_id = int(row.get("speaker_id", 0))
        frame_index = int(row["frame_index"])
        if not runs or runs[-1]["speaker_id"] != speaker_id:
            if runs:
                runs[-1]["end_frame"] = frame_index
            runs.append(
                {
                    "speaker_id": speaker_id,
                    "start_frame": frame_index,
                    "end_frame": len(truth),
                    "turn_labels": [],
                }
            )
        label = row.get("turn_label")
        if label is not None and label not in runs[-1]["turn_labels"]:
            runs[-1]["turn_labels"].append(label)
    if runs:
        runs[-1]["end_frame"] = int(truth[-1]["frame_index"]) + 1
    return runs


def _overlap_end_from_truth(
    truth: Sequence[Dict[str, Any]], boundary: int, outgoing_speaker: int
) -> int | None:
    end = None
    for index in range(boundary, len(truth)):
        active = truth[index].get("active_speaker_ids")
        if not isinstance(active, list):
            return None
        if outgoing_speaker in [int(x) for x in active]:
            end = index + 1
        elif end is not None:
            return end
    return end


def _energy_overlap_end(
    features: Sequence[Dict[str, Any]], boundary: int, config: EngineConfig
) -> int:
    threshold_ratio = float(config["anomaly"]["energy_drop_ratio"])
    post = _energy_window(features, boundary, boundary + 48)
    if not post:
        return boundary + 1
    peak = max(post)
    peak_offset = post.index(peak)
    search_from = boundary + max(4, peak_offset + 3)
    for index in range(search_from, min(len(features), boundary + 48)):
        if _rms_at(features, index) <= peak * (1.0 - threshold_ratio):
            return index
    return min(len(features), boundary + 30)


def detect_turn_anomalies(
    truth: Sequence[Dict[str, Any]],
    features: Sequence[Dict[str, Any]],
    config: EngineConfig,
) -> Dict[str, Any]:
    thresholds = config["anomaly"]
    long_pause = int(thresholds["long_pause_frames"])
    max_interruption = int(thresholds.get("short_interruption_frames", 45))
    sustain_ratio = float(thresholds["energy_sustain_ratio"])
    events: List[Dict[str, Any]] = []
    event_number = 1

    for index, row in enumerate(truth):
        if int(row.get("speaker_id", 0)) != 0:
            continue
        if index > 0 and int(truth[index - 1].get("speaker_id", 0)) == 0:
            continue
        start = index
        end = start
        while end < len(truth) and int(truth[end].get("speaker_id", 0)) == 0:
            end += 1
        if start == 0 or end >= len(truth):
            continue
        previous_row = truth[start - 1]
        next_row = truth[end]
        duration = end - start
        if duration < long_pause:
            continue
        energy = _energy_window(features, start, end)
        max_rms = max(energy) if energy else 0.0
        mean_rms = sum(energy) / len(energy) if energy else 0.0
        confidence = min(0.99, 0.82 + min(0.15, duration / (long_pause * 8.0)))
        events.append(
            {
                "event_id": f"EVT_{event_number:03d}",
                "type": "abnormal_pause",
                "start_frame": start,
                "end_frame": end,
                "start_ms": start * int(config["hop_ms"]),
                "end_ms": end * int(config["hop_ms"]),
                "confidence": round(confidence, 4),
                "truth_turn_labels": [
                    previous_row.get("turn_label"),
                    next_row.get("turn_label"),
                ],
                "evidence": {
                    "duration_frames": duration,
                    "duration_ms": duration * int(config["hop_ms"]),
                    "threshold_frames": long_pause,
                    "previous_speaker_id": int(previous_row.get("speaker_id", 0)),
                    "next_speaker_id": int(next_row.get("speaker_id", 0)),
                    "max_rms": round(max_rms, 9),
                    "mean_rms": round(mean_rms, 9),
                    "energy_floor": float(thresholds["energy_floor"]),
                    "summary_text": (
                        f"pause of {duration} frames exceeds {long_pause}; "
                        "bounded by two speech turns"
                    ),
                },
            }
        )
        event_number += 1

    runs = [run for run in _runs(truth) if int(run["speaker_id"]) > 0]
    for run_index in range(len(runs) - 1):
        previous = runs[run_index]
        current = runs[run_index + 1]
        if int(previous["end_frame"]) != int(current["start_frame"]):
            continue
        boundary = int(current["start_frame"])
        if run_index >= 1:
            prior = runs[run_index - 1]
            is_resume_boundary = (
                int(prior["speaker_id"]) == int(current["speaker_id"])
                and int(prior["end_frame"]) == int(previous["start_frame"])
                and int(previous["end_frame"]) == boundary
                and int(previous["end_frame"]) - int(previous["start_frame"])
                <= max_interruption
            )
            if is_resume_boundary:
                continue
        if run_index + 2 < len(runs):
            following = runs[run_index + 2]
            interruption_pattern = (
                int(following["speaker_id"]) == int(previous["speaker_id"])
                and int(current["end_frame"]) == int(following["start_frame"])
                and int(current["end_frame"]) - int(current["start_frame"])
                <= max_interruption
            )
        else:
            interruption_pattern = False

        pre_energy = median(_energy_window(features, boundary - 12, boundary) or [0.0])
        boundary_energy = median(_energy_window(features, boundary, boundary + 12) or [0.0])
        continuity = boundary_energy / max(pre_energy, 1e-9)
        zcr_delta = 0.0
        if boundary < len(features) and boundary > 0:
            zcr_delta = float(features[boundary].get("zcr", 0.0)) - float(
                features[boundary - 1].get("zcr", 0.0)
            )

        if interruption_pattern:
            start = int(current["start_frame"])
            end = int(current["end_frame"])
            confidence = 0.90
            if continuity >= sustain_ratio:
                confidence += 0.05
            if abs(zcr_delta) >= float(thresholds["overlap_zcr_delta"]) * 0.2:
                confidence += 0.02
            events.append(
                {
                    "event_id": f"EVT_{event_number:03d}",
                    "type": "interruption",
                    "start_frame": start,
                    "end_frame": end,
                    "start_ms": start * int(config["hop_ms"]),
                    "end_ms": end * int(config["hop_ms"]),
                    "confidence": round(min(0.99, confidence), 4),
                    "truth_turn_labels": current["turn_labels"]
                    + following["turn_labels"],
                    "evidence": {
                        "pattern": "A-B-A",
                        "interrupted_speaker_id": int(previous["speaker_id"]),
                        "interrupting_speaker_id": int(current["speaker_id"]),
                        "resuming_speaker_id": int(following["speaker_id"]),
                        "resume_frame": int(following["start_frame"]),
                        "interjection_frames": end - start,
                        "max_interruption_frames": max_interruption,
                        "pre_boundary_rms_median": round(pre_energy, 9),
                        "post_boundary_rms_median": round(boundary_energy, 9),
                        "energy_continuity_ratio": round(continuity, 6),
                        "zcr_delta": round(zcr_delta, 9),
                        "summary_text": (
                            "short B turn is enclosed by the same A speaker/turn label"
                        ),
                    },
                }
            )
            event_number += 1
            continue

        overlap_end = _overlap_end_from_truth(
            truth, boundary, int(previous["speaker_id"])
        )
        evidence_source = "frame_truth.active_speaker_ids+energy"
        if overlap_end is None:
            overlap_end = _energy_overlap_end(features, boundary, config)
            evidence_source = "energy_envelope+speaker_labels"
        confidence = 0.84
        if continuity >= sustain_ratio:
            confidence += 0.08
        if overlap_end > boundary + 2:
            confidence += 0.03
        if abs(zcr_delta) >= float(thresholds["overlap_zcr_delta"]):
            confidence += 0.02
        events.append(
            {
                "event_id": f"EVT_{event_number:03d}",
                "type": "floor_grab",
                "start_frame": boundary,
                "end_frame": int(overlap_end),
                "start_ms": boundary * int(config["hop_ms"]),
                "end_ms": int(overlap_end) * int(config["hop_ms"]),
                "confidence": round(min(0.98, confidence), 4),
                "truth_turn_labels": previous["turn_labels"] + current["turn_labels"],
                "evidence": {
                    "pattern": "A->B without_pause",
                    "previous_speaker_id": int(previous["speaker_id"]),
                    "current_speaker_id": int(current["speaker_id"]),
                    "pre_boundary_rms_median": round(pre_energy, 9),
                    "post_boundary_rms_median": round(boundary_energy, 9),
                    "energy_continuity_ratio": round(continuity, 6),
                    "zcr_delta": round(zcr_delta, 9),
                    "overlap_duration_frames": int(overlap_end - boundary),
                    "evidence_source": evidence_source,
                    "summary_text": (
                        "speaker label changes with no pause and sustained energy indicates floor grabbing"
                    ),
                },
            }
        )
        event_number += 1

    events.sort(key=lambda event: (int(event["start_frame"]), event["type"], event["event_id"]))
    for index, event in enumerate(events, start=1):
        event["event_id"] = f"EVT_{index:03d}"

    return {
        "status": "anomalies_detected" if events else "no_anomaly",
        "empty": not events,
        "marker": "ANOMALY_LIST_EMPTY" if not events else "ANOMALY_LIST_NONEMPTY",
        "anomaly_count": len(events),
        "events": events,
    }
