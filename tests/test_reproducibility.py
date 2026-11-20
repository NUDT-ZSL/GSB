from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from leasecoord.db import answer_json
from scripts.demo import run_demo


class ReproducibilityTests(unittest.TestCase):
    def test_demo_output_is_identical_across_two_clean_runs(self) -> None:
        with TemporaryDirectory() as first_temp, TemporaryDirectory() as second_temp:
            first = run_demo(Path(first_temp) / "demo", workers=4)
            second = run_demo(Path(second_temp) / "demo", workers=4)
            self.assertTrue(first["ok"], first)
            self.assertEqual(answer_json(first), answer_json(second))


if __name__ == "__main__":
    unittest.main()
