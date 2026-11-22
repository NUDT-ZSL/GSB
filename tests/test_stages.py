from __future__ import annotations

import json
from pathlib import Path

from motion_pipeline.config import PipelineConfig
from motion_pipeline.dataset import load_dataset
from motion_pipeline.deterministic import canonical_bytes, content_hash
from motion_pipeline.stages import (
    aggregate,
    encode_motion,
    extrapolate,
    recompute_clip_metrics,
    recompute_overall_metrics,
    sample_frames,
)
from motion_pipeline.verification import verify_checkpoint

from tests.helpers import DatasetTestCase


class StageDeterminismTests(DatasetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = PipelineConfig()
        self.loaded = load_dataset(self.manifest, self.frames, self.config)
        self.sampled = sample_frames(self.loaded, self.config)
        self.encoded = encode_motion(self.loaded, self.sampled, self.config)
        self.extrapolated = extrapolate(
            self.loaded, self.sampled, self.encoded, self.config
        )
        self.aggregated = aggregate(
            self.loaded,
            self.sampled,
            self.encoded,
            self.extrapolated,
            self.config,
        )

    def test_frame_sampling_is_deterministic_and_covers_all_frames(self) -> None:
        second = sample_frames(self.loaded, self.config)
        self.assertEqual(
            canonical_bytes(self.sampled), canonical_bytes(second)
        )
        for clip_id in self.loaded["clip_ids"]:
            record = self.sampled["clips"][clip_id]
            observed = set(record["observed_indices"])
            forecast = set(record["forecast_indices"])
            self.assertEqual(observed & forecast, set())
            self.assertEqual(
                observed | forecast, set(range(record["frame_count"]))
            )
            for pair in record["pairs"]:
                self.assertLessEqual(pair["from_frame_index"], pair["to_frame_index"])
                self.assertEqual(
                    pair["step_ms"],
                    pair["step_frames"] * record["frame_interval_ms"],
                )
            self.assertEqual(
                record["pairs"],
                sorted(
                    record["pairs"],
                    key=lambda item: (
                        item["from_frame_index"],
                        item["to_frame_index"],
                        item["step_frames"],
                    ),
                ),
            )

    def test_motion_features_are_deterministic_and_complete(self) -> None:
        second = encode_motion(self.loaded, self.sampled, self.config)
        self.assertEqual(
            canonical_bytes(self.encoded), canonical_bytes(second)
        )
        for clip_id in self.loaded["clip_ids"]:
            objects = self.encoded["clips"][clip_id]["objects"]
            self.assertEqual(
                len(objects), self.loaded["spec_by_clip"][clip_id]["object_count"]
            )
            for object_record in objects.values():
                self.assertGreater(object_record["samples"], [])
                for sample in object_record["samples"]:
                    self.assertEqual(len(sample["displacement"]), 2)
                    self.assertEqual(len(sample["acceleration"]), 2)
                    self.assertGreaterEqual(sample["confidence"], 0.0)
                    self.assertLessEqual(sample["confidence"], 1.0)
                    self.assertGreaterEqual(sample["speed"], 0.0)

    def test_recursive_history_state_is_explicit(self) -> None:
        second = extrapolate(
            self.loaded, self.sampled, self.encoded, self.config
        )
        self.assertEqual(
            canonical_bytes(self.extrapolated), canonical_bytes(second)
        )
        for clip_id in self.loaded["clip_ids"]:
            record = self.extrapolated["clips"][clip_id]
            expected = self.sampled["clips"][clip_id]["forecast_indices"]
            self.assertEqual(
                [frame["frame_index"] for frame in record["prediction_frames"]],
                expected,
            )
            for frame in record["prediction_frames"]:
                state = frame["state"]
                self.assertIsInstance(
                    state["input_history_summary_hash"], str
                )
                self.assertIsInstance(
                    state["output_history_summary_hash"], str
                )
                self.assertEqual(
                    state["consumed_frame_index"], frame["frame_index"]
                )
            self.assertEqual(
                record["final_state"]["consumed_frame_index"], expected[-1]
            )

    def test_metrics_can_be_recomputed_from_per_frame_details(self) -> None:
        for clip_id, record in self.aggregated["clips"].items():
            self.assertEqual(
                recompute_clip_metrics(record["trajectory_hits"]),
                record["trajectory_stats"],
            )
        self.assertEqual(
            recompute_overall_metrics(self.aggregated["clips"]),
            self.aggregated["overall_metrics"],
        )

    def test_full_pipeline_cross_stage_verification(self) -> None:
        outcome = self.run_pipeline(self.root / "run")
        self.assertEqual(outcome.status, "completed")
        verification = verify_checkpoint(outcome.checkpoint)
        self.assertTrue(verification["passed"])
        self.assertTrue(outcome.checkpoint["version_history"])
        self.assertEqual(
            [item["version"] for item in outcome.checkpoint["version_history"]],
            [1, 2, 3, 4],
        )
        final_path = self.root / "run" / "final" / "result.json"
        self.assertTrue(final_path.exists())
        self.assertEqual(final_path.read_bytes(), outcome.final_result_bytes)

    def test_full_run_is_byte_stable_across_reset(self) -> None:
        first = self.run_pipeline(self.root / "one")
        second = self.run_pipeline(self.root / "two")
        self.assertEqual(first.final_result_bytes, second.final_result_bytes)
        self.assertEqual(
            (self.root / "one" / "snapshots" / "checkpoint.json").read_bytes(),
            (self.root / "two" / "snapshots" / "checkpoint.json").read_bytes(),
        )
        self.assertEqual(
            first.checkpoint["version_history"],
            second.checkpoint["version_history"],
        )
