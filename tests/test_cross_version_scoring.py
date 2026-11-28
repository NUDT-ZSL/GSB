import bootstrap as _bootstrap  # 导入即把 src 加入 sys.path

import unittest
from collections import defaultdict

import numpy as np

from piano_tempo.cross_version import compare_versions
from piano_tempo.generator import generate_dataset
from piano_tempo.io_utils import load_config
from piano_tempo.models import SectionScore
from piano_tempo.normalizer import load_record
from piano_tempo.pipeline import run_pipeline
from piano_tempo.scoring import build_coaching_plan

_SRC_PATH = _bootstrap.SRC


class CrossVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config("config/default.json")
        files = generate_dataset(
            seed=11, n_students=1, pieces_per_student=1,
            output_dir="artifacts/test_cross",
        )
        cls.grouped = defaultdict(dict)
        for path in files:
            record = load_record(path, cls.config["grid_step_beats"])
            cls.grouped[(record.student_id, record.piece_id)][
                record.format_version
            ] = run_pipeline(record, cls.config)

    def test_tolerance_violation_marks_manual_review(self):
        versions = next(iter(self.grouped.values()))
        strict_tolerance = {
            "curve_max_deviation_bpm": 0.0,
            "interval_time_deviation_beats": 0.0,
            "location_time_deviation_beats": 0.0,
        }
        report = compare_versions(versions, strict_tolerance)
        self.assertFalse(report["all_within_tolerance"])
        self.assertTrue(report["manual_review"])

    def test_realistic_tolerance_passes(self):
        versions = next(iter(self.grouped.values()))
        report = compare_versions(
            versions, self.config["cross_version"]
        )
        self.assertTrue(report["all_within_tolerance"])


class ScoringTests(unittest.TestCase):
    def test_score_capped_and_priority_mapped(self):
        config = {
            "scoring": {
                "weight_changepoints": 6.0,
                "weight_interval_width": 0.3,
                "weight_anomaly_ratio": 15.0,
                "priority_thresholds": {"high": 25.0, "medium": 12.0},
            }
        }
        from piano_tempo.scoring import score_sections
        from piano_tempo.models import AnomalyInterval, Forecast

        anomalies = [
            AnomalyInterval(0, 16, "accel", 20.0, "tempo_jump")
        ]
        forecasts = [
            Forecast(
                np.arange(8.0),
                np.ones(8) * 100,
                np.ones(8) * 100,
                np.ones(8) * 90,
                np.ones(8) * 110,
                0.9,
                1.0,
                {},
            )
        ]
        result = score_sections(
            "S1", "P1", anomalies, [4.0, 8.0], forecasts, config
        )
        self.assertLessEqual(max(item.score for item in result), 100.0)
        self.assertTrue(any(item.priority == "high" for item in result))

    def test_coaching_plan_sorted_per_student(self):
        scores = [
            SectionScore("S1", "P1", "A", 0.0, 16.0, 0, 0.0, 0.0,
                         10.0, "low"),
            SectionScore("S1", "P1", "B", 16.0, 32.0, 2, 10.0, 0.5,
                         40.0, "high"),
        ]
        plan = build_coaching_plan(scores)
        self.assertEqual(plan["S1"][0]["section_id"], "B")
        self.assertEqual(plan["S1"][0]["rank"], 1)


if __name__ == "__main__":
    unittest.main()
