"""Deterministic decision traces for replay."""

from __future__ import annotations

from typing import Any

from .calibration import calibrate_value
from .canonical import sha256_json
from .schema import record_score


def _score_segments(calibration_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not calibration_result:
        return []
    for item in calibration_result.get("items", []):
        if item.get("item") == "score" and item.get("status") == "calibrated":
            return item.get("segments", [])
    return []


def _category(score: float, bands: list[dict[str, Any]]) -> str:
    ordered = sorted(bands, key=lambda band: float(band.get("lower", float("-inf"))))
    for band in ordered:
        lower = band.get("lower")
        upper = band.get("upper")
        if lower is not None and score < float(lower):
            continue
        if upper is not None and score >= float(upper):
            continue
        return str(band.get("category", "unknown"))
    return ordered[-1].get("category", "unknown") if ordered else "unknown"


def build_decision_traces(
    datasets: list[dict[str, Any]],
    calibration_results: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    score_calibration = {
        batch_id: _score_segments(result) for batch_id, result in calibration_results.items()
    }
    score_config = next(
        (item for item in config.get("calibrations", []) if item.get("item") == "score"),
        {},
    )
    bands = score_config.get("threshold_bands") or config.get("decision_bands") or [
        {"category": "approve", "upper": 0.40},
        {"category": "manual_review", "lower": 0.40, "upper": 0.65},
        {"category": "high_risk", "lower": 0.65},
    ]

    traces = []
    for dataset in datasets:
        batch_id = dataset["batch_id"]
        segments = score_calibration.get(batch_id, [])
        for record in sorted(dataset["records"], key=lambda row: row.get("_line_no", 0)):
            score = record_score(record)
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            calibrated = calibrate_value(float(score), segments) if segments else None
            category_score = calibrated if calibrated is not None else float(score)
            identity = {
                "batch_id": batch_id,
                "cycle_id": dataset["cycle_id"],
                "line_no": record.get("_line_no"),
                "entity_id": record.get("entity_id"),
                "line_hash": record.get("_line_hash"),
            }
            traces.append(
                {
                    "trace_id": sha256_json(identity),
                    "batch_id": batch_id,
                    "cycle_id": dataset["cycle_id"],
                    "entity_id": record.get("entity_id") or record.get("id"),
                    "line_no": record.get("_line_no"),
                    "raw_score": float(score),
                    "calibrated_score": calibrated,
                    "decision_score": category_score,
                    "category": _category(category_score, bands),
                    "label": record.get("label"),
                    "calibration_status": calibration_results.get(batch_id, {}).get("status", "missing"),
                    "line_hash": record.get("_line_hash"),
                }
            )
    return traces


def traces_by_batch(traces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        grouped.setdefault(trace["batch_id"], []).append(trace)
    return dict(sorted(grouped.items()))
