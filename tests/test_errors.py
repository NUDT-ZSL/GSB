from __future__ import annotations

import unittest
from pathlib import Path

from term_pipeline.injection import EXPECTED_CODES, inject_error


class ErrorInjectionTests(unittest.TestCase):
    def test_all_required_business_errors(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for case, expected in EXPECTED_CODES.items():
            with self.subTest(case=case):
                result = inject_error(case, root)
                self.assertEqual(
                    result["observed"],
                    expected,
                    result.get("message"),
                )
                self.assertTrue(result["ok"])

