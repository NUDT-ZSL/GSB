from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from proxy_diag.clock import ClockSpec
from proxy_diag.paths import Paths
from proxy_diag.verifier import run_verification


class ClockConversionTests(unittest.TestCase):
    def test_relative_second_conversion_records_rounding(self) -> None:
        clock = ClockSpec.from_dict(
            {
                "clock_id": "unit",
                "base_absolute_ms": 1_700_000_000_000,
                "v1_origin_logical_ms": 0,
                "scale_numerator": 1000,
                "scale_denominator": 1,
                "rounding": "ceil",
                "watermark_ms": 3000,
            }
        )
        converted = clock.convert_v1_second(2)
        self.assertEqual(converted["logical_clock_ms"], 2000)
        self.assertEqual(converted["absolute_ms"], 1_700_000_002_000)
        self.assertEqual(converted["rounding_direction"], "ceil")


class EndToEndVerificationTests(unittest.TestCase):
    def test_verify_report_passes_in_clean_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.open(Path(temporary) / "workbench")
            report = run_verification(paths)
            self.assertEqual(report["summary"]["failed"], 0, report)
            self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()
