from __future__ import annotations

import json
import unittest

from llm_sampling_diag.canonical import canonical_json
from llm_sampling_diag.errors import DiagnosticError, ErrorCode
from llm_sampling_diag.formats import SuiteSources
from llm_sampling_diag.model import OperatorConfig
from llm_sampling_diag.selfcheck import run_self_check

from tests.support import cleanup, make_suite


class SelfCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir, cls.suite = make_suite()

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup(cls.temp_dir)

    def test_selfcheck_is_consistent_in_float64_and_float32(self) -> None:
        for dtype in ("float64", "float32"):
            config = OperatorConfig(
                mask_priority="mask_first",
                dtype=dtype,
                rounding_mode="none",
                subnormal_guard="clamp_tiny",
                tolerance=1e-5,
            )
            report = run_self_check(self.suite, config=config)
            self.assertTrue(report["summary"]["all_consistent"])
            self.assertEqual(report["summary"]["case_count"], 4)
            self.assertEqual(report["summary"]["tasks_checked"], 12)
            for case in report["cases"]:
                sequences = case["sample_sequences"]
                self.assertEqual(len({canonical_json(value) for value in sequences.values()}), 1)
                self.assertEqual(len({case["cache_keys"][name] for name in case["cache_keys"]}), 1)

    def test_cross_format_normalization_mismatch_is_inconsistent(self) -> None:
        mutable = json.loads((self.suite / "v1" / "tasks.json").read_text(encoding="utf-8"))
        mutable["tasks"][0]["logits"][0] += 1e-9
        altered = self.temp_dir / "altered_v1.json"
        altered.write_text(json.dumps(mutable), encoding="utf-8")
        sources = SuiteSources(v1=altered, v2=self.suite / "v2", v3=self.suite / "v3" / "tasks.json")
        with self.assertRaises(DiagnosticError) as context:
            run_self_check(sources=sources)
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_INCONSISTENT)
        self.assertIn("logits", error.field)
        self.assertIn("first_deviation", error.details)
        self.assertEqual(error.case_id, "case_tie_topk_mask")

    def test_missing_format_case_is_inconsistent(self) -> None:
        mutable = json.loads((self.suite / "v3" / "tasks.json").read_text(encoding="utf-8"))
        mutable["tasks"] = mutable["tasks"][1:]
        altered = self.temp_dir / "altered_v3.json"
        altered.write_text(json.dumps(mutable), encoding="utf-8")
        sources = SuiteSources(v1=self.suite / "v1" / "tasks.json", v2=self.suite / "v2", v3=altered)
        with self.assertRaises(DiagnosticError) as context:
            run_self_check(sources=sources)
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_INCONSISTENT)
        self.assertEqual(error.details["missing_format"], "v3")


if __name__ == "__main__":
    unittest.main()

