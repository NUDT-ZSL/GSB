from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tracker_app.data_gen import generate_dataset, mutate_detection
from tracker_app.engine import TrackingEngine
from tracker_app.errors import TrackingError
from tracker_app.jsonio import read_json

from tests.helpers import materialize_dataset, run_cli, small_config


COMPARABLE = (
    "assignments.jsonl",
    "match_candidates.jsonl",
    "track_states.jsonl",
    "gap_completions.jsonl",
    "gap_intervals.json",
    "gap_skipped.json",
    "segments.json",
    "metrics.json",
)


class RecoveryRevisionTests(unittest.TestCase):
    def test_forced_termination_before_and_after_commit_matches_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            config, dataset, config_path = materialize_dataset(temp)
            data_dir = temp / "data"
            full_output = temp / "output_full"
            full_engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=temp / "runtime_full",
                output_dir=full_output,
            )
            full_engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            for phase, flag in (
                ("before", "--crash-before-commit-frame"),
                ("after", "--crash-after-commit-frame"),
            ):
                runtime = temp / f"runtime_{phase}"
                output = temp / f"output_{phase}"
                crashed = run_cli(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--data",
                        str(data_dir),
                        "--runtime",
                        str(runtime),
                        "--output",
                        str(output),
                        flag,
                        "8",
                    ]
                )
                self.assertEqual(crashed.returncode, 137, crashed.stderr)
                resumed = run_cli(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--data",
                        str(data_dir),
                        "--runtime",
                        str(runtime),
                        "--output",
                        str(output),
                    ]
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                report = read_json(output / "seq-000" / "r000001" / "run_report.json")
                expected_start = 8 if phase == "before" else 9
                self.assertEqual(report["mode"], "resume")
                self.assertEqual(report["recovery"]["start_frame"], expected_start)
                for filename in COMPARABLE:
                    self.assertEqual(
                        (
                            full_output
                            / "seq-000"
                            / "r000001"
                            / filename
                        ).read_bytes(),
                        (output / "seq-000" / "r000001" / filename).read_bytes(),
                        f"{phase}:{filename}",
                    )

    def test_revision_prefix_reuse_matches_fresh_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            config = small_config()
            base_dataset = generate_dataset(config)
            revised = mutate_detection(
                base_dataset,
                seq_id="seq-000",
                frame_index=8,
                track_id_true=0,
                changes={"occluded": False, "x1": 180.0, "x2": 220.0},
                revision=2,
            )
            incremental_engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=temp / "runtime_incremental",
                output_dir=temp / "output_incremental",
            )
            incremental_engine.run_dataset(
                base_dataset["detections"],
                base_dataset["truth"],
                dataset_manifest=base_dataset["manifest"],
            )
            incremental_result = incremental_engine.run_dataset(
                revised["detections"],
                revised["truth"],
                dataset_manifest=revised["manifest"],
            )
            fresh_engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=temp / "runtime_fresh",
                output_dir=temp / "output_fresh",
            )
            fresh_result = fresh_engine.run_dataset(
                revised["detections"],
                revised["truth"],
                dataset_manifest=revised["manifest"],
            )
            incremental = incremental_result["sequence_results"][0]
            fresh = fresh_result["sequence_results"][0]
            revision_report = incremental.report["revision_report"]
            self.assertEqual(incremental.report["mode"], "revision_suffix")
            self.assertEqual(revision_report["affected_frames"], [8])
            self.assertEqual(revision_report["reused_frames"], 8)
            self.assertEqual(revision_report["recomputed_frames"], 16)
            for filename in COMPARABLE:
                self.assertEqual(
                    (incremental.output_dir / filename).read_bytes(),
                    (fresh.output_dir / filename).read_bytes(),
                    filename,
                )
            self.assertEqual(
                incremental.report["revision_report"]["discarded_old_suffix_frames"],
                16,
            )

    def test_revision_regression_and_same_revision_mutation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            config = small_config()
            base_dataset = generate_dataset(config)
            revised = mutate_detection(
                base_dataset,
                seq_id="seq-000",
                frame_index=8,
                track_id_true=0,
                changes={"occluded": False},
                revision=2,
            )
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=temp / "runtime",
                output_dir=temp / "output",
            )
            engine.run_dataset(
                base_dataset["detections"],
                base_dataset["truth"],
                dataset_manifest=base_dataset["manifest"],
            )
            mismatched_same_revision = mutate_detection(
                base_dataset,
                seq_id="seq-000",
                frame_index=8,
                track_id_true=0,
                changes={"score": 0.5},
            )
            with self.assertRaises(TrackingError) as conflict:
                engine.run_dataset(
                    mismatched_same_revision["detections"],
                    mismatched_same_revision["truth"],
                    dataset_manifest=mismatched_same_revision["manifest"],
                )
            self.assertEqual(conflict.exception.code, "E_REVISION_CONFLICT")
            engine.run_dataset(
                revised["detections"],
                revised["truth"],
                dataset_manifest=revised["manifest"],
            )
            with self.assertRaises(TrackingError) as regression:
                engine.run_dataset(
                    base_dataset["detections"],
                    base_dataset["truth"],
                    dataset_manifest=base_dataset["manifest"],
                )
            self.assertEqual(regression.exception.code, "E_REVISION_INVALID")


if __name__ == "__main__":
    unittest.main()
