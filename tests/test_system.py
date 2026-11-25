from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drift_ae.autoencoder import AEConfig, DeterministicAutoencoder
from drift_ae.calibration import CalibrationService
from drift_ae.data import generate_windows
from drift_ae.demo import prepare_pipeline, run_demo
from drift_ae.drift import build_baseline, evaluate_drift
from drift_ae.execution_control import BudgetConfig, CalibrationGate, admit_calibration_batch
from drift_ae.features import ScoringConfig, extract_features
from drift_ae.fixtures import build_fixture_bundle
from drift_ae.io_utils import read_json
from drift_ae.preprocessing import NormalizationStats, PreprocessConfig, WindowPreprocessor
from drift_ae.replay import replay_file, replay_record
from drift_ae.snapshots import SnapshotStore
from drift_ae.threshold_formats import (
    downgrade_v2_to_v1,
    read_v1,
    read_v2,
    upgrade_v1_to_v2,
    write_v1,
    write_v2,
)
from drift_ae.thresholds import (
    GLOBAL_SEGMENT,
    ThresholdModel,
    ThresholdRow,
    adjudicate,
    build_rows_from_samples,
)


class DeterministicChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.windows = generate_windows(n_windows=10)
        self.stats = NormalizationStats.from_windows([w.values for w in self.windows[:6]])

    def test_reconstruction_and_latent_are_byte_identical(self) -> None:
        config = PreprocessConfig(seed=406)
        pre = WindowPreprocessor(config, stats=self.stats)
        ae_config = AEConfig(seed=77, epochs=0)
        window = self.windows[1]

        first_pre = pre.process(window.window_id, window.values)
        first_ae = DeterministicAutoencoder(128, ae_config)
        first_out = first_ae.forward(first_pre.completed)
        first_features = extract_features(first_pre, first_out.reconstruction)

        second_pre = WindowPreprocessor(PreprocessConfig(seed=406), stats=self.stats).process(
            window.window_id, window.values
        )
        second_ae = DeterministicAutoencoder(128, AEConfig(seed=77, epochs=0))
        second_out = second_ae.forward(second_pre.completed)
        second_features = extract_features(second_pre, second_out.reconstruction)

        self.assertEqual(first_out.reconstruction.tobytes(), second_out.reconstruction.tobytes())
        self.assertEqual(first_out.latent.tobytes(), second_out.latent.tobytes())
        self.assertEqual(first_features.checksum, second_features.checksum)
        self.assertEqual(first_ae.checksum, second_ae.checksum)

    def test_mask_boundaries_are_traceable_and_decay_contributions(self) -> None:
        pre = WindowPreprocessor(PreprocessConfig(seed=406), stats=self.stats)
        window = self.windows[3]
        result = pre.process(window.window_id, window.values)
        ae = DeterministicAutoencoder(128, AEConfig(epochs=0))
        features = extract_features(result, ae.forward(result.completed).reconstruction)

        self.assertTrue(result.traces)
        self.assertTrue(all(item.window_id == window.window_id for item in result.traces))
        self.assertTrue(all(0 <= item.dimension < 128 for item in result.traces))
        self.assertTrue(any(abs(item.delta) > 0.0 for item in result.traces))
        self.assertTrue(features.boundary_traces)
        for trace in features.boundary_traces:
            self.assertEqual(trace["window_id"], window.window_id)
            self.assertIn("dimension", trace)
            self.assertIn("left_dimension", trace)
            self.assertIn("right_dimension", trace)

        masked_dimensions = np.flatnonzero(result.mask)
        self.assertTrue(masked_dimensions.size)
        np.testing.assert_allclose(result.contribution_weights[masked_dimensions], 0.25)

    def test_backward_pass_matches_numeric_gradient(self) -> None:
        ae = DeterministicAutoencoder(
            6, AEConfig(seed=9, hidden_dim=5, latent_dim=3, gradient_clip=0.0)
        )
        x = np.random.default_rng(0).normal(size=(2, 6))
        output = ae.forward(x)
        grads = ae.backward(x, output)
        epsilon = 1e-6
        for name in ("W1", "b1", "W4", "b4"):
            param = ae.weights[name]
            index = (0, 0) if name.startswith("W") else (0,)
            old = float(param[index])
            param[index] = old + epsilon
            plus = float(np.mean((ae.forward(x).reconstruction - x) ** 2))
            param[index] = old - epsilon
            minus = float(np.mean((ae.forward(x).reconstruction - x) ** 2))
            param[index] = old
            numeric = (plus - minus) / (2 * epsilon)
            self.assertAlmostEqual(numeric, float(grads[name][index]), places=7)


class ThresholdMigrationTests(unittest.TestCase):
    def _model(self) -> ThresholdModel:
        rows = build_rows_from_samples(
            {
                GLOBAL_SEGMENT: [0.1, 0.2, 0.3, 0.4, 0.8, 1.2, 1.5, 2.0],
                "stream-0": [0.1, 0.2, 0.25, 0.3, 0.7, 0.9, 1.1, 1.4],
            }
        )
        model = ThresholdModel(model_version=4, rows=rows, tolerance_ratio=0.05)
        model.recompute_checksum()
        return model

    def test_v1_v2_roundtrip_preserves_decisions(self) -> None:
        model = self._model()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v1_path = root / "thresholds-v1.json"
            v2_dir = root / "thresholds-v2"
            write_v1(v1_path, model)
            upgraded = upgrade_v1_to_v2(v1_path, v2_dir, known_segments=["stream-1"])
            self.assertEqual(upgraded.row("stream-1").inherited_from, GLOBAL_SEGMENT)
            self.assertEqual(upgraded.row("stream-1").granularity, "window_segment")

            v2_model = read_v2(v2_dir)
            v1_model, _ = read_v1(v1_path)
            scores = [0.15, 0.4, 0.9, 1.1, 1.35, 1.8]
            for score in scores:
                original = adjudicate(score, "stream-0", model)
                from_v2 = adjudicate(score, "stream-0", v2_model)
                from_v1 = adjudicate(score, "stream-0", v1_model)
                self.assertEqual(original["is_anomaly"], from_v2["is_anomaly"])
                self.assertEqual(original["severity"], from_v2["severity"])
                self.assertEqual(from_v1["is_anomaly"], from_v2["is_anomaly"])

            raw_v1 = json.loads(v1_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_v1["format"], "ae-threshold-v1")
            self.assertTrue(all(isinstance(value, (int, float)) for value in raw_v1["quantiles"].values()))
            self.assertTrue((v2_dir / "thresholds.sqlite").exists())
            self.assertTrue(list(v2_dir.glob("shard_*.json")))

    def test_downgrade_records_and_discards_layered_rows(self) -> None:
        model = self._model()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v1_path = root / "v1.json"
            v2_dir = root / "v2"
            downgraded_path = root / "downgraded-v1.json"
            write_v1(v1_path, model)
            upgrade_v1_to_v2(v1_path, v2_dir, known_segments=["stream-0"])
            v2_model = read_v2(v2_dir)
            layered = ThresholdRow(
                segment_id="stream-0",
                tier="sensor_layer",
                granularity="sensor_block",
                quantiles={"p95": 9.0, "p99": 10.0},
                condition={"sensor_block": "B"},
            )
            layered.recompute_checksum()
            v2_model.rows.append(layered)
            write_v2(v2_dir, v2_model)

            _, records = downgrade_v2_to_v1(v2_dir, downgraded_path)
            self.assertTrue(any(item["action"] == "drop_layered_condition" for item in records))
            downgraded, sources = read_v1(downgraded_path)
            self.assertTrue(sources)
            for score in [0.2, 0.8, 1.2, 1.7]:
                self.assertEqual(
                    adjudicate(score, "stream-0", model)["is_anomaly"],
                    adjudicate(score, "stream-0", downgraded)["is_anomaly"],
                )


class CalibrationBudgetAndConcurrencyTests(unittest.TestCase):
    def _feature_index(self) -> dict:
        index = {}
        for segment in ("stream-0", "stream-1", "stream-2"):
            prefix = {"stream-0": 0.2, "stream-1": 0.5, "stream-2": 0.9}[segment]
            for i in range(12):
                window_id = f"{segment}-w{i:02d}"
                index[window_id] = {
                    "window_id": window_id,
                    "segment_id": segment,
                    "score": prefix + i * 0.03,
                }
        return index

    def test_local_calibration_reuses_unaffected_segments(self) -> None:
        feature_index = self._feature_index()
        rows = build_rows_from_samples(
            {
                segment: [item["score"] for item in feature_index.values() if item["segment_id"] == segment]
                for segment in ("stream-0", "stream-1", "stream-2")
            }
        )
        model = ThresholdModel(model_version=1, rows=rows)
        model.recompute_checksum()
        old_stream_one = model.row("stream-1").checksum
        old_stream_two = model.row("stream-2").checksum

        with tempfile.TemporaryDirectory() as tmp:
            service = CalibrationService(Path(tmp))
            outcome = service.submit(
                model,
                {
                    "reason": "false_positive_fix",
                    "feature_index": feature_index,
                    "labels": {window_id: 0 for window_id in feature_index},
                    "window_updates": [
                        {"window_id": "stream-0-w00", "corrected_label": "anomaly"}
                    ],
                    "input_dim": 128,
                },
            )
            self.assertTrue(outcome["ok"], outcome)
            propagation = outcome["propagation"]
            self.assertIn("stream-0", propagation["recompute"]["segments"])
            self.assertIn(GLOBAL_SEGMENT, propagation["recompute"]["segments"])
            self.assertEqual(propagation["reuse"]["segments"], ["stream-1", "stream-2"])
            self.assertIn("stream-1", propagation["reuse"]["segments"])
            new_model = outcome["model"]
            self.assertEqual(new_model.row("stream-1").checksum, old_stream_one)
            self.assertEqual(new_model.row("stream-2").checksum, old_stream_two)
            self.assertEqual(new_model.model_version, 2)

    def test_budget_degradation_is_recorded(self) -> None:
        decision = admit_calibration_batch(
            [f"w{i}" for i in range(40)],
            input_dim=128,
            config=BudgetConfig(
                max_reconstruction_comparisons=10_000_000,
                max_resident_feature_bytes=12_000,
            ),
            pinned_window_ids=["w7"],
        )
        self.assertTrue(decision.admitted)
        self.assertTrue(decision.degraded)
        self.assertLess(decision.admitted_windows, 40)
        self.assertIn("w7", decision.selected_window_ids)
        self.assertTrue(decision.decision_basis)

    def test_second_commit_returns_stage_busy(self) -> None:
        feature_index = self._feature_index()
        rows = build_rows_from_samples(
            {
                segment: [item["score"] for item in feature_index.values() if item["segment_id"] == segment]
                for segment in ("stream-0", "stream-1", "stream-2")
            }
        )
        model = ThresholdModel(model_version=1, rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            gate = CalibrationGate(state)
            service = CalibrationService(state, gate=gate)
            self.assertTrue(gate.try_acquire())
            try:
                outcome = service.submit(
                    model,
                    {
                        "reason": "concurrent_test",
                        "feature_index": feature_index,
                        "input_dim": 128,
                    },
                )
                self.assertFalse(outcome["ok"])
                self.assertEqual(outcome["error_code"], "E_STAGE_BUSY")
            finally:
                gate.release()


class DriftAndAdjudicationTests(unittest.TestCase):
    def test_drift_alert_attributes_modules_and_windows(self) -> None:
        baseline = []
        recent = []
        for i in range(12):
            baseline.append(
                {
                    "window_id": f"b{i}",
                    "segment_id": "stream-1",
                    "metrics": {
                        "score": 0.5 + 0.02 * (i % 3),
                        "mean_residual": 0.3 + 0.01 * (i % 2),
                        "mask_consistency": 0.95,
                    },
                }
            )
        for i in range(4):
            recent.append(
                {
                    "window_id": f"r{i}",
                    "window_index": 70 + i,
                    "segment_id": "stream-1",
                    "metrics": {
                        "score": 3.0 + 0.1 * i,
                        "mean_residual": 1.8 + 0.1 * i,
                        "mask_consistency": 0.6,
                    },
                }
            )
        result = evaluate_drift(recent, build_baseline(baseline))
        self.assertTrue(result["alert"])
        self.assertEqual(result["affected_segments"], ["stream-1"])
        self.assertEqual(result["time_window"], [70, 73])
        self.assertIn("autoencoder", result["module_set"])
        self.assertIn("threshold_model", result["module_set"])

    def test_strict_and_tolerance_strategies_are_deterministic(self) -> None:
        row = ThresholdRow(
            segment_id="s",
            quantiles={"p90": 0.8, "p95": 0.9, "p99": 1.0},
        )
        row.recompute_checksum()
        model = ThresholdModel(model_version=9, rows=[row], tolerance_ratio=0.02)
        strict_first = adjudicate(1.0, "s", model, strategy="strict")
        strict_second = adjudicate(1.0, "s", model, strategy="strict")
        self.assertEqual(strict_first, strict_second)
        self.assertTrue(strict_first["is_anomaly"])

        tolerance = adjudicate(1.005, "s", model, strategy="tolerance_band")
        self.assertTrue(tolerance["in_tolerance_band"])
        self.assertEqual(tolerance["severity"], "boundary")
        above = adjudicate(1.03, "s", model, strategy="tolerance_band")
        self.assertTrue(above["is_anomaly"])


class SnapshotTests(unittest.TestCase):
    def test_monotonic_snapshot_and_rollback_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp))
            first = store.commit("plan", {"cursor": 0})
            second = store.commit("plan", {"cursor": 8})
            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)

            rollback = store.attempt_rollback("plan", target_version=1)
            self.assertFalse(rollback["ok"])
            self.assertEqual(rollback["error_code"], "E_SNAPSHOT_ROLLBACK")

            old_envelope = {
                "kind": "plan",
                "version": 1,
                "payload": {"cursor": 0},
            }
            old_envelope["checksum"] = SnapshotStore.checksum_for(
                "plan", old_envelope["version"], old_envelope["payload"]
            )
            rejected = store.replace_if_changed(old_envelope)
            self.assertEqual(rejected["error_code"], "E_SNAPSHOT_ROLLBACK")
            self.assertEqual(store.load("plan")["version"], 2)


class ReplayAndRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.fixtures = cls.root / "fixtures"
        build_fixture_bundle(
            cls.fixtures,
            n_windows=24,
            baseline_count=16,
            train_epochs=80,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_demo_records_replay(self) -> None:
        state = self.root / "replay-state"
        summary = run_demo(
            state_dir=state,
            fixtures_dir=self.fixtures,
            fresh=True,
            batch_size=6,
        )
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["replay"]["mismatch_count"], 0)
        replay = replay_file(state / "diagnostics.jsonl")
        self.assertTrue(replay["ok"])
        for result in replay["results"]:
            self.assertIn("threshold_model_version", result)

        self.assertGreater(replay["record_count"], 24)

    def test_batch_append_reuses_committed_batches(self) -> None:
        state = self.root / "append-state"
        pipeline = prepare_pipeline(state, self.fixtures, fresh=True, batch_size=6)
        windows = read_json(self.fixtures / "sample_windows.json")

        first = pipeline.run_pending(windows[:12])
        self.assertEqual(len(first["processed"]), 12)

        second = pipeline.run_pending(windows[12:18], append=True)
        self.assertEqual(second["processed"], ["w0012", "w0013", "w0014", "w0015", "w0016", "w0017"])
        self.assertEqual(len(second["reused"]), 12)
        self.assertEqual(pipeline.snapshots.load("plan")["payload"]["window_ids"], [w["window_id"] for w in windows[:18]])

    def test_sigkill_recovery_matches_baseline(self) -> None:
        example = ROOT / "examples" / "end_to_end_demo.py"
        crash_state = self.root / "crash-state"
        baseline_state = self.root / "baseline-state"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        crash = subprocess.run(
            [
                sys.executable,
                str(example),
                "--fixtures-dir",
                str(self.fixtures),
                "--state-dir",
                str(crash_state),
                "--fresh",
                "--batch-size",
                "6",
                "--crash-after",
                "2",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(crash.returncode, -9, crash.stdout + crash.stderr)

        resume = subprocess.run(
            [
                sys.executable,
                str(example),
                "--fixtures-dir",
                str(self.fixtures),
                "--state-dir",
                str(crash_state),
                "--batch-size",
                "6",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)

        baseline = subprocess.run(
            [
                sys.executable,
                str(example),
                "--fixtures-dir",
                str(self.fixtures),
                "--state-dir",
                str(baseline_state),
                "--fresh",
                "--batch-size",
                "6",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

        recovered_summary = json.loads((crash_state / "demo_summary.json").read_text())
        self.assertEqual(recovered_summary["run"]["reused_count"], 12)
        self.assertEqual(recovered_summary["run"]["processed_count"], 12)
        self.assertTrue(recovered_summary["replay"]["ok"])

        recovered = json.loads((crash_state / "snapshots" / "result" / "current.json").read_text())["payload"]
        expected = json.loads((baseline_state / "snapshots" / "result" / "current.json").read_text())["payload"]
        self.assertEqual(recovered["decisions"], expected["decisions"])
        recovered_checksums = [record["record_checksum"] for record in recovered["records"]]
        expected_checksums = [record["record_checksum"] for record in expected["records"]]
        self.assertEqual(recovered_checksums, expected_checksums)


if __name__ == "__main__":
    unittest.main()
