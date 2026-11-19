from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from annotation_app.demo import run_demo
from annotation_app.samplegen import write_assets
from annotation_app.storage import read_json

from tests.helpers import ROOT


class DemoTests(unittest.TestCase):
    def test_demo_is_reproducible_and_complete(self):
        first = run_demo(ROOT)
        second = run_demo(ROOT)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["report_checksum"], second["report_checksum"])
        self.assertEqual(first["migration_roundtrip"]["v1_path"]["status"], "equal")
        self.assertEqual(
            first["migration_roundtrip"]["v2_path"]["status"],
            "differences_registered",
        )
        self.assertEqual(first["line_roundtrip"]["status"], "equal")
        self.assertEqual(first["edit_session"]["status"], "replay_equal")
        self.assertTrue(first["ontology_contraction"]["strict"]["blocked"])
        self.assertEqual(first["ontology_contraction"]["lenient"]["affected_count"], 3)
        self.assertEqual(first["unattributed"], [])
        self.assertEqual(
            [row["status"] for row in first["consistency"]["checks"]],
            ["equal", "equal", "equal"],
        )

    def test_unexpected_exception_is_aggregated_and_not_continued(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_assets(root)
            with mock.patch(
                "annotation_app.demo.upgrade_v1_to_v2",
                side_effect=RuntimeError("injected failure"),
            ):
                with self.assertRaises(RuntimeError):
                    run_demo(root)
            report = read_json(root / "cache" / "demo-report.json")
            self.assertEqual(report["status"], "internal_error")
            self.assertEqual(report["unattributed"][0]["code"], "E_UNATTRIBUTED")
            self.assertIn("injected failure", report["unattributed"][0]["message"])


if __name__ == "__main__":
    unittest.main()

