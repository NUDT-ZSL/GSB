"""End-to-end orchestration for the local precision-propagation pipeline."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import load_config, normalize_config
from .consistency import verify_report_consistency
from .errors import ConfigInvalid
from .evaluation import evaluate_metrics
from .forecast import forecast_windows
from .precision import Quantizer, quantization_audit
from .scaling import fit_feature_scaler
from .series import SeriesData, generate_series
from .snapshots import SnapshotStore
from .spectrum import build_error_spectrum
from .training import train_models
from .utils import hash_payload, matrix_digest, sha256_json
from .windows import WindowCollection, build_windows, ordered_training_rows


def _public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in config.items()
        if not str(key).startswith("_")
    }


def _validate_series_compatibility(
    series: SeriesData,
    config: Mapping[str, Any],
) -> None:
    if series.length != int(config["series"]["length"]):
        raise ConfigInvalid(
            "series.length",
            (
                f"input series length {series.length} does not match configured "
                f"length {config['series']['length']}"
            ),
        )
    required_exogenous = [
        str(item["name"]) for item in config["series"]["exogenous"]
    ]
    required_events = [str(item["name"]) for item in config["series"]["events"]]
    missing_exogenous = [
        name for name in required_exogenous if name not in series.exogenous
    ]
    missing_events = [name for name in required_events if name not in series.events]
    if missing_exogenous:
        raise ConfigInvalid(
            "series.exogenous",
            f"input series is missing exogenous columns: {missing_exogenous}",
        )
    if missing_events:
        raise ConfigInvalid(
            "series.events",
            f"input series is missing event columns: {missing_events}",
        )


def _threshold_history(
    config: Mapping[str, Any],
    store: SnapshotStore | None,
    snapshot_names: list[str],
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if store is not None and "precision_config" in snapshot_names:
        existing = store.load("precision_config")
        if existing is not None:
            history = list(
                existing["payload"].get("threshold_strategy_history", [])
            )
    strategy = str(config["evaluation"]["threshold_strategy"])
    previous_strategy = history[-1]["strategy"] if history else None
    changed = previous_strategy != strategy
    if changed or not history:
        history.append(
            {
                "sequence": len(history) + 1,
                "previous_strategy": previous_strategy,
                "strategy": strategy,
                "changed": bool(changed),
                "reason": str(
                    config["evaluation"].get("strategy_reason", "config_update")
                ),
                "threshold": float(config["evaluation"]["threshold"]),
                "tolerance": float(config["evaluation"]["tolerance"]),
            }
        )
    return history


def _attach_open_loop_details(
    actual: dict[str, Any],
    open_loop: dict[str, Any],
) -> None:
    open_loop_windows = {
        str(window["window_id"]): window for window in open_loop["windows"]
    }
    for window in actual["windows"]:
        peer = open_loop_windows[str(window["window_id"])]
        for step, open_step in zip(window["steps"], peer["steps"]):
            open_prediction = float(open_step["prediction"])
            step["open_loop_diagnostic_prediction"] = open_prediction
            step["recursive_feedback_delta"] = float(
                abs(float(step["prediction"]) - open_prediction)
            )
            step["open_loop_note"] = (
                "diagnostic only; open-loop uses true prior targets and is never "
                "used by recursive execution"
            )


def _window_report(collection: WindowCollection) -> dict[str, Any]:
    summary = collection.summary()
    return {
        **summary,
        "items": [window.summary() for window in collection.windows],
    }


def _reference_summary(reference_training: Any, reference_forecast: Any) -> dict[str, Any]:
    return {
        "profile": "float64",
        "training": "full_batch",
        "feature_quantization": "identity",
        "model": reference_training.full_batch_model.to_dict(),
        "training_round_sha256": sha256_json(reference_training.full_batch_rounds),
        "prediction_sha256": reference_forecast["prediction_sha256"],
    }


def _quantization_probes(
    scaler: Any,
    model: Any,
    target: np.ndarray,
) -> list[float]:
    probes: list[float] = []
    probes.extend(float(value) for value in scaler.means[:3])
    probes.extend(float(value) for value in scaler.stds[:3])
    parameters = model.parameter_vector()
    probes.extend(float(value) for value in parameters[:8])
    probes.extend(float(value) for value in target[:3])
    return probes


def _snapshot_payloads(
    *,
    report: Mapping[str, Any],
    config: Mapping[str, Any],
    threshold_history: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        "window_stats": {
            "window_count": report["windows"]["train_count"]
            + report["windows"]["test_count"],
            "train_count": report["windows"]["train_count"],
            "test_count": report["windows"]["test_count"],
            "split_index": report["windows"]["split_index"],
            "separation_gap": report["windows"]["separation_gap"],
            "no_train_test_overlap": report["windows"]["no_train_test_overlap"],
            "feature_columns": report["windows"]["feature_columns"],
            "collection_digest": report["windows"]["collection_digest"],
            "window_ids": [
                window["window_id"] for window in report["windows"]["items"]
            ],
        },
        "precision_config": {
            "precision": report["precision"],
            "evaluation": {
                "threshold": float(config["evaluation"]["threshold"]),
                "tolerance": float(config["evaluation"]["tolerance"]),
                "threshold_strategy": str(
                    config["evaluation"]["threshold_strategy"]
                ),
                "metrics": list(config["evaluation"]["metrics"]),
            },
            "config_sha256": report["config_sha256"],
            "threshold_strategy_history": threshold_history,
        },
        "error_spectrum": {
            "reference": report["error_spectrum"]["reference"],
            "dominant_source": report["error_spectrum"]["dominant_source"],
            "dominant_absolute_error": report["error_spectrum"][
                "dominant_absolute_error"
            ],
            "levels": [
                {
                    "level": level["level"],
                    "absolute_error": level["absolute_error"],
                    "relative_error": level["relative_error"],
                    "interval_lower": level["interval_lower"],
                    "interval_upper": level["interval_upper"],
                    "amplification_ratio": level["amplification_ratio"],
                }
                for level in report["error_spectrum"]["levels"]
            ],
            "total_vs_reference": {
                key: report["error_spectrum"]["total_vs_reference"][key]
                for key in (
                    "absolute_error",
                    "interval_lower",
                    "interval_upper",
                )
            },
        },
    }


def run_pipeline(
    config_or_raw: Mapping[str, Any] | None = None,
    series: SeriesData | None = None,
    *,
    requested_steps: int | None = None,
    snapshot_versions: Mapping[str, int] | None = None,
    write_snapshots: bool = True,
) -> dict[str, Any]:
    """Run every stage and return a JSON-safe report.

    Snapshot versions are monotonic by default. Pass explicit versions only when
    testing rollback behavior; older versions are rejected by the store.
    """

    raw = config_or_raw or {}
    config = (
        dict(raw)
        if "_feature_lookback" in raw
        else normalize_config(raw)
    )
    sample = series if series is not None else generate_series(config)
    _validate_series_compatibility(sample, config)
    collection = build_windows(sample, config)
    raw_training_matrix, one_step_targets, row_map = ordered_training_rows(collection)
    scaler = fit_feature_scaler(collection, config)
    scaled_training_matrix = scaler.transform(raw_training_matrix)

    execution_quantizer = Quantizer.from_config(config)
    identity_quantizer = Quantizer.identity()
    actual_training = train_models(
        scaled_training_matrix,
        one_step_targets,
        config,
        feature_quantizer=execution_quantizer,
        model_quantizer=execution_quantizer,
    )
    reference_training = train_models(
        scaled_training_matrix,
        one_step_targets,
        config,
        feature_quantizer=identity_quantizer,
        model_quantizer=identity_quantizer,
    )
    reference_model = reference_training.full_batch_model

    reference_forecast = forecast_windows(
        reference_model,
        collection,
        sample,
        scaler,
        config,
        identity_quantizer,
    )
    feature_counterfactual = forecast_windows(
        reference_model,
        collection,
        sample,
        scaler,
        config,
        execution_quantizer,
    )
    actual_forecast = forecast_windows(
        actual_training.model,
        collection,
        sample,
        scaler,
        config,
        execution_quantizer,
        requested_steps=requested_steps,
    )
    open_loop_actual = forecast_windows(
        actual_training.model,
        collection,
        sample,
        scaler,
        config,
        execution_quantizer,
        mode="open_loop_diagnostic",
        requested_steps=requested_steps,
    )
    pre_quantization_forecast = forecast_windows(
        actual_training.pre_model_quantization_model,
        collection,
        sample,
        scaler,
        config,
        execution_quantizer,
        requested_steps=requested_steps,
    )
    _attach_open_loop_details(actual_forecast, open_loop_actual)

    spectrum = build_error_spectrum(
        reference=reference_forecast,
        feature_counterfactual=feature_counterfactual,
        actual=actual_forecast,
        open_loop_actual=open_loop_actual,
        pre_model_quantization=pre_quantization_forecast,
        precision_profile=execution_quantizer.profile,
    )
    metrics = evaluate_metrics(actual_forecast["steps_flat"], config)

    snapshot_config = config["snapshot"]
    snapshot_names = list(snapshot_config["names"])
    store: SnapshotStore | None = None
    if bool(snapshot_config["enabled"]) and write_snapshots:
        store = SnapshotStore(snapshot_config["directory"])
    threshold_history = _threshold_history(
        config,
        store,
        snapshot_names,
    )

    public_config = _public_config(config)
    probes = _quantization_probes(
        scaler,
        actual_training.model,
        sample.target,
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "business_code": "OK",
        "name": config["name"],
        "config": public_config,
        "config_sha256": hash_payload(public_config),
        "sample": {
            **sample.summary(),
            "content_checksum": sample.content_checksum(),
        },
        "windows": _window_report(collection),
        "feature_scaling": scaler.summary(),
        "training": {
            **actual_training.summary(),
            "window_ids": collection.train_ids,
            "row_map_sha256": sha256_json(row_map),
            "raw_training_matrix_sha256": sha256_json(
                {
                    "shape": list(raw_training_matrix.shape),
                    "digest": matrix_digest(raw_training_matrix),
                }
            ),
        },
        "precision": {
            **execution_quantizer.describe(),
            "quantization_audit": quantization_audit(probes, execution_quantizer),
            "audit_recompute": (
                "fixed_point: round(original*scale, rounding)/scale"
                if execution_quantizer.is_fixed
                else "profile cast is recorded verbatim"
            ),
        },
        "analysis_reference": _reference_summary(
            reference_training,
            reference_forecast,
        ),
        "error_spectrum": spectrum,
        "predictions": actual_forecast,
        "metrics": metrics,
        "threshold_policy": {
            "current_strategy": str(
                config["evaluation"]["threshold_strategy"]
            ),
            "threshold": float(config["evaluation"]["threshold"]),
            "tolerance": float(config["evaluation"]["tolerance"]),
            "history": threshold_history,
        },
        "reproducibility": {
            "window_collection_sha256": collection.collection_digest,
            "feature_matrix_sha256": sha256_json(
                [window.feature_digest for window in collection.windows]
            ),
            "training_rounds_sha256": actual_training.summary()[
                "reproducibility"
            ]["rounds_sha256"],
            "reference_prediction_sha256": reference_forecast[
                "prediction_sha256"
            ],
            "actual_prediction_sha256": actual_forecast["prediction_sha256"],
            "error_spectrum_sha256": sha256_json(spectrum),
        },
    }

    consistency = verify_report_consistency(report)
    report["consistency"] = consistency

    if store is not None:
        payloads_all = _snapshot_payloads(
            report=report,
            config=config,
            threshold_history=threshold_history,
        )
        payloads = {
            name: payloads_all[name]
            for name in snapshot_names
        }
        explicit_versions = (
            dict(snapshot_versions)
            if snapshot_versions is not None
            else dict(snapshot_config.get("versions", {}))
        )
        if (
            snapshot_config["version_mode"] == "explicit"
            and snapshot_versions is None
            and any(name not in explicit_versions for name in snapshot_names)
        ):
            raise ConfigInvalid(
                "snapshot.versions",
                "explicit version mode requires a version for every snapshot name",
            )
        committed = store.sync(payloads, explicit_versions)
        report["snapshots"] = {
            "enabled": True,
            "directory": str(snapshot_config["directory"]),
            "items": {
                name: {
                    "version": int(envelope["version"]),
                    "checksum": str(envelope["checksum"]),
                    "path": str(store.path_for(name)),
                }
                for name, envelope in committed.items()
            },
        }
    else:
        report["snapshots"] = {
            "enabled": False,
            "items": {},
        }

    report["deterministic_core_sha256"] = sha256_json(
        {
            key: report[key]
            for key in (
                "config_sha256",
                "sample",
                "windows",
                "feature_scaling",
                "training",
                "precision",
                "analysis_reference",
                "error_spectrum",
                "predictions",
                "metrics",
                "threshold_policy",
                "consistency",
            )
        }
    )
    return report


def run_pipeline_from_config(
    config_path: str | Path,
    series: SeriesData | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return run_pipeline(load_config(config_path), series=series, **kwargs)
