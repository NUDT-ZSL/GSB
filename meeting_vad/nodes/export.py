"""Node 5: canonical JSONL interval manifest export."""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..errors import fail
from ..util import canonical_json_line, sha256_bytes
from .vad import validate_interval_bounds


NODE_VERSION = "export-v1"


def _export_item(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "clip_id": record["clip_id"],
        "segment_id": record["segment_id"],
        "candidate_id": record["candidate_id"],
        "start_sample": int(record["start_sample"]),
        "end_sample": int(record["end_sample"]),
        "start_ms": float(record["start_ms"]),
        "end_ms": float(record["end_ms"]),
        "speaker_label": record["speaker_label"],
        "label_revision": int(record["label_revision"]),
        "confidence_lower": float(record["confidence_lower"]),
        "confidence_upper": float(record["confidence_upper"]),
    }


def compute_export(
    speaker_payload: dict[str, Any],
    clips_by_id: dict[str, Any],
    config: AppConfig,
) -> dict[str, Any]:
    controlled = set(config.speaker.labels)
    items: list[dict[str, Any]] = []
    for clip_id in speaker_payload["clip_order"]:
        clip_records = speaker_payload["clips"][clip_id]["intervals"]
        duration_ms = float(clips_by_id[clip_id].duration_ms)
        for record in clip_records:
            identifier = record.get("segment_id") or record.get("candidate_id")
            label = record.get("speaker_label")
            if label not in controlled:
                fail(
                    "E_SPEAKER_UNKNOWN",
                    "exported speaker label is outside the controlled set",
                    segment_id=identifier,
                    speaker_label=label,
                    allowed_labels=sorted(controlled),
                )
            lower = float(record.get("confidence_lower", 0.0))
            upper = float(record.get("confidence_upper", 0.0))
            if lower > upper or lower < 0.0 or upper > 1.0:
                fail(
                    "E_INTERVAL_INVALID",
                    "exported confidence interval is invalid",
                    segment_id=identifier,
                    confidence_lower=lower,
                    confidence_upper=upper,
                )
            item = _export_item(record)
            if item["start_ms"] < 0 or item["end_ms"] > duration_ms:
                fail(
                    "E_OUT_OF_RANGE",
                    "exported interval lies outside the clip duration",
                    interval_id=identifier,
                    start_ms=item["start_ms"],
                    end_ms=item["end_ms"],
                    duration_ms=duration_ms,
                )
            items.append(item)
        validate_interval_bounds(
            clip_records, duration_ms, "candidate_id"
        )

    items.sort(
        key=lambda item: (
            item["clip_id"],
            item["start_sample"],
            item["end_sample"],
            item["segment_id"],
        )
    )
    lines = [canonical_json_line(item) for item in items]
    jsonl_bytes = b"".join(lines)
    return {
        "schema": NODE_VERSION,
        "export_schema": config.export.schema_version,
        "clip_order": sorted(speaker_payload["clip_order"]),
        "items": items,
        "item_count": len(items),
        "jsonl": jsonl_bytes.decode("utf-8"),
        "jsonl_sha256": sha256_bytes(jsonl_bytes),
    }


def export_key_details(
    speaker_payload: dict[str, Any],
    upstream_hash: str,
    config: AppConfig,
    annotation_digest: str,
    annotation_revision: int,
    audio_manifest_hash: str,
) -> dict[str, Any]:
    return {
        "upstream_hash": upstream_hash,
        "audio_content_hash": audio_manifest_hash,
        "frame_hop_ms": config.frame_hop_ms,
        "threshold_version": config.vad.threshold_version,
        "threshold_params": {
            "noise_quantile": config.vad.noise_quantile,
            "low_multiplier": config.vad.low_multiplier,
            "high_multiplier": config.vad.high_multiplier,
            "min_silence_ms": config.vad.min_silence_ms,
        },
        "annotation_hash": annotation_digest,
        "annotation_revision": annotation_revision,
        "export_schema": config.export.schema_version,
        "item_count": sum(
            len(speaker_payload["clips"][clip_id]["intervals"])
            for clip_id in speaker_payload["clip_order"]
        ),
        "item_ids": [
            [item["clip_id"], item["segment_id"], item["start_sample"], item["end_sample"]]
            for clip_id in speaker_payload["clip_order"]
            for item in speaker_payload["clips"][clip_id]["intervals"]
        ],
    }
