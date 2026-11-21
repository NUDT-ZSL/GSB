"""Node 4: deterministic acoustic speaker attribution."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from ..config import AppConfig
from ..errors import fail
from ..models import Annotation
from ..util import ms_to_sample, quantize, sample_to_ms


NODE_VERSION = "speaker-v1"


def _validate_annotation_labels(
    annotations: Iterable[Annotation], labels: tuple[str, ...]
) -> None:
    controlled = set(labels)
    for annotation in annotations:
        if annotation.speaker_label not in controlled:
            fail(
                "E_SPEAKER_UNKNOWN",
                "annotation speaker label is outside the controlled set",
                segment_id=annotation.segment_id,
                clip_id=annotation.clip_id,
                speaker_label=annotation.speaker_label,
                allowed_labels=sorted(controlled),
            )
        if (annotation.confidence_lower is None) != (
            annotation.confidence_upper is None
        ):
            fail(
                "E_INTERVAL_INVALID",
                "annotation confidence interval requires both bounds",
                segment_id=annotation.segment_id,
            )
        if annotation.confidence_lower is not None:
            lower = annotation.confidence_lower
            upper = annotation.confidence_upper
            if lower > upper or lower < 0.0 or upper > 1.0:
                fail(
                    "E_INTERVAL_INVALID",
                    "annotation confidence interval bounds are invalid",
                    segment_id=annotation.segment_id,
                    confidence_lower=lower,
                    confidence_upper=upper,
                )


def _match_annotation(
    interval: dict[str, Any],
    annotations: list[Annotation],
    sample_rate: int,
) -> Annotation | None:
    interval_start = int(interval["start_sample"])
    interval_end = int(interval["end_sample"])
    best: tuple[int, str, Annotation] | None = None
    for annotation in annotations:
        annotation_start = ms_to_sample(annotation.start_ms, sample_rate)
        annotation_end = ms_to_sample(annotation.end_ms, sample_rate)
        overlap = max(
            0, min(interval_end, annotation_end) - max(interval_start, annotation_start)
        )
        if overlap <= 0:
            continue
        key = (overlap, annotation.segment_id, annotation)
        if best is None or overlap > best[0] or (
            overlap == best[0] and annotation.segment_id < best[1]
        ):
            best = key
    return best[2] if best else None


def _acoustic_features(
    interval: dict[str, Any],
    clip_envelope: dict[str, Any],
) -> tuple[float, float]:
    starts = np.asarray(clip_envelope["frame_start_samples"], dtype=np.int64)
    ends = np.asarray(clip_envelope["frame_end_samples"], dtype=np.int64)
    energy = np.asarray(clip_envelope["energy"], dtype=np.float64)
    zcr = np.asarray(clip_envelope["zcr"], dtype=np.float64)
    mask = (starts < int(interval["end_sample"])) & (
        ends > int(interval["start_sample"])
    )
    if not np.any(mask):
        return 0.0, 0.0
    return float(np.mean(energy[mask])), float(np.mean(zcr[mask]))


def _score_labels(
    energy: float,
    zcr: float,
    matched: Annotation | None,
    segment_id: str,
    config: AppConfig,
) -> list[tuple[float, str]]:
    scores: list[tuple[float, str]] = []
    for label in sorted(config.speaker.labels):
        template = config.speaker.templates[label]
        target_energy = max(float(template["energy"]), 1e-12)
        target_zcr = max(float(template["zcr"]), 1e-12)
        distance = abs(energy - target_energy) / target_energy + abs(
            zcr - target_zcr
        ) / target_zcr
        score = 1.0 / (1.0 + distance)
        if matched is not None and matched.speaker_label == label:
            score += config.speaker.annotation_bonus
        scores.append((quantize(max(0.0, score)), label))
    scores.sort(key=lambda item: (-item[0], item[1], segment_id))
    return scores


def _record_for_interval(
    clip_id: str,
    interval: dict[str, Any],
    clip_envelope: dict[str, Any],
    annotations: list[Annotation],
    config: AppConfig,
) -> dict[str, Any]:
    matched = _match_annotation(interval, annotations, config.sample_rate)
    segment_id = (
        matched.segment_id if matched is not None else str(interval["candidate_id"])
    )
    energy, zcr = _acoustic_features(interval, clip_envelope)
    scores = _score_labels(
        energy, zcr, matched, segment_id, config
    )
    best_score, best_label = scores[0]
    if matched is not None and matched.confidence_lower is not None:
        lower = float(matched.confidence_lower)
        upper = float(matched.confidence_upper)
    else:
        confidence_score = min(1.0, best_score)
        lower = max(0.0, confidence_score - config.speaker.confidence_margin)
        upper = min(1.0, confidence_score + config.speaker.confidence_margin)
    lower = quantize(lower)
    upper = quantize(upper)
    if lower > upper:
        fail(
            "E_INTERVAL_INVALID",
            "speaker confidence interval has lower bound above upper bound",
            segment_id=segment_id,
            confidence_lower=lower,
            confidence_upper=upper,
        )
    return {
        "clip_id": clip_id,
        "candidate_id": interval["candidate_id"],
        "segment_id": segment_id,
        "start_sample": int(interval["start_sample"]),
        "end_sample": int(interval["end_sample"]),
        "start_ms": sample_to_ms(int(interval["start_sample"]), config.sample_rate),
        "end_ms": sample_to_ms(int(interval["end_sample"]), config.sample_rate),
        "speaker_label": best_label,
        "label_revision": int(matched.label_revision) if matched is not None else 0,
        "confidence_lower": lower,
        "confidence_upper": upper,
        "score": best_score,
        "acoustic": {
            "energy": quantize(energy),
            "zcr": quantize(zcr),
        },
        "annotation_matched": matched is not None,
    }


def _overlaps_ranges(
    clip_id: str, interval: dict[str, Any], affected_ranges: list[dict[str, Any]]
) -> bool:
    start = int(interval["start_sample"])
    end = int(interval["end_sample"])
    for affected in affected_ranges:
        if affected.get("clip_id") != clip_id:
            continue
        if start < int(affected["end_sample"]) and end > int(
            affected["start_sample"]
        ):
            return True
    return False


def compute_speaker(
    envelope_payload: dict[str, Any],
    refine_payload: dict[str, Any],
    annotations: list[Annotation],
    config: AppConfig,
    *,
    affected_ranges: list[dict[str, Any]] | None = None,
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_annotation_labels(annotations, config.speaker.labels)
    annotations_by_clip: dict[str, list[Annotation]] = {}
    for annotation in annotations:
        annotations_by_clip.setdefault(annotation.clip_id, []).append(annotation)
    for values in annotations_by_clip.values():
        values.sort(key=lambda item: (item.start_ms, item.end_ms, item.segment_id))

    previous_records: dict[str, dict[str, Any]] = {}
    if previous_payload is not None:
        for clip_records in previous_payload.get("clips", {}).values():
            for record in clip_records.get("intervals", []):
                previous_records[str(record["candidate_id"])] = record

    ranges = affected_ranges or []
    clips_payload: dict[str, Any] = {}
    recomputed_intervals = 0
    reused_intervals = 0
    for clip_id in refine_payload["clip_order"]:
        clip_refine = refine_payload["clips"][clip_id]
        clip_envelope = envelope_payload["clips"][clip_id]
        clip_annotations = annotations_by_clip.get(clip_id, [])
        records: list[dict[str, Any]] = []
        for interval in clip_refine["intervals"]:
            candidate_id = str(interval["candidate_id"])
            reusable = (
                previous_records.get(candidate_id)
                if not _overlaps_ranges(clip_id, interval, ranges)
                else None
            )
            if (
                reusable is not None
                and int(reusable["start_sample"]) == int(interval["start_sample"])
                and int(reusable["end_sample"]) == int(interval["end_sample"])
            ):
                records.append(reusable)
                reused_intervals += 1
            else:
                records.append(
                    _record_for_interval(
                        clip_id,
                        interval,
                        clip_envelope,
                        clip_annotations,
                        config,
                    )
                )
                recomputed_intervals += 1
        records.sort(
            key=lambda item: (
                int(item["start_sample"]),
                int(item["end_sample"]),
                str(item["segment_id"]),
            )
        )
        clips_payload[clip_id] = {
            "clip_id": clip_id,
            "intervals": records,
        }
    return {
        "schema": NODE_VERSION,
        "clip_order": refine_payload["clip_order"],
        "stats": {
            "recomputed_intervals": recomputed_intervals,
            "reused_intervals": reused_intervals,
        },
        "clips": clips_payload,
    }


def speaker_key_details(
    refine_payload: dict[str, Any],
    upstream_hash: str,
    config: AppConfig,
    annotation_digest: str,
    annotation_revision: int,
) -> dict[str, Any]:
    return {
        "upstream_hash": upstream_hash,
        "annotation_hash": annotation_digest,
        "annotation_revision": annotation_revision,
        "threshold_version": config.vad.threshold_version,
        "threshold_params": {
            "noise_quantile": config.vad.noise_quantile,
            "low_multiplier": config.vad.low_multiplier,
            "high_multiplier": config.vad.high_multiplier,
            "min_silence_ms": config.vad.min_silence_ms,
        },
        "controlled_labels": sorted(config.speaker.labels),
        "templates": config.speaker.templates,
        "annotation_bonus": config.speaker.annotation_bonus,
        "confidence_margin": config.speaker.confidence_margin,
        "interval_ids": {
            clip_id: [
                interval["candidate_id"]
                for interval in refine_payload["clips"][clip_id]["intervals"]
            ]
            for clip_id in refine_payload["clip_order"]
        },
    }
