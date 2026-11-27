from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from layout_analyzer.pipeline import analyze_dataset, generate_dataset
from layout_analyzer.verifier import verify_from_files


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls.temp_dir.name)
        cls.samples_dir = root / "samples"
        cls.results_dir = root / "results"
        cls.manifest = generate_dataset(cls.samples_dir, seed=424242, page_count=3)
        cls.results = analyze_dataset(cls.samples_dir, cls.results_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_fixed_output_tree_and_metrics(self) -> None:
        summary = self.results["summary"]
        self.assertTrue(summary["all_versions_consistent"])
        self.assertTrue(summary["independent_verifier_ok"])
        self.assertGreaterEqual(summary["block_segmentation_iou"], 0.82)
        self.assertGreaterEqual(summary["type_classification_accuracy"], 0.95)
        self.assertGreaterEqual(summary["kendall_rank_correlation"], 0.99)
        self.assertEqual(summary["expected_conflicts"], 1)
        self.assertEqual(summary["matched_conflicts"], 1)
        for page in self.manifest["pages"]:
            page_id = page["page_id"]
            for relative in (
                f"annotations/{page_id}.json",
                f"features/{page_id}.json",
                f"reports/{page_id}.json",
                f"reports/{page_id}.md",
                f"cross_version/{page_id}.json",
                f"verifier/{page_id}.json",
            ):
                self.assertTrue((self.results_dir / relative).is_file(), relative)
        self.assertTrue((self.results_dir / "run_results.json").is_file())

    def test_page_three_conflict_is_recorded_in_all_formats(self) -> None:
        report = json.loads(
            (self.results_dir / "reports/page_003.json").read_text(encoding="utf-8")
        )
        conflict = report["conflict_section"]["conflicts"][0]
        self.assertEqual(conflict["kind"], "ambiguous_annotation_anchor")
        self.assertEqual(len(conflict["candidate_edges"]), 2)
        comparison = json.loads(
            (self.results_dir / "cross_version/page_003.json").read_text(encoding="utf-8")
        )
        self.assertTrue(comparison["consensus"]["all_versions_consistent"])
        self.assertEqual(comparison["differences"], [])

    def test_standalone_verifier_accepts_report_and_features_only(self) -> None:
        result = verify_from_files(
            self.results_dir / "reports/page_001.json",
            self.results_dir / "features/page_001.json",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["input_contract"]["ordering_code_reused"])


if __name__ == "__main__":
    unittest.main()
