from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice_state.aggregation import recompute_dialog_from_details
from voice_state.canonical import canonical_json_bytes, read_jsonl, write_jsonl_atomic
from voice_state.config import AnalysisConfig
from voice_state.dataset import load_audio, load_dataset, load_truth
from voice_state.engine import AnalysisEngine
from voice_state.errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_INCONSISTENT,
    E_SPEAKER_INVALID,
)
from voice_state.features import assign_speaker_id, extract_energy_zcr
from voice_state.selfcheck import run_selfcheck

from tests.helpers import make_dataset


class FramingTests(unittest.TestCase):
    def test_frame_features_are_deterministic_and_truth_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=2, seed=11)
            dataset = load_dataset(dataset_dir)
            record = dataset.dialogs[0]
            config = AnalysisConfig()
            audio = load_audio(record, config.sample_rate)
            truth = load_truth(record)
            first = extract_energy_zcr(audio, config)
            second = extract_energy_zcr(audio, config)
            self.assertEqual(len(first), len(truth))
            self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
            self.assertEqual(first[0]["frame_index"], 0)
            self.assertEqual(first[-1]["frame_index"], len(truth) - 1)

    def test_invalid_configuration_fields_are_reported(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            AnalysisConfig(sample_rate=8000).validate()
        self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "sample_rate")

        with self.assertRaises(BusinessError) as caught:
            AnalysisConfig(frame_ms=20.0, hop_ms=21.0).validate()
        self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "hop_ms")

    def test_manifest_sample_rate_is_rejected_with_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=1, seed=12)
            manifest_path = dataset_dir / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            rows[0]["sample_rate"] = 8000
            write_jsonl_atomic(manifest_path, rows)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            with self.assertRaises(BusinessError) as caught:
                engine.run_stage("framing")
            self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
            self.assertEqual(caught.exception.details["field"], "sample_rate")

    def test_frame_count_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=1, seed=13)
            truth_path = dataset_dir / "truth" / "dlg_0001.jsonl"
            rows = read_jsonl(truth_path)
            rows = rows[:-1]
            write_jsonl_atomic(truth_path, rows)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            with self.assertRaises(BusinessError) as caught:
                engine.run_stage("framing")
            self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
            self.assertEqual(caught.exception.details["field"], "num_frames")


class SegmentationTests(unittest.TestCase):
    def test_out_of_registry_truth_speaker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=2, seed=21)
            truth_path = dataset_dir / "truth" / "dlg_0001.jsonl"
            rows = read_jsonl(truth_path)
            frame_index = 7
            rows[frame_index]["speaker_id"] = 99
            write_jsonl_atomic(truth_path, rows)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            with self.assertRaises(BusinessError) as caught:
                engine.run()
            self.assertEqual(caught.exception.code, E_SPEAKER_INVALID)
            self.assertEqual(caught.exception.details["dialog_id"], "dlg_0001")
            self.assertEqual(caught.exception.details["frame_index"], frame_index)

    def test_exact_similarity_tie_uses_lowest_speaker_id(self) -> None:
        descriptor = __import__("numpy").asarray(
            [8.8, 0.1, 0.09, 0.007, 0.23, 0.74, 0.02, 0.001],
            dtype=float,
        )
        statistics = {
            5: {"mean": descriptor.tolist(), "std": [0.1] * 8, "frame_count": 100},
            2: {"mean": descriptor.tolist(), "std": [0.1] * 8, "frame_count": 100},
        }
        self.assertEqual(assign_speaker_id(descriptor, statistics, AnalysisConfig()), 2)


class RegressionAggregationTests(unittest.TestCase):
    def test_scores_are_reproducible_and_independently_recomputable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=2, speakers=2, seed=31)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            first = engine.run()
            second = engine.run()
            self.assertEqual(first.report_bytes, second.report_bytes)
            self.assertEqual(second.computed_stages, [])
            self.assertEqual(second.reused_stages, ["framing", "segmentation", "regression", "aggregation"])
            self.assertEqual(first.report_hash, second.report_hash)
            for dialog in first.report["dialogs"]:
                valence, arousal = recompute_dialog_from_details(dialog)
                self.assertAlmostEqual(valence, dialog["weighted_valence"], places=10)
                self.assertAlmostEqual(arousal, dialog["weighted_arousal"], places=10)
                for segment in dialog["segments"]:
                    self.assertLessEqual(segment["valence_ci95"][0], segment["valence"])
                    self.assertGreaterEqual(segment["valence_ci95"][1], segment["valence"])
                    self.assertLessEqual(segment["arousal_ci95"][0], segment["arousal"])
                    self.assertGreaterEqual(segment["arousal_ci95"][1], segment["arousal"])
            expected = sorted(
                first.report["dialogs"],
                key=lambda item: (
                    -item["weighted_arousal"],
                    -item["weighted_valence"],
                    item["dialog_id"],
                ),
            )
            self.assertEqual(
                [row["dialog_id"] for row in first.report["ranking"]],
                [row["dialog_id"] for row in expected],
            )
            check = run_selfcheck(
                engine.store,
                expected_dialog_ids=[item.dialog_id for item in engine.dataset.dialogs],
                full_report_bytes=first.report_bytes,
                recovered_report_bytes=second.report_bytes,
            )
            self.assertTrue(check["ok"])

    def test_inconsistent_dialog_set_returns_e_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=1, seed=32)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            result = engine.run()
            with self.assertRaises(BusinessError) as caught:
                run_selfcheck(
                    engine.store,
                    expected_dialog_ids=["not-a-dialog"],
                    full_report_bytes=result.report_bytes,
                    recovered_report_bytes=result.report_bytes,
                )
            self.assertEqual(caught.exception.code, E_INCONSISTENT)
            self.assertEqual(caught.exception.details["failed_checks"][0]["name"], "dialog_set_consistency")

    def test_recovered_report_hash_mismatch_returns_e_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=1, seed=33)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            result = engine.run()
            with self.assertRaises(BusinessError) as caught:
                run_selfcheck(
                    engine.store,
                    expected_dialog_ids=[item.dialog_id for item in engine.dataset.dialogs],
                    full_report_bytes=result.report_bytes,
                    recovered_report_bytes=result.report_bytes + b"tampered\n",
                )
            self.assertEqual(caught.exception.code, E_INCONSISTENT)
            failed_names = [item["name"] for item in caught.exception.details["failed_checks"]]
            self.assertIn("recovery_byte_identity", failed_names)


if __name__ == "__main__":
    unittest.main()

