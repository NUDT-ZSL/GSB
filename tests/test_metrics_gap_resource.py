from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from tracker_app.data_gen import generate_dataset
from tracker_app.engine import TrackingEngine
from tracker_app.errors import TrackingError
from tracker_app.gaps import complete_gaps
from tracker_app.metrics import verify_metric_recompute
from tracker_app.models import Track

from tests.helpers import small_config


class MetricsGapResourceTests(unittest.TestCase):
    def test_metrics_recompute_and_completion_accounting(self) -> None:
        config = small_config()
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=Path(temp) / "runtime",
                output_dir=Path(temp) / "output",
            )
            result = engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            sequence = result["sequence_results"][0]
            metrics = sequence.metrics
            self.assertEqual(
                metrics["mota"],
                round(
                    1.0
                    - (
                        metrics["false_negatives"]
                        + metrics["false_positives"]
                        + metrics["id_switches"]
                    )
                    / metrics["ground_truth_count"],
                    10,
                ),
            )
            self.assertGreater(metrics["completion"]["completed_hits"], 0)
            real_keys = {
                (row["pred_track_id"], row["frame_index"])
                for row in sequence.assignments
            }
            completion_keys = {
                (row["pred_track_id"], row["frame_index"])
                for row in sequence.completion_rows
            }
            self.assertFalse(real_keys & completion_keys)
            verify_metric_recompute(
                metrics,
                assignments=sequence.assignments,
                truth_by_frame=result["preprocessed"].sequences[sequence.seq_id].truth_frames,
                completion_rows=sequence.completion_rows,
                gap_reports=[],
                segment_rows=sequence.segments,
            )

    def test_overlapping_gap_plan_returns_e_gap_invalid(self) -> None:
        config = small_config()
        config["gap"]["manual_completion_plans"] = [
            {"seq_id": "s", "track_id": 1, "gap_start": 2, "gap_end": 3},
            {"seq_id": "s", "track_id": 1, "gap_start": 3, "gap_end": 4},
        ]
        observations = [
            {
                "frame_index": 0,
                "pred_track_id": 1,
                "track_id_true": 0,
                "bbox": [10.0, 10.0, 30.0, 50.0],
                "score": 0.9,
                "class_id": 0,
            },
            {
                "frame_index": 10,
                "pred_track_id": 1,
                "track_id_true": 0,
                "bbox": [50.0, 10.0, 70.0, 50.0],
                "score": 0.9,
                "class_id": 0,
            },
        ]
        track = Track(
            track_id=1,
            state="tracked",
            birth_frame=0,
            last_frame=10,
            last_bbox=(50.0, 10.0, 70.0, 50.0),
            appearance=(0.1, 0.2, 0.3),
            velocity=(4.0, 0.0, 0.0, 0.0),
        )
        with self.assertRaises(TrackingError) as raised:
            complete_gaps(
                seq_id="s",
                revision=1,
                assignments=observations,
                final_tracks={1: track},
                config=config,
                frame_range=(0, 10),
            )
        self.assertEqual(raised.exception.code, "E_GAP_INVALID")
        self.assertEqual(raised.exception.seq_id, "s")
        self.assertEqual(raised.exception.category, "overlapping_intervals")

    def test_resource_policy_error_and_graceful_degrade(self) -> None:
        config = small_config()
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            strict = copy.deepcopy(config)
            strict["resources"]["policy"] = "error"
            strict["resources"]["max_detections_per_frame"] = 1
            strict_engine = TrackingEngine(
                strict,
                base_dir=temp,
                runtime_dir=Path(temp) / "runtime_strict",
                output_dir=Path(temp) / "output_strict",
            )
            with self.assertRaises(TrackingError) as raised:
                strict_engine.run_dataset(
                    dataset["detections"],
                    dataset["truth"],
                    dataset_manifest=dataset["manifest"],
                )
            self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
            self.assertEqual(raised.exception.category, "detection_resident")
            self.assertEqual(raised.exception.frame_index, 0)

            degraded = copy.deepcopy(config)
            degraded["resources"]["policy"] = "degrade"
            degraded["resources"]["max_detections_per_frame"] = 2
            degraded["resources"]["max_parallel_gap_tracks"] = 1
            degraded_engine = TrackingEngine(
                degraded,
                base_dir=temp,
                runtime_dir=Path(temp) / "runtime_degraded",
                output_dir=Path(temp) / "output_degraded",
            )
            degraded_result = degraded_engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            report = degraded_result["sequence_results"][0].report
            self.assertIn("detection_cap", report["degraded_categories"])
            self.assertIn("gap_parallelism_capped", report["degraded_categories"])
            self.assertGreater(report["resource_report"]["total_dropped_detections"], 0)

    def test_appearance_cache_limit_returns_e_resource_limit(self) -> None:
        config = small_config()
        config["resources"]["policy"] = "error"
        config["resources"]["max_appearance_cache_entries"] = 1
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=Path(temp) / "runtime",
                output_dir=Path(temp) / "output",
            )
            with self.assertRaises(TrackingError) as raised:
                engine.run_dataset(
                    dataset["detections"],
                    dataset["truth"],
                    dataset_manifest=dataset["manifest"],
                )
            self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
            self.assertEqual(raised.exception.category, "appearance_cache")


if __name__ == "__main__":
    unittest.main()
