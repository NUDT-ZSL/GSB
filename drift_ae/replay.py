"""Deterministic replay of diagnostic records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .features import ScoringConfig, extract_features
from .io_utils import payload_checksum, read_jsonl
from .preprocessing import PreprocessedWindow
from .thresholds import ThresholdModel, ThresholdRow, adjudicate


def replay_record(record: dict[str, Any]) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []

    stored_checksum = record.get("record_checksum")
    if stored_checksum:
        expected_checksum = payload_checksum({k: v for k, v in record.items() if k != "record_checksum"})
        if expected_checksum != stored_checksum:
            mismatches.append(
                {
                    "field": "record_checksum",
                    "expected": expected_checksum,
                    "actual": stored_checksum,
                }
            )

    pre_cfg = record.get("preprocess_config", {})
    score_cfg_data = dict(record.get("scoring_config", {}))
    score_cfg = ScoringConfig(**score_cfg_data) if score_cfg_data else ScoringConfig()
    mask = np.asarray(record.get("mask", []), dtype=bool)
    completed = np.asarray(record.get("completed_window", []), dtype=np.float64)
    reconstruction = np.asarray(record.get("reconstruction", []), dtype=np.float64)
    mask_decay = float(pre_cfg.get("mask_decay", 0.25))
    weights = np.where(mask, mask_decay, 1.0).astype(np.float64)
    input_shape = tuple(record.get("input_shape", (32, 4)))
    if len(input_shape) != 2:
        input_shape = (int(np.prod(input_shape)), 1)

    preprocessed = PreprocessedWindow(
        window_id=str(record["window_id"]),
        raw=np.asarray(record.get("raw_window", []), dtype=np.float64),
        normalized=np.asarray(record.get("normalized_window", []), dtype=np.float64),
        mask=mask,
        completed=completed,
        contribution_weights=weights,
        input_shape=input_shape,
        traces=[],
    )
    features = extract_features(
        preprocessed,
        reconstruction,
        smooth_kernel=int(pre_cfg.get("smooth_kernel", 5)),
        scoring_config=score_cfg,
    )
    stored_features = record.get("features", {})
    if features.checksum != stored_features.get("checksum"):
        mismatches.append(
            {
                "field": "features.checksum",
                "expected": features.checksum,
                "actual": stored_features.get("checksum"),
            }
        )
    for key in ("score", "mean_residual", "max_residual", "smooth_residual", "mask_consistency"):
        expected = float(features.scalar_values()[key])
        actual = float(stored_features.get(key))
        if expected != actual:
            mismatches.append({"field": f"features.{key}", "expected": expected, "actual": actual})

    threshold_info = record["threshold"]
    row = ThresholdRow.from_dict(threshold_info["row"])
    model = ThresholdModel(
        model_version=int(threshold_info.get("model_version", 1)),
        rows=[row],
        tolerance_ratio=float(threshold_info.get("tolerance_ratio", 0.02)),
    )
    strategy = threshold_info.get("strategy", "strict")
    replayed_decision = adjudicate(
        features.score,
        str(record["segment_id"]),
        model,
        strategy=strategy,
    )
    stored_decision = record["decision"]
    for key in ("severity", "is_anomaly", "strategy"):
        if replayed_decision.get(key) != stored_decision.get(key):
            mismatches.append(
                {
                    "field": f"decision.{key}",
                    "expected": replayed_decision.get(key),
                    "actual": stored_decision.get(key),
                }
            )
    if replayed_decision["threshold_row_checksum"] != stored_decision["band"]["segment_checksum"]:
        mismatches.append(
            {
                "field": "threshold_row_checksum",
                "expected": replayed_decision["threshold_row_checksum"],
                "actual": stored_decision["band"]["segment_checksum"],
            }
        )

    return {
        "record_id": record.get("record_id"),
        "window_id": record.get("window_id"),
        "matches": not mismatches,
        "mismatches": mismatches,
        "replayed_score": features.score,
        "replayed_severity": replayed_decision["severity"],
        "replayed_is_anomaly": replayed_decision["is_anomaly"],
        "threshold_model_version": threshold_info.get("model_version"),
        "calibration_path": record.get("calibration_path") or record.get("recalibration_path"),
    }


def replay_file(path: str | Path) -> dict[str, Any]:
    records = read_jsonl(path)
    results = [replay_record(record) for record in records]
    return {
        "ok": all(item["matches"] for item in results),
        "record_count": len(records),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Replay drift-AE diagnostic JSONL records")
    parser.add_argument("path", help="Path to diagnostics.jsonl")
    args = parser.parse_args(argv)
    result = replay_file(args.path)
    printable = {
        "ok": result["ok"],
        "record_count": result["record_count"],
        "mismatches": [item for item in result["results"] if not item["matches"]],
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
