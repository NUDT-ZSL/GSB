"""Cross-stage self checks that point at the first inconsistent field."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .errors import InconsistentError
from .evaluation import evaluate_metrics, threshold_decision
from .models import model_from_dict
from .spectrum import CONTROLLED_LEVELS
from .utils import rms

REPORT_FLOOR = 1e-12


def _fail(position: str, message: str, **details: Any) -> None:
    raise InconsistentError(message, position=position, **details)


def _close(left: float, right: float, *, absolute: float = 1e-12) -> bool:
    return math.isclose(
        float(left),
        float(right),
        rel_tol=0.0,
        abs_tol=absolute,
    )


def _steps(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for window in report["predictions"]["windows"]:
        result.extend(window["steps"])
    return result


def verify_report_consistency(
    report: Mapping[str, Any],
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    first_failure: str | None = None

    def check(name: str, function: Any) -> None:
        nonlocal first_failure
        try:
            detail = function()
        except InconsistentError as exc:
            if raise_on_error:
                raise
            position = str(exc.details.get("position", name))
            first_failure = first_failure or position
            checks.append(
                {
                    "name": name,
                    "status": "FAIL",
                    "position": position,
                    "detail": exc.message,
                }
            )
            return
        except Exception as exc:
            if raise_on_error:
                _fail(name, str(exc))
            first_failure = first_failure or name
            checks.append({"name": name, "status": "FAIL", "detail": str(exc)})
            return
        checks.append({"name": name, "status": "OK", "detail": detail})

    def window_ids() -> dict[str, Any]:
        windows = report["windows"]["items"]
        items = [
            (
                int(window["window_index"]),
                int(window["start_index"]),
                str(window["window_id"]),
                str(window["split"]),
            )
            for window in windows
        ]
        if items != sorted(items):
            _fail("windows.items", "windows are not sorted by start index")
        if len({item[2] for item in items}) != len(items):
            _fail("windows.items", "duplicate window IDs")
        train_ids = [
            item[2] for item in items if item[3] == "train"
        ]
        test_ids = [
            item[2] for item in items if item[3] == "test"
        ]
        if train_ids != report["training"]["window_ids"]:
            _fail(
                "training.window_ids",
                "training stage window ID set/order differs from window split",
            )
        prediction_ids = [
            str(window["window_id"])
            for window in report["predictions"]["windows"]
        ]
        if prediction_ids != test_ids:
            _fail(
                "predictions.windows",
                "prediction stage window ID set/order differs from test windows",
            )
        for level in report["error_spectrum"]["levels"]:
            level_ids = [
                str(item["window_id"]) for item in level["deviations"]
            ]
            if level_ids != test_ids:
                _fail(
                    f"error_spectrum.levels.{level['level']}.deviations",
                    "error-spectrum window IDs differ from predictions",
                )
        columns = report["windows"]["feature_columns"]
        for window in windows:
            if window["feature_columns"] != columns:
                _fail(
                    f"windows.items.{window['window_id']}.feature_columns",
                    "feature column order changed across windows",
                )
        return {
            "train_window_count": len(train_ids),
            "test_window_count": len(test_ids),
        }

    def prediction_recompute() -> dict[str, Any]:
        model = model_from_dict(report["training"]["model"])
        checked = 0
        for window in report["predictions"]["windows"]:
            wpos = (
                f"predictions.windows[{window['window_index']}]"
                f"({window['window_id']})"
            )
            for step in window["steps"]:
                position = f"{wpos}.steps[{step['step']}]"
                features = step["executed_features"]
                if len(features) != model.feature_count:
                    _fail(
                        f"{position}.executed_features",
                        "feature vector length differs from model feature count",
                    )
                recomputed = model.predict_one(features)
                if not _close(recomputed, step["prediction"]):
                    _fail(
                        f"{position}.prediction",
                        "prediction cannot be reproduced from features and model",
                        reported=float(step["prediction"]),
                        recomputed=float(recomputed),
                    )
                if not _close(
                    abs(float(step["prediction"]) - float(step["true_value"])),
                    step["absolute_error"],
                ):
                    _fail(
                        f"{position}.absolute_error",
                        "absolute error does not match prediction and true value",
                    )
                checked += 1
        return {"checked_steps": checked}

    def metrics_recompute() -> dict[str, Any]:
        recomputed = evaluate_metrics(_steps(report), report["config"])
        for key in (
            "step_count",
            "mae",
            "rmse",
            "mape_percent",
            "smape_percent",
            "max_abs_error",
            "threshold_pass_rate",
            "interval_coverage_rate",
        ):
            if not _close(float(recomputed[key]), float(report["metrics"][key])):
                _fail(
                    f"metrics.{key}",
                    "metric disagrees with prediction detail recomputation",
                    reported=float(report["metrics"][key]),
                    recomputed=float(recomputed[key]),
                )
        return {"metric_count": 7}

    def spectrum_recompute() -> dict[str, Any]:
        levels = report["error_spectrum"]["levels"]
        if [item["level"] for item in levels] != list(CONTROLLED_LEVELS):
            _fail("error_spectrum.levels", "controlled level order is invalid")
        previous = None
        for level in levels:
            deviations = level["deviations"]
            absolute = rms(
                item["absolute_deviation_rms"] for item in deviations
            )
            lower = min(
                (item["absolute_deviation_rms"] for item in deviations),
                default=0.0,
            )
            upper = max(
                (item["absolute_deviation_rms"] for item in deviations),
                default=0.0,
            )
            if not _close(absolute, level["absolute_error"]):
                _fail(
                    f"error_spectrum.levels.{level['level']}.absolute_error",
                    "spectrum absolute error cannot be recomputed from deviations",
                )
            if not _close(lower, level["interval_lower"]):
                _fail(
                    f"error_spectrum.levels.{level['level']}.interval_lower",
                    "spectrum interval lower bound is inconsistent",
                )
            if not _close(upper, level["interval_upper"]):
                _fail(
                    f"error_spectrum.levels.{level['level']}.interval_upper",
                    "spectrum interval upper bound is inconsistent",
                )
            expected_ratio = (
                1.0
                if previous is None
                else absolute / max(previous, REPORT_FLOOR)
            )
            if not _close(expected_ratio, level["amplification_ratio"]):
                _fail(
                    f"error_spectrum.levels.{level['level']}.amplification_ratio",
                    "amplification ratio cannot be recomputed",
                )
            reference_rms = rms(
                item["baseline_rms"] for item in deviations
            )
            expected_relative = absolute / max(reference_rms, REPORT_FLOOR)
            if not _close(expected_relative, level["relative_error"]):
                _fail(
                    f"error_spectrum.levels.{level['level']}.relative_error",
                    "relative error cannot be recomputed from deviations",
                )
            for deviation in deviations:
                expected_window_relative = (
                    float(deviation["absolute_deviation_rms"])
                    / max(float(deviation["baseline_rms"]), REPORT_FLOOR)
                )
                if not _close(
                    expected_window_relative,
                    deviation["relative_deviation"],
                ):
                    _fail(
                        (
                            f"error_spectrum.levels.{level['level']}"
                            f".deviations.{deviation['window_id']}"
                            ".relative_deviation"
                        ),
                        "per-window relative deviation is inconsistent",
                    )
            previous = absolute
        dominant = max(
            levels,
            key=lambda item: item["absolute_error"],
        )
        expected_dominant = (
            "none"
            if float(dominant["absolute_error"]) <= REPORT_FLOOR
            else str(dominant["level"])
        )
        if expected_dominant != report["error_spectrum"]["dominant_source"]:
            _fail(
                "error_spectrum.dominant_source",
                "dominant source does not match spectrum aggregates",
            )
        total = report["error_spectrum"]["total_vs_reference"]
        total_abs = rms(
            item["absolute_deviation_rms"] for item in total["deviations"]
        )
        if not _close(total_abs, total["absolute_error"]):
            _fail(
                "error_spectrum.total_vs_reference.absolute_error",
                "total reference deviation cannot be recomputed",
            )
        for deviation in total["deviations"]:
            expected_relative = (
                float(deviation["absolute_deviation_rms"])
                / max(float(deviation["reference_rms"]), REPORT_FLOOR)
            )
            if not _close(expected_relative, deviation["relative_deviation"]):
                _fail(
                    (
                        "error_spectrum.total_vs_reference.deviations."
                        f"{deviation['window_id']}.relative_deviation"
                    ),
                    "total reference relative deviation is inconsistent",
                )
        return {"level_count": len(levels)}

    def threshold_recompute() -> dict[str, Any]:
        protected = 0
        skipped = 0
        for window in report["predictions"]["windows"]:
            for step in window["steps"]:
                expected = threshold_decision(
                    float(step["absolute_error"]),
                    report["config"],
                )
                for field in ("passed", "protected", "skipped"):
                    if bool(expected[field]) != bool(step["decision"][field]):
                        _fail(
                            (
                                f"predictions.windows[{window['window_index']}]"
                                f"({window['window_id']}).steps[{step['step']}]"
                                f".decision.{field}"
                            ),
                            "threshold decision differs from policy execution",
                        )
                protected += int(bool(expected["protected"]))
                skipped += int(bool(expected["skipped"]))
        return {"protected_decisions": protected, "skipped_decisions": skipped}

    check("window_id_sets", window_ids)
    check("prediction_recompute", prediction_recompute)
    check("metrics_recompute", metrics_recompute)
    check("spectrum_recompute", spectrum_recompute)
    check("threshold_recompute", threshold_recompute)
    return {
        "status": "OK" if first_failure is None else "FAIL",
        "checks": checks,
        "first_deviation": first_failure,
    }
