import copy
import json
import tempfile
import unittest
from pathlib import Path

from aed_scale.codes import BusinessCode
from aed_scale.consistency import run_self_check
from aed_scale.errors import AedError
from aed_scale.pipeline import run_dataset
from aed_scale.synthesis import generate_dataset


class PipelineConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name) / "dataset"
        generate_dataset(
            cls.root,
            seed=123,
            num_dialogs=2,
            duration_ms=2_200,
            speaker_count=3,
        )
        cls.report = run_dataset(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_end_to_end_report_contract(self):
        report = self.report
        self.assertEqual(report["business_code"], BusinessCode.OK.value)
        self.assertEqual(report["consistency"]["status"], BusinessCode.OK.value)
        self.assertEqual(report["selected_path"], "fine")
        self.assertFalse(report["degraded"])
        self.assertIn("decision_sha256", report["tradeoff_decision"])
        self.assertEqual(report["metrics"]["frame"]["total_frames"], 440)
        event_metrics = report["metrics"]["event"]
        self.assertGreater(event_metrics["truth_count"], 0)
        self.assertGreater(event_metrics["matched_count"], 0)
        self.assertIn("truth_events", event_metrics["details"])
        self.assertIn("predicted_events", event_metrics["details"])
        for dialog in report["dialogs"]:
            self.assertEqual(
                len(dialog["frame_decisions"]),
                dialog["frame_count"],
            )
            self.assertEqual(dialog["frame_index_checks"]["truth_count"], dialog["frame_count"])
            self.assertEqual(
                set(dialog["frame_index_checks"][key] for key in (
                    "truth_sha256",
                    "feature_sha256",
                    "detection_sha256",
                    "postprocess_sha256",
                )),
                {dialog["frame_index_checks"]["truth_sha256"]},
            )
            self.assertEqual(len(dialog["feature"]["feature_sha256"]), 64)
            self.assertGreaterEqual(len(dialog["segments"]), 1)
            self.assertEqual(dialog["segments"][0]["start_frame"], 0)
            self.assertEqual(dialog["segments"][-1]["end_frame"], dialog["frame_count"] - 1)
        json.dumps(report, sort_keys=True)

    def test_approximate_override_marks_degraded_and_stays_consistent(self):
        report = run_dataset(self.root, path_override="approximate")
        self.assertEqual(report["selected_path"], "approximate")
        self.assertEqual(report["auto_selected_path"], "fine")
        self.assertTrue(report["degraded"])
        self.assertEqual(report["consistency"]["status"], BusinessCode.OK.value)

    def test_self_check_replays_after_json_round_trip(self):
        round_tripped = json.loads(json.dumps(self.report))
        result = run_self_check(round_tripped)
        self.assertEqual(result["status"], BusinessCode.OK.value)

    def test_self_check_detects_metric_tampering(self):
        tampered = copy.deepcopy(self.report)
        tampered["dialogs"][0]["metrics"]["frame"]["frame_accuracy"] = 0.123
        with self.assertRaises(AedError) as context:
            run_self_check(tampered)
        self.assertEqual(context.exception.code, BusinessCode.E_INCONSISTENT.value)
        self.assertEqual(context.exception.dialog_id, tampered["dialogs"][0]["dialog_id"])
        self.assertEqual(context.exception.details["category"], "metrics_recompute")

    def test_self_check_detects_timeline_gap(self):
        tampered = copy.deepcopy(self.report)
        tampered["dialogs"][0]["segments"] = tampered["dialogs"][0]["segments"][:-1]
        with self.assertRaises(AedError) as context:
            run_self_check(tampered)
        self.assertEqual(context.exception.code, BusinessCode.E_INCONSISTENT.value)
        self.assertEqual(context.exception.dialog_id, tampered["dialogs"][0]["dialog_id"])
        self.assertEqual(context.exception.details["category"], "timeline_coverage")

    def test_self_check_detects_frame_set_tampering(self):
        tampered = copy.deepcopy(self.report)
        tampered["dialogs"][0]["frame_index_checks"]["feature_sha256"] = "0" * 64
        with self.assertRaises(AedError) as context:
            run_self_check(tampered)
        self.assertEqual(context.exception.code, BusinessCode.E_INCONSISTENT.value)
        self.assertEqual(context.exception.details["category"], "frame_index_sets")


if __name__ == "__main__":
    unittest.main()
