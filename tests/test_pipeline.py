from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ts_pipeline.config import normalize_config
from ts_pipeline.consistency import verify_report_consistency
from ts_pipeline.errors import (
    ConfigInvalid,
    ErrorSpectrumInvalid,
    InconsistentError,
    RecursionInvalid,
    SnapshotCorrupt,
    SnapshotRollback,
)
from ts_pipeline.evaluation import threshold_decision
from ts_pipeline.pipeline import run_pipeline
from ts_pipeline.precision import quantize_fixed_scalar
from ts_pipeline.scaling import fit_feature_scaler
from ts_pipeline.series import generate_series
from ts_pipeline.snapshots import SnapshotStore
from ts_pipeline.spectrum import validate_error_spectrum
from ts_pipeline.training import train_models
from ts_pipeline.windows import build_windows, ordered_training_rows


def small_config(extra: dict | None = None) -> dict:
    raw = {
        "name": "test-pipeline",
        "seed": 77123,
        "series": {
            "length": 164,
            "target": {
                "trend": 0.02,
                "cycles": [{"amplitude": 1.1, "period": 12.0, "phase": 0.3}],
                "noise_std": 0.04,
                "event_effects": {"promo": 0.5},
            },
            "exogenous": [
                {
                    "name": "load",
                    "seed": 1001,
                    "trend": 0.001,
                    "amplitude": 0.4,
                    "period": 9.0,
                    "phase": 0.2,
                    "noise_std": 0.02,
                }
            ],
            "events": [{"name": "promo", "seed": 2001, "probability": 0.08}],
        },
        "window": {"length": 10, "horizon": 3, "train_fraction": 0.7},
        "features": {
            "lags": [1, 2],
            "rolling": [{"stat": "mean", "window": 3}],
            "differences": [1],
            "include_exogenous": True,
            "include_events": True,
            "standardize": True,
        },
        "model": {
            "family": "linear_autoregressive",
            "epochs": 4,
            "learning_rate": 0.02,
            "l2": 0.0001,
            "confidence_z": 1.96,
            "stump_rounds": 3,
            "stump_learning_rate": 0.1,
            "stump_min_leaf": 3,
        },
        "training": {
            "chunk_size": 7,
            "compensated_sum": True,
            "subnormal_guard": True,
            "reduction_order": "window_then_anchor",
        },
        "precision": {
            "profile": "float64",
            "fixed_scale": 100000,
            "rounding": "nearest_even",
        },
        "evaluation": {
            "threshold": 0.5,
            "tolerance": 0.02,
            "threshold_strategy": "tolerance_band",
            "metrics": ["mae", "rmse", "mape", "smape", "max_abs_error"],
            "confidence_z": 1.96,
            "metric_epsilon": 1e-12,
        },
        "snapshot": {
            "enabled": False,
            "directory": "unused",
            "names": ["window_stats", "precision_config", "error_spectrum"],
            "version_mode": "auto",
            "versions": {},
        },
    }
    if extra:
        _merge(raw, extra)
    return normalize_config(raw)


def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


class DeterministicWindowTests(unittest.TestCase):
    def test_windows_are_byte_reproducible_and_temporally_separated(self) -> None:
        config = small_config()
        series_a = generate_series(config)
        series_b = generate_series(config)
        self.assertEqual(series_a.digest, series_b.digest)
        first = build_windows(series_a, config)
        second = build_windows(series_b, config)
        self.assertEqual(first.collection_digest, second.collection_digest)
        self.assertEqual(
            [window.start_index for window in first.windows],
            sorted(window.start_index for window in first.windows),
        )
        for left, right in zip(first.windows, second.windows):
            self.assertEqual(left.feature_digest, right.feature_digest)
            self.assertEqual(left.target_digest, right.target_digest)
            np.testing.assert_array_equal(left.feature_matrix, right.feature_matrix)
            np.testing.assert_array_equal(
                left.target_vector,
                right.target_vector,
            )
        self.assertLessEqual(
            max(window.target_end for window in first.train_windows),
            min(window.start_index for window in first.test_windows),
        )
        self.assertGreaterEqual(first.separation_gap, 0)

    def test_feature_anchors_and_boundaries_are_exact(self) -> None:
        config = small_config()
        windows = build_windows(generate_series(config), config)
        for window in windows.windows:
            self.assertEqual(
                window.feature_anchor_start,
                window.start_index + config["_feature_lookback"],
            )
            self.assertEqual(
                window.feature_anchor_end,
                window.start_index + config["window"]["length"],
            )
            self.assertEqual(
                window.feature_rows,
                window.feature_anchor_end - window.feature_anchor_start,
            )
            self.assertEqual(
                window.target_end,
                window.target_start + config["window"]["horizon"],
            )


class TrainingTests(unittest.TestCase):
    def _training_inputs(self, config: dict):
        series = generate_series(config)
        windows = build_windows(series, config)
        matrix, targets, _ = ordered_training_rows(windows)
        scaler = fit_feature_scaler(windows, config)
        return scaler.transform(matrix), targets

    def test_chunked_training_is_reproducible_and_compared_per_round(self) -> None:
        config = small_config()
        matrix, targets = self._training_inputs(config)
        from ts_pipeline.precision import Quantizer

        first = train_models(
            matrix,
            targets,
            config,
            feature_quantizer=Quantizer.identity(),
            model_quantizer=Quantizer.identity(),
        )
        second = train_models(
            matrix,
            targets,
            config,
            feature_quantizer=Quantizer.identity(),
            model_quantizer=Quantizer.identity(),
        )
        self.assertEqual(
            first.summary()["reproducibility"]["model_parameter_sha256"],
            second.summary()["reproducibility"]["model_parameter_sha256"],
        )
        self.assertEqual(
            first.summary()["reproducibility"]["rounds_sha256"],
            second.summary()["reproducibility"]["rounds_sha256"],
        )
        self.assertEqual(len(first.comparisons), config["model"]["epochs"])
        for comparison in first.comparisons:
            self.assertTrue(math.isfinite(comparison["loss_absolute_deviation"]))
            self.assertTrue(
                math.isfinite(comparison["parameter_max_abs_deviation"])
            )
            self.assertIn("chunked_loss", comparison)
            self.assertIn("full_batch_loss", comparison)
        self.assertTrue(
            all(round_item["chunks"] for round_item in first.rounds)
        )

    def test_stump_family_is_supported_and_reproducible(self) -> None:
        config = small_config(
            {
                "model": {
                    "family": "stump_gradient_boosting",
                    "stump_rounds": 3,
                }
            }
        )
        matrix, targets = self._training_inputs(config)
        from ts_pipeline.precision import Quantizer

        first = train_models(
            matrix,
            targets,
            config,
            feature_quantizer=Quantizer.identity(),
            model_quantizer=Quantizer.identity(),
        )
        second = train_models(
            matrix,
            targets,
            config,
            feature_quantizer=Quantizer.identity(),
            model_quantizer=Quantizer.identity(),
        )
        self.assertEqual(first.model.family, "stump_gradient_boosting")
        self.assertEqual(
            first.model.parameter_digest(),
            second.model.parameter_digest(),
        )
        self.assertEqual(len(first.rounds), 3)


class PrecisionTests(unittest.TestCase):
    def test_profiles_share_windows_and_fixed_rounding_replays(self) -> None:
        reports = {}
        for profile in ("float64", "float32", "fixed_point"):
            config = small_config(
                {
                    "precision": {
                        "profile": profile,
                        "fixed_scale": 100000,
                        "rounding": "nearest_even",
                    }
                }
            )
            reports[profile] = run_pipeline(config, write_snapshots=False)
        digests = {
            profile: [
                window["feature_digest"]
                for window in report["windows"]["items"]
            ]
            for profile, report in reports.items()
        }
        self.assertEqual(digests["float64"], digests["float32"])
        self.assertEqual(digests["float64"], digests["fixed_point"])
        self.assertEqual(
            reports["float64"]["windows"]["feature_columns"],
            reports["fixed_point"]["windows"]["feature_columns"],
        )
        audit = reports["fixed_point"]["precision"]["quantization_audit"]
        self.assertTrue(audit)
        for record in audit:
            code, replayed = quantize_fixed_scalar(
                record["original"],
                record["scale"],
                record["rounding"],
            )
            self.assertEqual(code, record["integer_code"])
            self.assertEqual(replayed, record["replayed"])
        self.assertNotEqual(
            reports["float64"]["predictions"]["prediction_sha256"],
            reports["fixed_point"]["predictions"]["prediction_sha256"],
        )
        self.assertEqual(reports["float64"]["consistency"]["status"], "OK")
        self.assertEqual(
            reports["float32"]["consistency"]["status"],
            "OK",
        )
        self.assertEqual(
            reports["fixed_point"]["consistency"]["status"],
            "OK",
        )


class ForecastAndSpectrumTests(unittest.TestCase):
    def test_recursive_prediction_does_not_access_future_targets(self) -> None:
        config = small_config()
        report = run_pipeline(config, write_snapshots=False)
        for window in report["predictions"]["windows"]:
            target_start = window["target_bounds"][0]
            for step in window["steps"]:
                self.assertLess(
                    step["max_true_target_index_accessed"],
                    target_start,
                )
                self.assertEqual(step["true_value_use"], "evaluation_only_after_prediction")
                self.assertGreaterEqual(step["interval_upper"], step["interval_lower"])
                self.assertGreaterEqual(step["cumulative_absolute_error"], 0.0)

    def test_invalid_recursion_depth_has_window_and_step(self) -> None:
        config = small_config()
        with self.assertRaises(RecursionInvalid) as caught:
            run_pipeline(
                config,
                write_snapshots=False,
                requested_steps=config["window"]["horizon"] + 1,
            )
        self.assertEqual(caught.exception.code, "E_RECURSION_INVALID")
        self.assertIn("window_index", caught.exception.details)
        self.assertIn("step", caught.exception.details)

    def test_error_spectrum_is_recomputable_and_invalid_values_are_rejected(self) -> None:
        config = small_config(
            {"precision": {"profile": "float32", "fixed_scale": 100000}}
        )
        report = run_pipeline(config, write_snapshots=False)
        spectrum = report["error_spectrum"]
        self.assertIn(
            spectrum["dominant_source"],
            [level["level"] for level in spectrum["levels"]] + ["none"],
        )
        validate_error_spectrum(spectrum)
        broken = copy.deepcopy(spectrum)
        broken["levels"][1]["interval_lower"] = 2.0
        broken["levels"][1]["interval_upper"] = 1.0
        with self.assertRaises(ErrorSpectrumInvalid) as caught:
            validate_error_spectrum(broken)
        self.assertEqual(caught.exception.code, "E_ERROR_SPECTRUM_INVALID")
        self.assertEqual(caught.exception.details["level"], "feature_scaling")
        bad_level = copy.deepcopy(spectrum)
        bad_level["levels"][0]["level"] = "unknown_level"
        with self.assertRaises(ErrorSpectrumInvalid) as caught:
            validate_error_spectrum(bad_level)
        self.assertIn("level", caught.exception.details)

    def test_metrics_and_consistency_can_detect_tampering(self) -> None:
        report = run_pipeline(small_config(), write_snapshots=False)
        self.assertEqual(report["consistency"]["status"], "OK")
        tampered = copy.deepcopy(report)
        tampered["predictions"]["windows"][0]["steps"][0]["prediction"] += 0.25
        with self.assertRaises(InconsistentError) as caught:
            verify_report_consistency(tampered)
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")
        self.assertIn("prediction", caught.exception.details["position"])
        aggregated = verify_report_consistency(tampered, raise_on_error=False)
        self.assertEqual(aggregated["status"], "FAIL")
        self.assertIn("prediction", aggregated["first_deviation"])

        metric_tampered = copy.deepcopy(report)
        metric_tampered["metrics"]["mae"] += 1.0
        with self.assertRaises(InconsistentError) as caught:
            verify_report_consistency(metric_tampered)
        self.assertIn("metrics.mae", caught.exception.details["position"])


class ThresholdAndConfigTests(unittest.TestCase):
    def test_tolerance_band_stabilizes_exact_threshold_across_profiles(self) -> None:
        decisions = []
        for profile in ("float64", "float32", "fixed_point"):
            config = small_config(
                {
                    "precision": {"profile": profile, "fixed_scale": 1000},
                    "evaluation": {
                        "threshold": 0.5,
                        "tolerance": 0.02,
                        "threshold_strategy": "tolerance_band",
                    },
                }
            )
            decisions.append(threshold_decision(0.5, config)["passed"])
        self.assertEqual(decisions, [True, True, True])
        strict = small_config(
            {"evaluation": {"threshold_strategy": "strict"}}
        )
        self.assertFalse(threshold_decision(0.5, strict)["passed"])
        self.assertTrue(threshold_decision(0.5 - 1e-12, strict)["passed"])

    def test_invalid_threshold_and_tolerance_report_exact_field(self) -> None:
        with self.assertRaises(ConfigInvalid) as threshold_error:
            small_config({"evaluation": {"threshold": -0.1}})
        self.assertEqual(threshold_error.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(
            threshold_error.exception.details["field"],
            "evaluation.threshold",
        )
        with self.assertRaises(ConfigInvalid) as tolerance_error:
            small_config({"evaluation": {"tolerance": -0.01}})
        self.assertEqual(
            tolerance_error.exception.details["field"],
            "evaluation.tolerance",
        )


class SnapshotTests(unittest.TestCase):
    def test_whole_replacement_rollback_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(directory)
            first = store.commit("window_stats", {"count": 1, "value": 10.0})
            self.assertEqual(first["version"], 1)
            second = store.commit(
                "window_stats",
                {"count": 2, "value": 20.0, "new_field": True},
            )
            self.assertEqual(second["version"], 2)
            loaded = store.load("window_stats")
            self.assertEqual(
                loaded["payload"],
                {"count": 2, "value": 20.0, "new_field": True},
            )
            with self.assertRaises(SnapshotRollback) as rollback:
                store.commit("window_stats", {"count": 0}, version=1)
            self.assertEqual(rollback.exception.code, "E_SNAPSHOT_ROLLBACK")
            self.assertEqual(rollback.exception.details["snapshot_name"], "window_stats")

            path = store.path_for("window_stats")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["payload"]["count"] = 999
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises(SnapshotCorrupt) as corrupt:
                store.load("window_stats")
            self.assertEqual(corrupt.exception.code, "E_SNAPSHOT_CORRUPT")
            self.assertEqual(corrupt.exception.details["snapshot_name"], "window_stats")
            self.assertEqual(corrupt.exception.details["version"], 2)

    def test_restart_sync_replaces_changed_content_with_next_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_store = SnapshotStore(directory)
            first_store.commit("precision_config", {"profile": "float64"})
            restarted = SnapshotStore(directory)
            unchanged = restarted.sync(
                {"precision_config": {"profile": "float64"}}
            )
            self.assertEqual(unchanged["precision_config"]["version"], 1)
            changed = restarted.sync(
                {"precision_config": {"profile": "float32"}}
            )
            self.assertEqual(changed["precision_config"]["version"], 2)
            self.assertEqual(
                changed["precision_config"]["payload"]["profile"],
                "float32",
            )

    def test_threshold_policy_history_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_config = small_config(
                {
                    "snapshot": {
                        "enabled": True,
                        "directory": directory,
                        "version_mode": "auto",
                    }
                }
            )
            run_pipeline(first_config)
            second_config = small_config(
                {
                    "evaluation": {"threshold_strategy": "strict"},
                    "snapshot": {
                        "enabled": True,
                        "directory": directory,
                        "version_mode": "auto",
                    },
                }
            )
            report = run_pipeline(second_config)
            history = report["threshold_policy"]["history"]
            self.assertGreaterEqual(len(history), 2)
            self.assertEqual(history[-1]["strategy"], "strict")
            self.assertTrue(history[-1]["changed"])


class CliTests(unittest.TestCase):
    def test_cli_writes_complete_report_and_business_error(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            config = small_config()
            config_path = temp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        key: value
                        for key, value in config.items()
                        if not key.startswith("_")
                    }
                ),
                encoding="utf-8",
            )
            output = temp / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "main.py"),
                    "--config",
                    str(config_path),
                    "--output",
                    str(output),
                    "--no-snapshots",
                    "--print-summary",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["business_code"], "OK")
            for key in (
                "windows",
                "training",
                "error_spectrum",
                "predictions",
                "metrics",
            ):
                self.assertIn(key, report)
            self.assertEqual(report["consistency"]["status"], "OK")

            invalid_path = temp / "invalid.json"
            invalid_path.write_text(
                json.dumps(
                    {
                        "evaluation": {"threshold": -1.0},
                        "snapshot": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            error_output = temp / "error.json"
            failed = subprocess.run(
                [
                    sys.executable,
                    str(root / "main.py"),
                    "--config",
                    str(invalid_path),
                    "--output",
                    str(error_output),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(failed.returncode, 2)
            error_report = json.loads(error_output.read_text(encoding="utf-8"))
            self.assertEqual(error_report["business_code"], "E_CONFIG_INVALID")
            self.assertEqual(
                error_report["error"]["field"],
                "evaluation.threshold",
            )


if __name__ == "__main__":
    unittest.main()
