from __future__ import annotations

import math
import unittest

from hdr_color.curves import (
    HLG_CONSTANTS,
    PQ_CONSTANTS,
    SDR_CONSTANTS,
    TransferFunctions,
    continuity_diagnostics,
    hlg_decode_scene,
    hlg_encode_scene,
    hlg_system_gamma,
    pq_decode_nits,
    pq_encode_nits,
    roundtrip_diagnostics,
    roundtrip_error,
)


class TransferCurveTests(unittest.TestCase):
    def test_spec_constants_are_exposed(self) -> None:
        self.assertAlmostEqual(SDR_CONSTANTS["alpha"], 1.09929682680944, places=14)
        self.assertAlmostEqual(SDR_CONSTANTS["beta"], 0.018053968510807, places=15)
        self.assertEqual(SDR_CONSTANTS["slope"], 4.5)
        self.assertEqual(SDR_CONSTANTS["gamma"], 0.45)

        self.assertEqual(HLG_CONSTANTS["a"], 0.17883277)
        self.assertEqual(HLG_CONSTANTS["b"], 0.28466892)
        self.assertEqual(HLG_CONSTANTS["c"], 0.55991073)

        self.assertEqual(PQ_CONSTANTS["m1"], 2610.0 / 16384.0)
        self.assertEqual(PQ_CONSTANTS["m2"], 2523.0 / 4096.0 * 128.0)
        self.assertEqual(PQ_CONSTANTS["c1"], 3424.0 / 4096.0)
        self.assertEqual(PQ_CONSTANTS["c2"], 2413.0 / 4096.0 * 32.0)
        self.assertEqual(PQ_CONSTANTS["c3"], 2392.0 / 4096.0 * 32.0)

    def test_piecewise_curves_are_continuous(self) -> None:
        sdr = continuity_diagnostics("SDR_GAMMA")
        hlg = continuity_diagnostics("HLG")
        pq = continuity_diagnostics("PQ")
        self.assertTrue(sdr["continuous"])
        self.assertLess(float(sdr["max_step_at_breakpoint"]), 1e-7)
        self.assertTrue(hlg["continuous"])
        self.assertLess(float(hlg["max_step_at_scene_breakpoint"]), 1e-7)
        self.assertLess(float(hlg["max_step_at_signal_breakpoint"]), 1e-7)
        self.assertFalse(pq["piecewise"])

    def test_hlg_breakpoints_meet_exactly(self) -> None:
        scene_breakpoint = 1.0 / 12.0
        self.assertAlmostEqual(hlg_encode_scene(scene_breakpoint), 0.5, places=9)
        self.assertAlmostEqual(hlg_decode_scene(0.5), scene_breakpoint, places=9)
        self.assertGreaterEqual(hlg_system_gamma(1000.0), 1.2)
        self.assertLessEqual(hlg_system_gamma(4000.0), 1.4)

    def test_pq_absolute_endpoints(self) -> None:
        self.assertEqual(pq_decode_nits(0.0), 0.0)
        self.assertAlmostEqual(pq_decode_nits(1.0), 10000.0, places=9)
        self.assertAlmostEqual(pq_encode_nits(10000.0), 1.0, places=12)
        self.assertGreater(pq_encode_nits(100.0), 0.5)
        self.assertLess(pq_encode_nits(100.0), 0.52)

    def test_all_three_transfer_roundtrips(self) -> None:
        common = [[0.0, 0.0, 0.0], [1.0, 10.0, 100.0], [100.0, 500.0, 900.0]]
        for kind, extra in (
            ("SDR_GAMMA", [[0.001, 0.1, 2.0], [100.0, 500.0, 9000.0]]),
            ("HLG", [[0.001, 0.1, 2.0], [999.0, 4000.0, 9000.0]]),
            ("PQ", [[0.001, 0.1, 2.0], [4000.0, 8000.0, 9000.0]]),
        ):
            with self.subTest(kind=kind):
                functions = TransferFunctions.create(kind, 100.0, 1000.0, 65537)
                error = roundtrip_error(functions, common + extra)
                self.assertLess(error, 0.1)
                diagnostics = roundtrip_diagnostics(functions)
                self.assertTrue(diagnostics["passes"])
                self.assertLessEqual(
                    float(diagnostics["max_relative_error"]), 1e-5
                )

    def test_invalid_transfer_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "unsupported transfer"):
            TransferFunctions.create("NOT_A_TRANSFER", 100.0, 1000.0, 257)


if __name__ == "__main__":
    unittest.main()
