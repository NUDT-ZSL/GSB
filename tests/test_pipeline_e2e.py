import unittest
from collections import defaultdict

import bootstrap as _bootstrap  # 导入即把 src 加入 sys.path
_SRC_PATH = _bootstrap.SRC
import numpy as np

from piano_tempo.cross_version import compare_versions
from piano_tempo.generator import generate_dataset
from piano_tempo.injection import inject_pattern
from piano_tempo.io_utils import load_config
from piano_tempo.normalizer import load_record
from piano_tempo.pipeline import aggregate_coaching, run_pipeline


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config("config/default.json")
        cls.files = generate_dataset(
            seed=cls.config["generation"]["seed"],
            n_students=2,
            pieces_per_student=2,
            output_dir="artifacts/test_e2e",
        )
        cls.grouped = defaultdict(dict)
        cls.runs = []
        for path in cls.files:
            record = load_record(path, cls.config["grid_step_beats"])
            result = run_pipeline(record, cls.config)
            cls.grouped[(record.student_id, record.piece_id)][
                record.format_version
            ] = result
            cls.runs.append(result)

    def test_each_run_has_required_outputs(self):
        for result in self.runs:
            self.assertEqual(set(result["curves"]), {"rh", "lh"})
            for voice in ("rh", "lh"):
                curve = result["curves"][voice]
                self.assertEqual(
                    curve["bandwidth_beats"],
                    self.config["smoothing"]["lowess_bandwidth_beats"],
                )
                self.assertEqual(
                    curve["grid_step_beats"],
                    self.config["grid_step_beats"],
                )
                self.assertTrue(len(curve["tempo_bpm"]) > 10)
                forecast = result["forecasts"][voice]
                self.assertEqual(
                    forecast["interval_level"],
                    self.config["forecast"]["interval_level"],
                )
            self.assertEqual(len(result["section_scores"]), 4)
            for score in result["section_scores"]:
                self.assertIn(score["priority"], ("low", "medium", "high"))

    def test_cross_version_consistency(self):
        for key, versions in self.grouped.items():
            report = compare_versions(versions, self.config["cross_version"])
            self.assertTrue(
                report["all_within_tolerance"],
                msg=f"{key} 跨版本超限: {report['manual_review']}",
            )

    def test_truth_validation_populated(self):
        for result in self.runs:
            bias = result["validation"]["curve_bias"]["per_voice"]
            for voice in ("rh", "lh"):
                self.assertTrue(np.isfinite(bias[voice]["rmse_bpm"]))
            coverage = result["validation"]["forecast_coverage"]
            for voice in ("rh", "lh"):
                self.assertIn(coverage[voice]["status"], ("ok", "below_tolerance"))
            hit = result["validation"]["anomaly_hit_rate"]
            self.assertGreaterEqual(hit["recall"], 0.0)
            self.assertLessEqual(hit["recall"], 1.0)

    def test_coaching_plan_grouped_and_sorted(self):
        plan = aggregate_coaching(self.runs)
        self.assertEqual(len(plan), 2)
        for student_id, entries in plan.items():
            scores = [entry["score"] for entry in entries]
            self.assertEqual(scores, sorted(scores, reverse=True))
            ranks = [entry["rank"] for entry in entries]
            self.assertEqual(ranks, list(range(1, len(entries) + 1)))

    def test_injected_patterns_are_detected(self):
        record = load_record(
            self.files[0], self.config["grid_step_beats"]
        )
        for injection in self.config["injections"]:
            injected = inject_pattern(record, injection)
            result = run_pipeline(injected, self.config)
            in_window = [
                anomaly
                for anomaly in result["anomalies"]
                if anomaly["start_beat"] <= injection["end_beat"]
                and anomaly["end_beat"] >= injection["start_beat"]
            ]
            self.assertTrue(
                in_window,
                msg=f"注入片段 {injection['name']} 未被检测到",
            )
            truth_kinds = [
                section["kind"] for section in injected.true_sections
            ]
            self.assertIn(injection["kind"], truth_kinds)


if __name__ == "__main__":
    unittest.main()
