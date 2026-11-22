from __future__ import annotations

import copy

from motion_pipeline.config import PipelineConfig
from motion_pipeline.errors import BusinessError
from motion_pipeline.snapshots import SnapshotStore, resign_snapshot
from motion_pipeline.verification import compare_recovery

from tests.helpers import DatasetTestCase


class Crash(BaseException):
    pass


class RecoveryTests(DatasetTestCase):
    def test_forced_kill_and_resume_at_every_stage_matches_baseline(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        baseline_checkpoint_bytes = (
            self.root / "baseline" / "snapshots" / "checkpoint.json"
        ).read_bytes()
        for kill_stage in (1, 2, 3, 4):
            with self.subTest(kill_stage=kill_stage):
                workdir = self.root / f"crash-{kill_stage}"

                def crash_hook(stage_index, _checkpoint, target=kill_stage):
                    if stage_index + 1 == target:
                        raise Crash()

                with self.assertRaises(Crash):
                    self.run_pipeline(workdir, on_stage_committed=crash_hook)
                partial = SnapshotStore(workdir).load()
                self.assertIsNotNone(partial)
                self.assertEqual(partial["version"], kill_stage)
                recovered = self.run_pipeline(workdir)
                self.assertEqual(recovered.status, "completed")
                reused_stages = [item["stage"] for item in recovered.reused]
                recomputed_stages = [item["stage"] for item in recovered.recomputed]
                expected_reused = [
                    stage
                    for stage in (
                        "frame_sample",
                        "motion_encoding",
                        "temporal_extrapolation",
                        "result_aggregation",
                    )
                    if (
                        (
                            "frame_sample",
                            "motion_encoding",
                            "temporal_extrapolation",
                            "result_aggregation",
                        ).index(stage)
                        < kill_stage
                    )
                ]
                expected_recomputed = [
                    stage
                    for stage in (
                        "frame_sample",
                        "motion_encoding",
                        "temporal_extrapolation",
                        "result_aggregation",
                    )
                    if (
                        (
                            "frame_sample",
                            "motion_encoding",
                            "temporal_extrapolation",
                            "result_aggregation",
                        ).index(stage)
                        >= kill_stage
                    )
                ]
                self.assertEqual(reused_stages, expected_reused)
                self.assertEqual(recomputed_stages, expected_recomputed)
                self.assertEqual(
                    recovered.final_result_bytes, baseline.final_result_bytes
                )
                self.assertEqual(
                    (workdir / "snapshots" / "checkpoint.json").read_bytes(),
                    baseline_checkpoint_bytes,
                )
                comparison = compare_recovery(
                    baseline.checkpoint,
                    recovered.checkpoint,
                    reference_final_bytes=baseline.final_result_bytes,
                    recovered_final_bytes=recovered.final_result_bytes,
                )
                self.assertTrue(comparison["equal"])
                self.assertTrue(comparison["final_bytes_equal"])

    def test_recovery_mismatch_reports_first_divergent_stage(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        drifted = copy.deepcopy(baseline.checkpoint)
        drifted["stages"][0]["payload"]["drift_marker"] = "corrupt"
        resign_snapshot(drifted)
        with self.assertRaises(BusinessError) as caught:
            compare_recovery(
                baseline.checkpoint,
                drifted,
                reference_final_bytes=baseline.final_result_bytes,
                recovered_final_bytes=baseline.final_result_bytes,
            )
        self.assertEqual(caught.exception.code, "E_RECOVERY_MISMATCH")
        self.assertEqual(caught.exception.stage, "frame_sample")

    def test_completed_checkpoint_rejects_model_config_drift(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        with self.assertRaises(BusinessError) as caught:
            self.run_pipeline(
                self.root / "baseline",
                PipelineConfig(hit_distance_px=3.25),
            )
        self.assertEqual(caught.exception.code, "E_RECOVERY_MISMATCH")
        self.assertEqual(caught.exception.stage, "result_aggregation")
