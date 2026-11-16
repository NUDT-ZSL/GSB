from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from regularization_lab.config import load_experiment
from regularization_lab.lab import ExperimentSession

from tests.helpers import ROOT, result_core


def small_config(config, root: Path, *, concurrency: int = 4, name: str = "state"):
    return replace(
        config,
        optimizer=replace(
            config.optimizer,
            max_iterations=6,
            tolerance=0.0,
            batch_size=32,
        ),
        runtime=replace(
            config.runtime,
            concurrency=concurrency,
            state_dir=root / name,
            report_path=root / f"{name}.json",
            round_delay_seconds=0.0,
        ),
    )


class TrainingConsistencyTests(unittest.TestCase):
    def test_serial_and_threaded_reports_are_exactly_equal(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            serial_session = ExperimentSession(small_config(base, root, concurrency=1, name="serial"))
            concurrent_session = ExperimentSession(small_config(base, root, concurrency=4, name="concurrent"))
            serial_results = serial_session.run_all()
            concurrent_results = concurrent_session.run_all()
            serial_report = serial_session.write_report()
            concurrent_report = concurrent_session.write_report()
            self.assertEqual(serial_report, concurrent_report)
            for serial, concurrent in zip(serial_results, concurrent_results):
                self.assertEqual(result_core(serial), result_core(concurrent))
                self.assertEqual(serial["failures"], concurrent["failures"])
            self.assertEqual(
                {result["regularization"]["type"] for result in serial_results},
                {"l2", "l1", "elastic_net"},
            )

    def test_resume_after_snapshot_is_field_for_field_identical(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline_config = small_config(base, root, name="baseline")
            baseline = ExperimentSession(baseline_config)
            baseline_result = baseline.run_scheme("ridge")
            assert baseline_result is not None

            paused_config = small_config(base, root, name="paused")
            paused_session = ExperimentSession(paused_config)
            paused = paused_session.run_scheme("ridge", pause_after_round=4)
            self.assertIsNotNone(paused)
            assert paused is not None
            self.assertEqual(paused["status"], "interrupted")
            self.assertEqual(paused["snapshot_version"], 4)

            restarted = ExperimentSession(paused_config)
            resumed = restarted.run_scheme("ridge")
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed["resumed_from_round"], 4)
            self.assertEqual(result_core(resumed), result_core(baseline_result))
            self.assertEqual(resumed["failures"], [])

    def test_cache_replay_does_not_execute_training_or_write_new_snapshot(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = small_config(base, root, name="cache")
            config = replace(
                config,
                optimizer=replace(config.optimizer, tolerance=1.0e9),
            )
            first = ExperimentSession(config)
            first_result = first.run_scheme("ridge")
            assert first_result is not None
            self.assertTrue(first_result["cache_decision"]["cache_written"])
            snapshot_files_before = len(list((root / "cache" / "snapshots").glob("*/*.json")))

            replay = ExperimentSession(config)
            replay_result = replay.run_scheme("ridge")
            assert replay_result is not None
            self.assertTrue(replay_result["cache_reused"])
            self.assertEqual(replay_result["training_executed_rounds"], [])
            self.assertEqual(replay_result["cache_decision"]["recomputed_rounds"], [])
            self.assertEqual(result_core(replay_result), result_core(first_result))
            snapshot_files_after = len(list((root / "cache" / "snapshots").glob("*/*.json")))
            self.assertEqual(snapshot_files_after, snapshot_files_before)

    def test_batch_and_update_failures_are_isolated_and_ordered(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = small_config(base, root, name="batch-failure")
            config = replace(
                config,
                optimizer=replace(config.optimizer, max_iterations=2),
                regularizers=[base.regularizers[0]],
            )
            with patch("regularization_lab.trainer._batch_worker", side_effect=RuntimeError("boom")):
                session = ExperimentSession(config)
                result = session.run_scheme("ridge")
            assert result is not None
            self.assertEqual(result["status"], "complete")
            failures = result["failures"]
            self.assertTrue(failures)
            self.assertTrue(all(item["business_code"] == "E_BATCH_FAILURE" for item in failures))
            self.assertTrue(all(item["exception_type"] == "RuntimeError" for item in failures))
            order = [(item["round"], item["batch_index"]) for item in failures]
            self.assertEqual(order, sorted(order))

            update_config = small_config(base, root, name="update-failure")
            update_config = replace(
                update_config,
                optimizer=replace(update_config.optimizer, max_iterations=2),
                regularizers=[base.regularizers[0]],
            )
            with patch(
                "regularization_lab.trainer.evaluate_regularization",
                side_effect=RuntimeError("objective boom"),
            ):
                update_session = ExperimentSession(update_config)
                update_result = update_session.run_scheme("ridge")
            assert update_result is not None
            self.assertEqual(update_result["status"], "complete")
            self.assertTrue(
                all(item["business_code"] == "E_UPDATE_FAILURE" for item in update_result["failures"])
            )

    def test_snapshot_write_failure_does_not_kill_training(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = small_config(base, root, name="snapshot-failure")
            config = replace(
                config,
                optimizer=replace(config.optimizer, max_iterations=2),
                regularizers=[base.regularizers[0]],
            )
            with patch(
                "regularization_lab.snapshot.SnapshotStore.write_snapshot",
                side_effect=OSError("disk unavailable"),
            ):
                session = ExperimentSession(config)
                result = session.run_scheme("ridge")
            assert result is not None
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["snapshot_version"], 0)
            self.assertTrue(
                all(item["business_code"] == "E_SNAPSHOT_FAILURE" for item in result["failures"])
            )


if __name__ == "__main__":
    unittest.main()
