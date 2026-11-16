from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import yaml

from featurelab.app import FeatureEngineeringApp
from featurelab.cache import KeyedCache
from featurelab.config import load_experiment_config
from featurelab.data import ColumnSnapshot, load_dataset
from featurelab.demo import run_sequence, verify_equivalence
from featurelab.encoding import encode_column, resolve_encoding
from featurelab.errors import E_CARDINALITY_EXCEEDED, E_CONFIG_INVALID, E_SNAPSHOT_CORRUPT, FeatureLabError
from featurelab.selector import select_features


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "experiment.yaml"
BUDGET_CONFIG = ROOT / "configs" / "budget_demo.yaml"


def _config_variant(directory: str, mutate) -> Path:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["data_description"] = str((CONFIG.parent / "data.yaml").resolve())
    mutate(raw)
    path = Path(directory) / "experiment.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


class ConfigAndDataTests(unittest.TestCase):
    def test_deterministic_snapshots(self) -> None:
        config = load_experiment_config(CONFIG)
        first = load_dataset(config.data_description, config.target_column)
        second = load_dataset(config.data_description, config.target_column)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.column_order, second.column_order)
        for name in first.column_order:
            self.assertEqual(
                first.columns[name].content_fingerprint,
                second.columns[name].content_fingerprint,
            )
            with self.assertRaises(TypeError):
                first.columns[name].values[0] = "mutated"  # type: ignore[index]

    def test_invalid_configs_return_business_code(self) -> None:
        def unknown_column(raw):
            raw["encoding"]["columns"]["not_a_column"] = "frequency"

        def type_mismatch(raw):
            raw["encoding"]["columns"]["age"] = "one_hot"

        def missing_target(raw):
            raw["target_column"] = "not_in_data"

        def zero_budget(raw):
            raw["latency_budget_ms"] = 0

        for mutate in (unknown_column, type_mismatch, missing_target, zero_budget):
            with self.subTest(mutate=mutate.__name__), tempfile.TemporaryDirectory() as td:
                path = _config_variant(td, mutate)
                with self.assertRaises(FeatureLabError) as raised:
                    load_experiment_config(path)
                self.assertEqual(raised.exception.code, E_CONFIG_INVALID)


class EncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_experiment_config(CONFIG)
        self.dataset = load_dataset(self.config.data_description, self.config.target_column)
        self.plan = self.config.initial_plan

    def test_target_encoding_excludes_own_label(self) -> None:
        snapshot = self.dataset.column("region")
        resolved = resolve_encoding(snapshot, "target", self.plan)
        indices = np.arange(self.dataset.n_rows, dtype=np.int64)
        y = np.asarray(self.dataset.target_values, dtype=np.int64)
        baseline = encode_column(snapshot, resolved, self.plan, indices, indices, y)
        observed_row = next(
            row for row, value in enumerate(snapshot.values) if value is not None
        )
        changed_y = y.copy()
        changed_y[observed_row] = 1 - changed_y[observed_row]
        changed = encode_column(snapshot, resolved, self.plan, indices, indices, changed_y)
        self.assertEqual(
            baseline.matrix[observed_row, 0], changed.matrix[observed_row, 0]
        )
        missing_row = next(
            row for row, value in enumerate(snapshot.values) if value is None
        )
        self.assertEqual(
            baseline.matrix[missing_row, 0],
            self.plan.target_encoding.missing_code,
        )

    def test_high_cardinality_fallback_and_error(self) -> None:
        snapshot = self.dataset.column("campaign_id")
        resolved = resolve_encoding(snapshot, "one_hot", self.plan)
        self.assertTrue(resolved.fallback_used)
        self.assertEqual(resolved.actual_method, "frequency")
        error_plan = replace(self.plan, cardinality_policy="error")
        with self.assertRaises(FeatureLabError) as raised:
            resolve_encoding(snapshot, "one_hot", error_plan)
        self.assertEqual(raised.exception.code, E_CARDINALITY_EXCEEDED)

    def test_one_hot_and_frequency_shapes(self) -> None:
        snapshot = self.dataset.column("channel")
        resolved = resolve_encoding(snapshot, "one_hot", self.plan)
        indices = np.arange(self.dataset.n_rows, dtype=np.int64)
        y = np.asarray(self.dataset.target_values, dtype=np.int64)
        artifact = encode_column(snapshot, resolved, self.plan, indices, indices, y)
        self.assertEqual(artifact.matrix.shape[0], self.dataset.n_rows)
        self.assertEqual(artifact.matrix.shape[1], len(resolved.feature_names))
        self.assertTrue(np.all(np.sum(artifact.matrix, axis=1) <= 1.0))
        frequency = resolve_encoding(snapshot, "frequency", self.plan)
        frequency_artifact = encode_column(
            snapshot, frequency, self.plan, indices, indices, y
        )
        self.assertEqual(frequency_artifact.matrix.shape[1], 1)


class SelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_experiment_config(CONFIG)
        self.params = config.initial_plan.selector

    def test_filters_and_reconstruction(self) -> None:
        matrix = np.asarray(
            [
                [1.0, 1.0, 5.0],
                [2.0, 2.0, 5.0],
                [3.0, 3.0, 5.0],
                [4.0, 4.0, 5.0],
            ]
        )
        names = ("a", "b", "constant")
        target = np.asarray([0.0, 0.0, 1.0, 1.0])
        weights = replace(
            self.params.weight_elimination, enabled=False, max_eliminations=0
        )
        params = replace(
            self.params,
            correlation_threshold=0.95,
            weight_elimination=weights,
        )
        state = select_features(names, matrix, target, params)
        self.assertEqual(state.selected_features, ("a",))
        removed = {item.feature: item.reason for item in state.removed_features}
        self.assertEqual(removed["b"], "correlation")
        self.assertEqual(removed["constant"], "variance")
        rebuilt = select_features(names, matrix, target, params)
        self.assertEqual(state.state_fingerprint, rebuilt.state_fingerprint)

    def test_weight_tie_removes_lexicographically(self) -> None:
        matrix = np.asarray(
            [[1.0, 1.0], [1.0, -1.0], [-1.0, 1.0], [-1.0, -1.0]]
        )
        target = matrix[:, 0] + matrix[:, 1]
        params = replace(
            self.params,
            correlation_threshold=0.99,
            weight_elimination=replace(
                self.params.weight_elimination,
                enabled=True,
                max_eliminations=1,
                min_features=1,
                ridge_alpha=0.0001,
            ),
        )
        state = select_features(("zeta", "alpha"), matrix, target, params)
        self.assertEqual(state.selected_features, ("zeta",))
        weight_removals = [item for item in state.removed_features if item.reason == "weight"]
        self.assertEqual(weight_removals[0].feature, "alpha")


class ApplicationTests(unittest.TestCase):
    def test_selector_cache_value_mismatch_is_invalidated(self) -> None:
        cache = KeyedCache(encode=lambda value: value, decode=lambda value: value)
        cache.get_or_compute("selection", "key", lambda: {"state": "stale"})
        value = cache.get_or_compute_validated(
            "selection",
            "key",
            lambda: {"state": "fresh"},
            lambda candidate: candidate.get("state") == "fresh",
        )
        self.assertEqual(value, {"state": "fresh"})
        self.assertEqual(cache.stats.invalidations, 1)

    def test_incremental_cache_invalidates_only_affected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            try:
                self.assertEqual(app.handle_command("evaluate")["status"], "CONFIRMED")
                before = app.cache.summary()["feature"]["compute_counts"]
                response = app.handle_command("set channel frequency")
                self.assertEqual(response["status"], "CONFIRMED")
                delta = response["cache_delta"]
                self.assertGreater(delta["hits"], 0)
                self.assertEqual(delta["invalidated_feature_columns"], ["channel"])
                after = app.cache.summary()["feature"]["compute_counts"]
                age_before = sum(
                    value for key, value in before.items() if key.startswith("feature:age:")
                )
                age_after = sum(
                    value for key, value in after.items() if key.startswith("feature:age:")
                )
                self.assertEqual(age_before, age_after)
                smoothing = app.handle_command("set-param target.smoothing 4.0")
                self.assertEqual(
                    smoothing["cache_delta"]["invalidated_feature_columns"],
                    ["region", "segment"],
                )
                selector = app.handle_command(
                    "set-param selector.weight_elimination.max_eliminations 3"
                )
                self.assertGreater(len(selector["cache_delta"]["invalidated_feature_columns"]), 5)
            finally:
                app.close()

    def test_budget_exhaustion_has_no_fabricated_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(BUDGET_CONFIG, state_dir=td, resume=False)
            try:
                response = app.handle_command("evaluate")
                self.assertEqual(response["code"], "E_BUDGET_EXHAUSTED")
                self.assertEqual(response["status"], "PARTIAL")
                self.assertIsNone(response["current_plan"]["metrics"])
                self.assertTrue(response["pending_plans"])
                self.assertEqual(
                    response["pending_plans"][0]["code"], "E_BUDGET_EXHAUSTED"
                )
                self.assertTrue(app.wait_for_all(timeout=10))
                status = app.handle_command("status")
                self.assertEqual(status["status"], "CONFIRMED")
                self.assertIsNotNone(status["current_plan"]["metrics"])
                self.assertFalse(status["pending_plans"])
            finally:
                app.close()

    def test_cardinality_error_is_returned_as_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            try:
                self.assertEqual(app.handle_command("evaluate")["status"], "CONFIRMED")
                response = app.handle_command("set-param cardinality_policy error")
                self.assertEqual(response["status"], "FAILED")
                self.assertEqual(response["code"], E_CARDINALITY_EXCEEDED)
                self.assertIsNone(response["current_plan"]["metrics"])
            finally:
                app.close()

    def test_incremental_and_serial_reports_are_equivalent(self) -> None:
        result = verify_equivalence(CONFIG)
        self.assertTrue(result["equivalent"], result.get("differences"))
        self.assertEqual(result["incremental_ranking"], result["serial_ranking"])
        self.assertEqual(result["incremental_best_plan"], result["serial_best_plan"])

    def test_snapshot_recovery_and_corrupt_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            first = app.handle_command("evaluate")
            second = app.handle_command("set channel frequency")
            expected_active = second["current_plan"]["plan_id"]
            version = app.close()
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=True)
            try:
                self.assertTrue(app.recovered)
                self.assertEqual(app.active_plan.plan_id, expected_active)
                self.assertEqual(app.snapshot_version, version)
            finally:
                version = app.close()

            latest = Path(td) / f"snapshot-{version:06d}.json"
            latest.write_text("{ this is not json", encoding="utf-8")
            recovered = FeatureEngineeringApp(CONFIG, state_dir=td, resume=True)
            try:
                report = recovered.build_report()
                self.assertTrue(
                    any(
                        failure.get("code") == E_SNAPSHOT_CORRUPT
                        for failure in report["failures"]
                    )
                )
                self.assertLess(recovered.snapshot_version, version)
                # The prior complete command boundary is still restored.
                self.assertEqual(recovered.active_plan.plan_id, expected_active)
            finally:
                recovered.close()

    def test_incomplete_temporary_snapshot_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            app.handle_command("evaluate")
            app.close()
            (Path(td) / "snapshot-000099.json.abcd.tmp").write_text(
                "incomplete", encoding="utf-8"
            )
            recovered = FeatureEngineeringApp(CONFIG, state_dir=td, resume=True)
            try:
                self.assertTrue(
                    any(
                        failure.get("code") == E_SNAPSHOT_CORRUPT
                        for failure in recovered.failures
                    )
                )
            finally:
                recovered.close()

    def test_interrupted_then_resumed_report_matches_uninterrupted(self) -> None:
        commands = (
            "evaluate",
            "set channel frequency",
            "set-param target.smoothing 4.0",
            "set-param selector.weight_elimination.max_eliminations 5",
        )
        full, _, _ = run_sequence(CONFIG, commands=commands)
        with tempfile.TemporaryDirectory() as td:
            partial = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            for command in commands[:2]:
                partial.handle_command(command)
            partial.close()
            resumed = FeatureEngineeringApp(CONFIG, state_dir=td, resume=True)
            for command in commands[2:]:
                resumed.handle_command(command)
            resumed.wait_for_all()
            resumed.close()
            report = resumed.build_report()
        self.assertEqual(report["semantic_fingerprint"], full["semantic_fingerprint"])

    def test_report_fields_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = FeatureEngineeringApp(CONFIG, state_dir=td, resume=False)
            app.handle_command("evaluate")
            app.wait_for_all()
            report_path = Path(td) / "report.json"
            app.write_report(report_path)
            app.close()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            required = {
                "dataset",
                "plans",
                "ranking",
                "best_plan",
                "commands",
                "cache",
                "snapshot_version",
                "failures",
                "semantic_fingerprint",
            }
            self.assertTrue(required.issubset(report))
            self.assertFalse(any(Path(td).glob("*.tmp")))
            self.assertTrue(report["plans"][0]["fold_metrics"])
            self.assertTrue(report["plans"][0]["selected_features"])
            self.assertTrue(report["plans"][0]["elimination_rounds"])

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics are exercised here")
    def test_sigterm_writes_snapshot_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "signal-report.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "featurelab",
                    "run",
                    "--config",
                    str(CONFIG),
                    "--state-dir",
                    td,
                    "--report",
                    str(report_path),
                ],
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdin is not None
            process.stdin.write("evaluate\n")
            process.stdin.flush()
            time.sleep(0.8)
            process.send_signal(signal.SIGTERM)
            return_code = process.wait(timeout=15)
            self.assertEqual(return_code, 0)
            self.assertTrue(report_path.exists())
            snapshots = list(Path(td).glob("snapshot-*.json"))
            self.assertTrue(snapshots)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
