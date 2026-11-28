import unittest

import bootstrap as _bootstrap  # 导入即把 src 加入 sys.path
_SRC_PATH = _bootstrap.SRC
import numpy as np

from piano_tempo.forecast import (
    fit_combined_model,
    forecast_curve,
    rolling_one_step,
)
from piano_tempo.models import TempoCurve
from piano_tempo.robust import detect_robust_anomalies, robust_z


def make_curve(tempos):
    return TempoCurve(
        grid_beats=np.arange(len(tempos), dtype=float),
        tempo_bpm=np.asarray(tempos, dtype=float),
        bandwidth_beats=2.0,
        grid_step_beats=1.0,
        voice="rh",
    )


class ForecastTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        x = np.arange(60, dtype=float)
        self.y = 100.0 + 0.1 * x + rng.normal(0, 1.5, 60)
        self.curve = make_curve(self.y)

    def test_fit_combined_model(self):
        model = fit_combined_model(
            np.arange(60, dtype=float), self.y, n_breakpoints=4, ar_order=4
        )
        self.assertEqual(len(model.ar_coef), 4)
        self.assertEqual(len(model.breakpoints), 4)
        self.assertGreater(model.residual_sigma, 0)

    def test_forecast_intervals_contain_level(self):
        forecast = forecast_curve(
            self.curve,
            n_breakpoints=4,
            ar_order=4,
            interval_level=0.9,
            horizon=8,
            min_train_points=12,
        )
        self.assertEqual(len(forecast.predicted), 8)
        self.assertTrue(np.all(forecast.upper > forecast.predicted))
        self.assertTrue(np.all(forecast.lower < forecast.predicted))
        self.assertEqual(forecast.interval_level, 0.9)
        self.assertIn("ar_coef", forecast.model_summary)

    def test_rolling_one_step_trains_only_on_past(self):
        result = rolling_one_step(
            self.curve.grid_beats,
            self.curve.tempo_bpm,
            n_breakpoints=3,
            ar_order=2,
            min_train_points=12,
        )
        self.assertTrue(np.isnan(result["predicted"][:12]).all())
        self.assertFalse(np.isnan(result["predicted"][12:]).any())


class RobustTests(unittest.TestCase):
    def test_robust_z_outlier(self):
        values = np.concatenate(
            [np.ones(30) * 100.0, np.array([140.0])]
        )
        z = robust_z(values)
        self.assertGreater(abs(z[-1]), 3.0)
        self.assertLess(np.max(np.abs(z[:-1])), 2.0)

    def test_detect_robust_anomaly_spike(self):
        rng = np.random.default_rng(5)
        tempos = 100.0 + rng.normal(0, 1.0, 60)
        tempos[40:42] += 25.0
        curve = make_curve(tempos)
        anomalies, evidence = detect_robust_anomalies(
            curve,
            n_breakpoints=3,
            ar_order=3,
            min_train_points=12,
            z_threshold=3.5,
            merge_gap_beats=2.0,
        )
        self.assertTrue(anomalies)
        self.assertTrue(
            any(35.0 <= item.start_beat <= 45.0 for item in anomalies)
        )
        window = anomalies[0].detail["evidence_window"]
        self.assertIn("observed", window)
        self.assertIn("robust_z", window)
        self.assertEqual(
            len(window["observed"]), len(window["robust_z"])
        )


if __name__ == "__main__":
    unittest.main()
