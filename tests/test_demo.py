from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bmm_pipeline.demo import run_demo
from bmm_pipeline.errors import PipelineError


class DemoTests(unittest.TestCase):
    def test_non_demo_directory_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "unrelated"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(PipelineError) as context:
                run_demo(target, fault_after_blocks=1)
            self.assertEqual(context.exception.code, "E_JOB_INVALID")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_hard_exit_resume_demo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = run_demo(
                Path(temp) / "demo",
                fault_after_blocks=10,
            )
            self.assertTrue(report["ok"])
            self.assertNotEqual(report["crash_exit_code"], 0)
            self.assertEqual(report["committed_stages"], ["validation", "planning"])
            self.assertEqual(
                report["rebuilt_stages"], ["accumulation", "aggregation"]
            )
            self.assertEqual(report["discarded_blocks"], 10)
            self.assertTrue(report["summary_byte_identical"])
            self.assertTrue(all(report["elementwise_identical"].values()))
            self.assertTrue(report["verification_ok"])


if __name__ == "__main__":
    unittest.main()
