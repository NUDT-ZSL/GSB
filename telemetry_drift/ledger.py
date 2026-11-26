"""Validation ledger with self-contained replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import (
    atomic_write_text,
    canonical_json,
    read_json,
    sha256_value,
    stable_float,
    verify_payload_checksum,
)
from .config import PipelineConfig
from .errors import DriftError, E_LEDGER_REPLAY
from .retraining import (
    ClosureRetrain,
    RetrainOutcome,
    closure_result_digest,
    closure_result_payload,
    incremental_ridge_update,
)
from .snapshots import PredictorModel
from .statistics import compute_window_statistics
from .windows import WindowEvidence, window_evidence_from_cache


LEDGER_SCHEMA = "validation-ledger-v1"
ENTRY_SCHEMA = "validation-ledger-entry-v1"


def build_ledger_entries(
    outcome: RetrainOutcome,
    windows: Iterable[WindowEvidence],
) -> list[dict[str, Any]]:
    by_id = {window.window_id: window for window in windows}
    entries: list[dict[str, Any]] = []
    for retrain in outcome.retrains:
        covered_windows = retrain.closure.covered_windows
        evidence = [by_id[window_id].to_dict() for window_id in covered_windows]
        business_codes = sorted(
            set(retrain.business_codes)
            | set(retrain.closure.business_codes)
            | {"LEDGER_ENTRY_REPLAYABLE"}
        )
        body = {
            "schema": ENTRY_SCHEMA,
            "entry_id": retrain.closure.closure_id,
            "covered_windows": list(covered_windows),
            "recompute_range": retrain.closure.to_dict()["recompute_range"],
            "drift_scores": dict(sorted(retrain.drift_scores.items())),
            "business_codes": business_codes,
            "window_evidence": evidence,
            "retrain": {
                "model_before": retrain.model_before.to_dict(),
                "model_after": retrain.model_after.to_dict(),
                "retrain_samples": [
                    list(row) for row in retrain.retrain_samples
                ],
                "retrain_targets": list(retrain.retrain_targets),
                "old_predictions": {
                    key: list(value)
                    for key, value in sorted(retrain.old_predictions.items())
                },
                "new_predictions": {
                    key: list(value)
                    for key, value in sorted(retrain.new_predictions.items())
                },
            },
            "closure": retrain.closure.to_dict(),
            "result_digest": retrain.result_digest,
        }
        body["entry_checksum"] = sha256_value(body)
        entries.append(body)
    return entries


def write_ledger(
    path: str | Path,
    entries: Iterable[dict[str, Any]],
) -> None:
    materialized = list(entries)
    header = {
        "schema": LEDGER_SCHEMA,
        "entry_count": len(materialized),
        "entries_digest": sha256_value(materialized),
    }
    lines = [canonical_json(header)]
    lines.extend(canonical_json(entry) for entry in materialized)
    atomic_write_text(path, "\n".join(lines) + "\n")


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            first = True
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if first:
                    first = False
                    if payload.get("schema") != LEDGER_SCHEMA:
                        raise DriftError(
                            E_LEDGER_REPLAY, "ledger header schema mismatch"
                        )
                    continue
                entries.append(payload)
    except OSError as exc:
        raise DriftError(E_LEDGER_REPLAY, "cannot read ledger") from exc
    except json.JSONDecodeError as exc:
        raise DriftError(E_LEDGER_REPLAY, "invalid ledger JSON") from exc
    return entries


def replay_ledger(
    entries: Iterable[dict[str, Any]], config: PipelineConfig
) -> dict[str, Any]:
    results = [replay_entry(entry, config) for entry in entries]
    return {
        "ok": all(result["ok"] for result in results),
        "replayed_entries": results,
        "replayed_digest": sha256_value(results),
    }


def replay_entry(
    entry: dict[str, Any], config: PipelineConfig
) -> dict[str, Any]:
    try:
        verify_payload_checksum(entry, "entry_checksum")
        if entry.get("schema") != ENTRY_SCHEMA:
            raise DriftError(E_LEDGER_REPLAY, "ledger entry schema mismatch")
        evidence = [
            window_evidence_from_cache(payload, config)
            for payload in entry["window_evidence"]
        ]
        by_id = {window.window_id: window for window in evidence}
        covered = [str(item) for item in entry["covered_windows"]]
        if set(covered) != set(by_id):
            raise DriftError(
                E_LEDGER_REPLAY,
                "covered window set differs from evidence",
            )
        drift_scores: dict[str, float] = {}
        for window in evidence:
            recomputed = compute_window_statistics(
                window.raw_residuals,
                feature_deltas=window.feature_deltas,
                coefficients=window.coefficients,
                config=config.statistics,
            )
            stored = window.statistics
            for field in (
                "mean",
                "variance",
                "standard_deviation",
                "skewness",
                "trend",
                "upstream_contribution",
                "input_amplitude",
                "drift_score",
                "effective_sample_count",
            ):
                if abs(float(getattr(recomputed, field)) - float(getattr(stored, field))) > 1e-11:
                    raise DriftError(
                        E_LEDGER_REPLAY,
                        "window statistic replay mismatch",
                        window=window.window_id,
                        field=field,
                    )
            drift_scores[window.window_id] = recomputed.drift_score
        if drift_scores != {
            str(key): float(value)
            for key, value in entry["drift_scores"].items()
        }:
            raise DriftError(E_LEDGER_REPLAY, "drift score replay mismatch")

        closure = entry["closure"]
        if list(closure["covered_windows"]) != covered:
            raise DriftError(E_LEDGER_REPLAY, "closure coverage mismatch")
        if not set(closure["event_windows"]) <= set(covered):
            raise DriftError(E_LEDGER_REPLAY, "closure event coverage mismatch")

        retrain = entry["retrain"]
        model_before = PredictorModel.from_dict(retrain["model_before"])
        recorded_after = PredictorModel.from_dict(retrain["model_after"])
        samples = np.asarray(retrain["retrain_samples"], dtype=np.float64)
        targets = np.asarray(retrain["retrain_targets"], dtype=np.float64)
        replayed_after = incremental_ridge_update(
            model_before,
            samples,
            targets,
            config,
            trained_until_ms=int(closure["recompute_range"]["end_ms"]),
        )
        if replayed_after.coefficients != recorded_after.coefficients or (
            replayed_after.intercept != recorded_after.intercept
        ):
            raise DriftError(E_LEDGER_REPLAY, "retrained model replay mismatch")

        new_predictions: dict[str, tuple[float, ...]] = {}
        for window in evidence:
            predicted = replayed_after.predict(np.asarray(window.features))
            new_predictions[window.window_id] = tuple(
                stable_float(item) for item in predicted
            )
        recorded_predictions = {
            str(key): tuple(float(item) for item in value)
            for key, value in retrain["new_predictions"].items()
        }
        if new_predictions != recorded_predictions:
            raise DriftError(
                E_LEDGER_REPLAY, "replacement prediction replay mismatch"
            )

        from .retraining import DriftClosure

        closure_object = DriftClosure(
            closure_id=str(closure["closure_id"]),
            covered_windows=tuple(closure["covered_windows"]),
            event_windows=tuple(closure["event_windows"]),
            window_ordinals=tuple(closure["window_ordinals"]),
            sample_indices=tuple(closure["sample_indices"]),
            recompute_start_index=int(closure["recompute_range"]["start_index"]),
            recompute_end_index=int(closure["recompute_range"]["end_index"]),
            recompute_start_ms=int(closure["recompute_range"]["start_ms"]),
            recompute_end_ms=int(closure["recompute_range"]["end_ms"]),
            max_drift_score=float(closure["max_drift_score"]),
            business_codes=tuple(closure["business_codes"]),
        )
        old_predictions = {
            str(key): tuple(float(item) for item in value)
            for key, value in retrain["old_predictions"].items()
        }
        replayed_digest = closure_result_digest(
            closure=closure_object,
            model_before=model_before,
            model_after=replayed_after,
            old_predictions=old_predictions,
            new_predictions=new_predictions,
            retrain_samples=samples,
            retrain_targets=targets,
            drift_scores=drift_scores,
        )
        if replayed_digest != entry["result_digest"]:
            raise DriftError(
                E_LEDGER_REPLAY, "closure result digest replay mismatch"
            )
        return {
            "ok": True,
            "entry_id": entry["entry_id"],
            "covered_windows": covered,
            "drift_scores": drift_scores,
            "replayed_digest": replayed_digest,
            "business_codes": list(entry["business_codes"]),
        }
    except DriftError as exc:
        if exc.code == E_LEDGER_REPLAY:
            raise
        raise DriftError(
            E_LEDGER_REPLAY,
            exc.message,
            original_code=exc.code,
            details=exc.details,
        ) from exc

