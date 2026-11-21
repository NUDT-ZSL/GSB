import unittest

from asr_gov.adaptive import ScaleObservation, decide, estimate_comparisons
from asr_gov.sample_data import default_config


class TestAdaptive(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

    def test_exact_path_small_scale(self):
        obs = ScaleObservation(num_slices=3, num_frames=478,
                               num_speakers=2, memory_headroom_bytes=1 << 22)
        decision = decide(obs, self.cfg)
        self.assertEqual(decision.path, "exact")
        self.assertEqual(decision.boundary_method, "per_frame_refine")
        self.assertEqual(decision.comparison_mode, "full_cross_slice")
        self.assertEqual(decision.estimated_comparisons, 3)
        self.assertEqual(decision.frame_shift_ms, self.cfg.frame_shift_ms)

    def test_approx_path_large_scale(self):
        obs = ScaleObservation(num_slices=5000, num_frames=9_000_000,
                               num_speakers=40, memory_headroom_bytes=1 << 10)
        decision = decide(obs, self.cfg)
        self.assertEqual(decision.path, "approximate")
        self.assertEqual(decision.boundary_method, "quantile_boundary_estimate")
        self.assertEqual(decision.comparison_mode, "blocked")
        self.assertEqual(decision.frame_shift_ms,
                         self.cfg.frame_shift_ms * self.cfg.approx_shift_multiplier)
        self.assertLess(decision.estimated_comparisons,
                        estimate_comparisons(5000, "exact", 0))
        self.assertTrue(decision.reasons)

    def test_decision_byte_deterministic(self):
        obs = ScaleObservation(num_slices=5000, num_frames=9_000_000,
                               num_speakers=40, memory_headroom_bytes=1 << 10)
        self.assertEqual(decide(obs, self.cfg).canonical(),
                         decide(obs, self.cfg).canonical())

    def test_comparison_estimates(self):
        self.assertEqual(estimate_comparisons(10, "exact", 0), 45)
        blocked = estimate_comparisons(130, "approximate", 64)
        self.assertEqual(blocked, 2 * (64 * 63 // 2) + 1)


if __name__ == "__main__":
    unittest.main()
