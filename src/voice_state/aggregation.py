"""Duration-weighted dialog aggregation and deterministic report generation."""

from __future__ import annotations

from .canonical import checksum_value, round_float
from .config import AnalysisConfig
from .errors import BusinessError, E_INCONSISTENT
from .regression import MODEL_CHECKSUM, MODEL_KIND


WEIGHT_BASIS = {
    "name": "segment_duration_ms",
    "units": "milliseconds",
    "formula": "sum(segment_duration_ms * segment_score) / sum(segment_duration_ms)",
    "segment_scope": "all consecutive speaker segments, including non-speech frames",
}


def aggregate_dialog(
    regression_dialog: dict,
    config: AnalysisConfig,
) -> dict:
    segments = regression_dialog.get("segments", [])
    segments_out: list[dict] = []
    total_weight = 0.0
    valence_weighted = 0.0
    arousal_weighted = 0.0
    for segment in segments:
        weight = float(segment["duration_ms"])
        if weight <= 0.0:
            raise BusinessError(
                E_INCONSISTENT,
                "Segment weight must be positive",
                {
                    "dialog_id": regression_dialog["dialog_id"],
                    "start_frame": segment.get("start_frame"),
                    "duration_ms": segment.get("duration_ms"),
                },
            )
        valence = float(segment["valence"])
        arousal = float(segment["arousal"])
        total_weight += weight
        valence_weighted += weight * valence
        arousal_weighted += weight * arousal
        segments_out.append(
            {
                **segment,
                "aggregation_weight_ms": round_float(weight, config.score_places),
            }
        )
    if total_weight <= 0.0:
        raise BusinessError(
            E_INCONSISTENT,
            "Dialog has no segment weight",
            {"dialog_id": regression_dialog["dialog_id"]},
        )
    dialog_valence = round_float(valence_weighted / total_weight, config.score_places)
    dialog_arousal = round_float(arousal_weighted / total_weight, config.score_places)
    return {
        "dialog_id": regression_dialog["dialog_id"],
        "num_frames": int(regression_dialog["num_frames"]),
        "weighted_valence": dialog_valence,
        "weighted_arousal": dialog_arousal,
        "total_weight_ms": round_float(total_weight, config.score_places),
        "weight_basis": WEIGHT_BASIS,
        "segments": segments_out,
    }


def aggregate_all(
    regression_payload: dict,
    config: AnalysisConfig,
    *,
    registry_version: int,
    registry_checksum: str,
    config_checksum: str,
) -> dict:
    dialogs = [
        aggregate_dialog(item, config) for item in regression_payload.get("dialogs", [])
    ]
    dialogs.sort(key=lambda item: item["dialog_id"])
    ordered = sorted(
        dialogs,
        key=lambda item: (
            -item["weighted_arousal"],
            -item["weighted_valence"],
            item["dialog_id"],
        ),
    )
    ranking = [
        {
            "rank": rank,
            "dialog_id": item["dialog_id"],
            "weighted_arousal": item["weighted_arousal"],
            "weighted_valence": item["weighted_valence"],
        }
        for rank, item in enumerate(ordered, 1)
    ]
    total_weight = sum(item["total_weight_ms"] for item in dialogs)
    summary_valence = (
        round_float(
            sum(item["weighted_valence"] * item["total_weight_ms"] for item in dialogs)
            / total_weight,
            config.score_places,
        )
        if total_weight
        else 0.0
    )
    summary_arousal = (
        round_float(
            sum(item["weighted_arousal"] * item["total_weight_ms"] for item in dialogs)
            / total_weight,
            config.score_places,
        )
        if total_weight
        else 0.0
    )
    report: dict = {
        "schema_version": 1,
        "registry_version": registry_version,
        "registry_checksum": registry_checksum,
        "config_checksum": config_checksum,
        "model": {
            "kind": MODEL_KIND,
            "coefficients_checksum": MODEL_CHECKSUM,
        },
        "aggregation": WEIGHT_BASIS,
        "dialogs": dialogs,
        "ranking": ranking,
        "summary": {
            "dialog_count": len(dialogs),
            "total_weight_ms": round_float(total_weight, config.score_places),
            "weighted_valence": summary_valence,
            "weighted_arousal": summary_arousal,
            "ranking_method": "weighted_arousal_desc, weighted_valence_desc, dialog_id_asc",
        },
    }
    report["report_hash"] = report_hash(report)
    return report


def report_hash(report_without_hash: dict) -> str:
    source = dict(report_without_hash)
    source.pop("report_hash", None)
    return checksum_value(source)


def recompute_dialog_from_details(
    dialog: dict,
) -> tuple[float, float]:
    segments = dialog["segments"]
    total = sum(float(segment["duration_ms"]) for segment in segments)
    if total <= 0:
        raise BusinessError(E_INCONSISTENT, "Aggregation details have zero weight", {"dialog_id": dialog.get("dialog_id")})
    valence = sum(float(segment["duration_ms"]) * float(segment["valence"]) for segment in segments) / total
    arousal = sum(float(segment["duration_ms"]) * float(segment["arousal"]) for segment in segments) / total
    return valence, arousal

