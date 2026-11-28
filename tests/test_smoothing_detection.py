import unittest

import bootstrap as _bootstrap  # 导入即把 src 加入 sys.path
_SRC_PATH = _bootstrap.SRC
import numpy as np

from piano_tempo.detection import (
    cusum_changepoints,
    detect_rhythm_offsets,
    detect_tempo_jumps,
    rolling_stationarity,
)
from piano_tempo.models import NoteEvent, TempoCurve
from piano_tempo.smoothing import instantaneous_tempo, lowess_smooth


def make_curve(tempos, step=1.0, voice="rh"):
    grid = np.arange(len(tempos), dtype=float) * step
    return TempoCurve(
        grid_beats=grid,
        tempo_bpm=np.asarray(tempos, dtype=float),
        bandwidth_beats=2.0,
        grid_step_beats=step,
        voice=voice,
    )


class SmoothingTests(unittest.TestCase):
    def test_lowess_recovers_constant(self):
        rng = np.random.default_rng(0)
        x = np.arange(40, dtype=float)
        y = np.full(40, 100.0) + rng.normal(0, 2, 40)
        grid = np.arange(0, 40, 2.0)
        smoothed = lowess_smooth(x, y, grid, bandwidth=4.0, min_points=3)
        np.testing.assert_allclose(smoothed, 100.0, atol=2.0)

    def test_instantaneous_tempo_uses_note_value(self):
        events = [
            NoteEvent(60, "C4", 0.0, 2.0, "rh"),
            NoteEvent(62, "D4", 2.0, 2.0, "rh"),
            NoteEvent(64, "E4", 3.0, 2.0, "rh"),
        ]
        _, tempos = instantaneous_tempo(events, base_bpm=100.0)
        self.assertAlmostEqual(tempos[1], 60.0)
        self.assertAlmostEqual(tempos[2], 120.0)


class DetectionTests(unittest.TestCase):
    def test_cusum_finds_step_change(self):
        tempos = [100.0] * 20 + [130.0] * 20
        curve = make_curve(tempos)
        changepoints = cusum_changepoints(curve, k=0.5, h=4.0)
        self.assertTrue(changepoints)
        self.assertEqual(changepoints[0]["direction"], "accel")
        beat = changepoints[0]["beat"]
        self.assertGreaterEqual(beat, 16.0)
        self.assertLessEqual(beat, 24.0)

    def test_tempo_jump_interval(self):
        tempos = [100.0] * 16 + [75.0] * 16
        curve = make_curve(tempos)
        intervals = detect_tempo_jumps(curve, jump_bpm=12.0, k=0.5, h=4.0)
        self.assertTrue(intervals)
        self.assertEqual(intervals[0].direction, "ritard")
        self.assertGreater(intervals[0].magnitude, 12.0)

    def test_stationarity_flags_shift(self):
        tempos = [100.0] * 16 + [130.0] * 16
        curve = make_curve(tempos)
        flags = rolling_stationarity(curve, window=6, pvalue=0.05)
        self.assertTrue(flags)

    def test_rhythm_offset_drag(self):
        events = []
        onset = 0.0
        for idx in range(24):
            step = 2.5 if 10 <= idx < 18 else 2.0
            onset += step
            events.append(NoteEvent(60, "C4", onset, 2.0, "lh"))
        reference = {
            "grid_beats": np.arange(0.0, 80.0, 1.0),
            "tempo_bpm": np.full(80, 60.0),
        }
        intervals = detect_rhythm_offsets(
            events, "lh", reference, offset_ratio=0.2
        )
        self.assertTrue(intervals)
        self.assertEqual(intervals[0].direction, "drag")
        self.assertGreaterEqual(intervals[0].magnitude, 0.2)


if __name__ == "__main__":
    unittest.main()
