from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from ragdiag.errors import E_INCONSISTENT, WorkbenchError
from ragdiag.report import validate_report
from ragdiag.runtime import run_workbench
from ragdiag.sample_generator import generate


class ReportConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.data = cls.root / "data"
        generate(cls.data)
        cls.cache_dir = cls.root / "cache"
        cls.report = run_workbench(
            cls.data / "tool_results.json",
            cls.data / "requests.jsonl",
            cls.data / "schedule.json",
            cls.data / "evidence_corpus.jsonl",
            cls.cache_dir,
            output_json=cls.root / "diagnostic.json",
            output_html=cls.root / "diagnostic.html",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_report_self_check(self) -> None:
        checked = validate_report(self.report)
        self.assertEqual(checked["status"], "ok")
        html_text = (self.root / "diagnostic.html").read_text(encoding="utf-8")
        self.assertIn("Computation Graph", html_text)
        self.assertIn("Three-Budget Admission", html_text)
        self.assertIn("Fragments and Source Paths", html_text)
        self.assertIn("Block Equivalence", html_text)

    def test_tampered_node_key_is_inconsistent(self) -> None:
        tampered = copy.deepcopy(self.report)
        tampered["graph"]["nodes"][1]["key"] = "0" * 64
        with self.assertRaises(WorkbenchError) as caught:
            validate_report(tampered)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)

    def test_tampered_invalidation_sets_are_inconsistent(self) -> None:
        tampered = copy.deepcopy(self.report)
        tampered["invalidation"] = {
            "recompute_nodes": ["merge:conv-001@1"],
            "reuse_nodes": ["merge:conv-001@1"],
            "affected_requests": ["conv-001@1"],
            "coverage": {
                "disjoint": True,
                "affected_requests_covered": True,
            },
            "counts": {"recompute_nodes": 1, "reuse_nodes": 1},
        }
        with self.assertRaises(WorkbenchError) as caught:
            validate_report(tampered)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)


if __name__ == "__main__":
    unittest.main()

